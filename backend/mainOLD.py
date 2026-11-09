import asyncio
import base64
import ipaddress
import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from io import BytesIO
from urllib.parse import urlparse

import asyncpg
import ollama
from crawl4ai import AsyncWebCrawler
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pdf2image import convert_from_bytes
from pydantic import BaseModel, field_validator

# ==========================================================================
# CONFIG
# --------------------------------------------------------------------------
# Central place for all environment-driven settings. Using os.getenv with
# defaults means the service is configurable in prod (docker-compose/k8s
# env vars) but still runs out-of-the-box locally without a .env file.
# ==========================================================================

# Which Ollama models power the two LLM-dependent stages of the pipeline:
# LOCAL_MODEL drives summarization/title/keyword generation (text-only,
# small/fast model), OCR_MODEL is a vision model used only for extracting
# text out of images/PDF pages.
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:3b")
OCR_MODEL = os.getenv("OCR_MODEL", "deepseek-ocr")

# MAX_CONTENT_CHARS: hard cap on how much scraped/OCR'd text is fed into the
# summarization prompt, to keep the model's context window and latency
# bounded regardless of how large the source page/document is.
MAX_CONTENT_CHARS = int(os.getenv("MAX_CONTENT_CHARS", "15000"))

# CRAWL_TIMEOUT_SECONDS: upper bound on how long crawl4ai is allowed to
# spend fetching+rendering a URL before we give up and return a 504.
CRAWL_TIMEOUT_SECONDS = int(os.getenv("CRAWL_TIMEOUT_SECONDS", "30"))

# MAX_CONCURRENT_MODEL_CALLS: size of the semaphore guarding all Ollama
# calls. Defaults to 1, i.e. fully serialized model access — appropriate
# when a single local Ollama instance/GPU can only do one generation at a
# time; raising this assumes the backend can truly handle concurrent calls.
MAX_CONCURRENT_MODEL_CALLS = int(os.getenv("MAX_CONCURRENT_MODEL_CALLS", "1"))

# Basic SSRF guardrails: only allow http/https schemes, and reject a
# hard-coded blocklist of obviously-local hostnames outright before any
# DNS resolution happens (see resolve_and_check_host() for the deeper,
# IP-based check).
ALLOWED_URL_SCHEMES = {"http", "https"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

# GENERATION_TIMEOUT_SECONDS: wall-clock budget for the summary-generation
# streaming loop. If the model runs long, we truncate gracefully rather
# than hanging the SSE connection indefinitely.
GENERATION_TIMEOUT_SECONDS = int(os.getenv("GENERATION_TIMEOUT_SECONDS", "120"))

# Postgres connection string for the results store.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/DSR_db")

# Prompt template for the main summarization step. Bakes in output-style
# rules (quotes, paragraph spacing, bullet points, no numbered lists, code
# block formatting, avoid the literal word "summary", break on markdown
# headers) directly into the instruction rather than via structured params.
SUMMARY_PROMPT_TEMPLATE = (
    """ 
      <task-context>
      You are a helpful assistant that summarizes the content of a URL.
      </task-context>
      <rules>
      - Use quotes from the content of the website to your output.
      - Use paragraphs in your output to separate content by including an empty line between paragraphs.
      - Use bullet points in your output.
      - do not use numbered lists in your output.
      - Use code blocks in your output if the content is code snippets and color them.
      - Not use the word "summary" or "Summary" or "SUMMARY" in your output.
      - break a line when there is new subtitle ### or ## or # in the content.    
      </rules>
      """
    " it's very important to highlight key takeaways:\n\n{content}"
)
# Second-pass prompt: given the finished summary, ask the model to produce
# a short title. Explicitly forbids preambles/markdown so the raw response
# can be used as-is (after a strip/quote-strip) without further parsing.
TITLE_PROMPT_TEMPLATE = (
    "Based on the following summary, generate a short, punchy title. "
    "Return ONLY the title text with no extra commentary, prefixes, or markdown formatting.\n\n"
    "Summary:\n{summary}"
)
# Third-pass prompt: same idea but for a small comma-separated keyword list,
# used for tagging/search in the UI.
KEYWORD_PROMPT_TEMPLATE = (
    "Based on the following summary, extract 3 to 6 core comma-separated keywords. "
    "Return ONLY the comma-separated keywords with no extra commentary or markdown formatting.\n\n"
    "Summary:\n{summary}"
)

# Logging: writes to a local file (summarizer.log), overwritten on each
# process start (filemode="w"). force=True resets any handlers configured
# by imported libraries so this config actually takes effect.

# ==========================================================================
# LOGGING
# ==========================================================================

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "summarizer.log")

logger = logging.getLogger("summarizer")
logger.setLevel(logging.INFO)
logger.propagate = False

# Avoid adding duplicate handlers if the module is reloaded.
if not logger.handlers:
    file_handler = logging.FileHandler(
        LOG_FILE,
        mode="w",
        encoding="utf-8"
    )

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

# ==========================================================================
# logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", filename="summarizer.log", filemode="w", force=True)
# logger = logging.getLogger("summarizer")
# ==========================================================================

# Global semaphore shared by every Ollama call site (summary streaming,
# title generation, keyword extraction, OCR frames) so that no matter which
# endpoint or code path triggers a model call, total concurrency into
# Ollama never exceeds MAX_CONCURRENT_MODEL_CALLS.
model_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MODEL_CALLS)


# ==========================================================================
# APP SETUP & LIFESPAN (DATABASE POOL)
# --------------------------------------------------------------------------
# FastAPI lifespan hook: runs once at startup (before yield) and once at
# shutdown (after yield). Used here to set up long-lived resources — the
# Ollama client and the asyncpg connection pool — once per process instead
# of per-request, and to guarantee the `summaries` table exists before any
# request tries to use it.
# ==========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared async Ollama client for the whole app's lifetime.
    app.state.ollama_client = ollama.AsyncClient()

    # Initialize PostgreSQL connection pool and ensure table exists.
    app.state.db_pool = await asyncpg.create_pool(DATABASE_URL)
    print("Connected to Postgres, DSN=%s", DATABASE_URL)

    # Idempotent schema bootstrap — safe to run on every startup since
    # CREATE TABLE IF NOT EXISTS is a no-op once the table is there.
    async with app.state.db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id SERIAL PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_identifier TEXT NOT NULL,
                title TEXT,
                summary TEXT,
                keywords TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("Ensured summaries table exists")

    # Everything after yield runs on shutdown: cleanly close the pool so
    # connections aren't leaked when the process exits.
    yield

    await app.state.db_pool.close()


app = FastAPI(lifespan=lifespan)

# CORS: frontend origin(s) come from an env var (comma-separated to support
# multiple environments/domains at once), defaulting to the local Vite dev
# server port. allow_credentials=True + allow_methods/headers="*" is a
# permissive setup appropriate for a small internal tool, but worth
# tightening if this is ever exposed publicly.
frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
origins = [origin.strip() for origin in frontend_url.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================================================
# SSRF PROTECTION: HOSTNAME RESOLUTION + IP VALIDATION
# --------------------------------------------------------------------------
# Defense against server-side request forgery: before the crawler is ever
# allowed to fetch a user-supplied URL, resolve its hostname and reject it
# if any resolved IP falls into a private/internal/reserved range. This
# stops naive attempts to make the backend hit internal services (e.g.
# cloud metadata endpoints, internal admin panels) via a public-looking
# hostname.
#
# IMPORTANT: both routes below now call this — and the file-type check for
# uploads — synchronously in the route function, *before* the
# StreamingResponse/event_generator is created. Once an SSE stream has
# started, the HTTP status code is already committed to 200, so any
# failure discovered inside the generator can only be reported as an
# "error" SSE event, not a proper 4xx. Running validation first keeps the
# error-reporting behavior identical (a clean 400) for every source type.
# ==========================================================================

async def resolve_and_check_host(hostname: str) -> None:
    """Resolve hostname to all its IPs and reject if any are private/internal.

    Note: this closes the "obvious hostname" gap (localhost, 127.0.0.1, etc.)
    but does not fully close DNS rebinding, since crawl_url() performs its
    own separate resolution later. For full protection, resolve once here,
    validate the IP, then pass that IP directly to the crawler (with the
    original Host header) instead of re-resolving — or enforce network-level
    egress rules blocking private ranges from the crawler's environment.
    """
    try:
        loop = asyncio.get_event_loop()
        # getaddrinfo returns all A/AAAA records - check every one,
        # not just the first, since DNS can return multiple IPs
        addrinfo = await loop.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        # Unresolvable host — surfaced to the caller as a validation error
        # rather than letting the crawler fail later with a less clear message.
        raise ValueError(f"Could not resolve host: {hostname}") from exc

    for _family, _, _, _, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)
        # Reject any address that isn't a routable, public unicast IP:
        # private ranges (RFC1918), loopback, link-local (this also covers
        # 169.254.169.254, the common cloud metadata endpoint), other IANA
        # "reserved" blocks, multicast, and the unspecified address.
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local      # covers 169.254.0.0/16 - cloud metadata range
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"URL host resolves to a disallowed address ({ip_str})"
            )


async def validate_url_or_raise(url: str) -> None:
    """Shared helper: parse + SSRF-check a URL, raising HTTPException(400) on failure.

    Used by both routes so a bad URL always produces the same clean 400
    response, called before any StreamingResponse is constructed.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL format.")
    try:
        await resolve_and_check_host(hostname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ==========================================================================
# REQUEST SCHEMA
# --------------------------------------------------------------------------
# Pydantic model for the JSON body of /api/summarize-url. Validation is
# split into two layers: cheap synchronous checks here (scheme, hostname
# presence, obvious blocked hostnames), and the async DNS-based IP check
# (resolve_and_check_host, via validate_url_or_raise) done separately in
# the route handler, since Pydantic v2 field validators can't be async.
# ==========================================================================

class ScrapeRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url_format(cls, value: str) -> str:
        """Sync checks only: scheme + hostname presence.

        DNS-based checks (resolve_and_check_host) run separately in the
        route handler since they require async I/O and can't live in a
        sync Pydantic validator.
        """
        parsed = urlparse(value)
        if parsed.scheme not in ALLOWED_URL_SCHEMES:
            raise ValueError(f"URL scheme must be one of {ALLOWED_URL_SCHEMES}")
        if not parsed.hostname:
            raise ValueError("URL must include a hostname")
        if parsed.hostname.lower() in BLOCKED_HOSTS:
            raise ValueError("URL host is not allowed")
        return value


# ==========================================================================
# SSE HELPERS
# --------------------------------------------------------------------------
# Small utilities for formatting Server-Sent Events. Every event is a JSON
# object with a "type" (status/summary_chunk/title/keywords/error) and a
# "content" payload, so the frontend can dispatch on `type` with a single
# parser. SSE_DONE is the sentinel the client uses to know the stream has
# ended (mirrors the OpenAI-style "[DONE]" convention).
# ==========================================================================

def sse_event(event_type: str, content: str) -> str:
    return f"data: {json.dumps({'type': event_type, 'content': content})}\n\n"


SSE_DONE = "data: [DONE]\n\n"


# ==========================================================================
# PIPELINE STEPS
# --------------------------------------------------------------------------
# The reusable building blocks of the summarization pipeline: crawling a
# URL, OCR'ing an uploaded image/PDF (with live progress), and the two
# short LLM post-processing passes (title, keywords). run_summarization_
# pipeline() below stitches summarization/title/keywords/save into one
# shared generator used by both routes, so that logic exists in exactly
# one place instead of being duplicated per-endpoint.
# ==========================================================================

# async def crawl_url(url: str) -> str:
#     # Uses crawl4ai's AsyncWebCrawler to fetch + render the page and return
#     # its content as markdown (handles JS-rendered pages better than a raw
#     # HTTP GET would). Bounded by CRAWL_TIMEOUT_SECONDS so a slow/hanging
#     # site can't block the request indefinitely.
#     try:
#         async with AsyncWebCrawler() as crawler:
#             result = await asyncio.wait_for(
#                 crawler.arun(url=url), timeout=CRAWL_TIMEOUT_SECONDS
#             )
#     except asyncio.TimeoutError as exc:
#         raise HTTPException(status_code=504, detail="Crawl timed out.") from exc

#     if not result.success:
#         raise HTTPException(status_code=400, detail="Crawl failed.")
#     if not result.markdown:
#         raise HTTPException(status_code=400, detail="No content found.")

#     return result.markdown

async def crawl_url(url: str) -> str:
    print("CRAWL: starting URL=%s", url)

    try:
        async with AsyncWebCrawler() as crawler:
            print("CRAWL: crawler initialized")

            result = await asyncio.wait_for(
                crawler.arun(url=url),
                timeout=CRAWL_TIMEOUT_SECONDS
            )

            print("CRAWL: arun() returned")

    except asyncio.TimeoutError as exc:
        logger.error("CRAWL: TIMEOUT after %s seconds", CRAWL_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=504,
            detail="Crawl timed out."
        ) from exc

    except Exception:
        logger.exception("CRAWL: unexpected exception")
        raise

    print(
        "CRAWL: success=%s markdown_length=%s",
        result.success,
        len(result.markdown or "")
    )

    if not result.success:
        raise HTTPException(status_code=400, detail="Crawl failed.")

    if not result.markdown:
        raise HTTPException(status_code=400, detail="No content found.")

    return result.markdown

async def process_single_ocr_frame(client: ollama.AsyncClient, base64_image: str) -> str:
    # Sends one base64-encoded image (a PDF page or a standalone image
    # upload) to the vision OCR model with a fixed instruction prompt, and
    # returns the extracted markdown. Serialized via model_semaphore along
    # with every other Ollama call in the app.
    prompt = "Free OCR. Convert text and document structures accurately to markdown."
    async with model_semaphore:
        response = await client.chat(
            model=OCR_MODEL,
            messages=[{
                "role": "user",
                "content": prompt,
                "images": [base64_image]
            }]
        )
    return response.get("message", {}).get("content", "")


async def parse_file_with_ocr(client: ollama.AsyncClient, file: UploadFile, status_callback=None) -> str:
    # Entry point for extracting text out of an uploaded file. Branches on
    # content type: PDFs are rasterized page-by-page and OCR'd in parallel;
    # anything else is treated as a single image and OCR'd directly.
    # `status_callback`, if given, is an async function called with a
    # human-readable progress message at each meaningful step — see
    # ocr_with_progress() below for how this actually reaches the client.
    file_bytes = await file.read()

    if file.content_type == "application/pdf":
        if status_callback:
            await status_callback("Converting PDF pages to images...")
        # pdf2image (poppler-backed) renders every page to a PIL image.
        images = convert_from_bytes(file_bytes)

        async def process_page(pil_img, page_num):
            # Re-encode each page as JPEG bytes -> base64, then OCR it.
            buffered = BytesIO()
            pil_img.save(buffered, format="JPEG")
            b64_img = base64.b64encode(buffered.getvalue()).decode("utf-8")
            if status_callback:
                await status_callback(f"Running OCR on PDF page {page_num + 1} of {len(images)}...")
            text = await process_single_ocr_frame(client, b64_img)
            return page_num, text

        # Fire all pages concurrently; model_semaphore inside
        # process_single_ocr_frame still serializes the actual Ollama
        # calls, so this just lets pages queue up rather than truly
        # running OCR in parallel.
        tasks = [process_page(img, i) for i, img in enumerate(images)]
        results = await asyncio.gather(*tasks)

        # Re-sort by original page order (asyncio.gather preserves order
        # of the input list already, so this sort is redundant but
        # harmless/defensive).
        sorted_results = sorted(results, key=lambda x: x[0])
        return "\n\n".join([res[1] for res in sorted_results])

    else:
        # Single image upload: OCR it directly, no page splitting needed.
        if status_callback:
            await status_callback("Processing image frame for OCR...")
        base64_data = base64.b64encode(file_bytes).decode("utf-8")
        text = await process_single_ocr_frame(client, base64_data)
        return text


async def ocr_with_progress(client: ollama.AsyncClient, file: UploadFile, result_holder: dict):
    """Run parse_file_with_ocr in the background and yield its progress live.

    This replaces the previous dead-end status_callback wiring: OCR now
    runs as a background asyncio task that pushes each progress message
    onto a queue via a real async callback, while this generator drains
    the queue and yields an SSE "status" event per message as it arrives.
    Per-page messages like "Running OCR on PDF page 2 of 5..." therefore
    actually reach the client, instead of being silently dropped.

    Async generators can't hand back a return value through normal
    iteration, so the final extracted text (or any exception raised inside
    the OCR task) is written into `result_holder` instead; the caller
    reads `result_holder["content"]` / `result_holder["error"]` once this
    generator is exhausted.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def status_callback(msg: str) -> None:
        await queue.put(msg)

    async def run_ocr() -> None:
        try:
            result_holder["content"] = await parse_file_with_ocr(client, file, status_callback)
        except Exception as exc:
            result_holder["error"] = exc
        finally:
            # Sentinel: tells the consumer loop below that no more status
            # messages are coming, regardless of success or failure.
            await queue.put(None)

    task = asyncio.create_task(run_ocr())
    while True:
        msg = await queue.get()
        if msg is None:
            break
        yield sse_event("status", msg)

    # Ensure the background task is fully finished (it always is by the
    # time the sentinel is put on the queue, but this also re-raises any
    # exception from a bug in run_ocr() itself, as opposed to one already
    # captured in result_holder["error"], which the caller checks
    # explicitly after this generator finishes).
    await task


async def generate_title(client: ollama.AsyncClient, summary: str) -> str:
    # Non-streaming call: one-shot title generation from the finished
    # summary text. strip('"') removes wrapping quotes the model sometimes
    # adds despite being told not to add extra formatting.
    prompt = TITLE_PROMPT_TEMPLATE.format(summary=summary)
    async with model_semaphore:
        response = await client.chat(
            model=LOCAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    return response["message"]["content"].strip().strip('"')


async def extract_keywords(client: ollama.AsyncClient, summary: str) -> str:
    # Same pattern as generate_title, but for the comma-separated keyword
    # string that gets stored alongside the summary.
    prompt = KEYWORD_PROMPT_TEMPLATE.format(summary=summary)
    async with model_semaphore:
        response = await client.chat(
            model=LOCAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    return response["message"]["content"].strip()


async def run_summarization_pipeline(
    client: ollama.AsyncClient,
    pool: asyncpg.Pool,
    extracted_content: str,
    source_type: str,
    source_identifier: str,
):
    """Shared tail of the pipeline: summarize -> title -> keywords -> persist.

    This is the single place implementing "stream a summary, then derive a
    title and keywords from it, then save the row" — both
    /api/summarize-url and /api/summarize call this once they've each
    produced `extracted_content` from their own source (crawled markdown
    or OCR'd text). Yields SSE event strings; raises on failure so the
    caller's own try/except can log it and emit one "error" event.
    """
    accumulated_summary = ""
    prompt = SUMMARY_PROMPT_TEMPLATE.format(content=extracted_content[:MAX_CONTENT_CHARS])

    async def run_stream():
        # Streams the summary token-by-token from Ollama, both forwarding
        # each token to the client as an SSE event and accumulating it
        # into `accumulated_summary` (closure variable) for use by the
        # later title/keyword/DB-save steps.
        nonlocal accumulated_summary
        async with model_semaphore:
            stream = await client.chat(
                model=LOCAL_MODEL,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                options={
                    "num_predict": 800,       # cap on generated tokens
                    "repeat_penalty": 1.3,    # discourage repetition
                    "repeat_last_n": 256,     # lookback window for the penalty
                },
            )
            async for chunk in stream:
                token = chunk["message"]["content"]
                accumulated_summary += token
                yield sse_event("summary_chunk", token)

    # Stage: stream the summary, bounded by an overall generation timeout
    # so a stuck model doesn't hang the request forever.
    yield sse_event("status", "Starting summarization pipeline...")
    try:
        async with asyncio.timeout(GENERATION_TIMEOUT_SECONDS):
            async for event in run_stream():
                yield event
    except TimeoutError:
        logger.warning("Generation timed out after %ss", GENERATION_TIMEOUT_SECONDS)
        yield sse_event("summary_chunk", "\n\n[Response truncated: generation took too long.]")

    # Stage: derive a title and keywords from the completed summary (not
    # streamed — short outputs, returned in one shot).
    yield sse_event("status", "Generating title...")
    title = await generate_title(client, accumulated_summary)
    yield sse_event("title", title)

    yield sse_event("status", "Extracting keywords...")
    keywords = await extract_keywords(client, accumulated_summary)
    yield sse_event("keywords", keywords)

    # Stage: persist the result for later retrieval via /api/summaries and
    # /api/summaries/{id}.
    yield sse_event("status", "Saving results to database...")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO summaries (source_type, source_identifier, title, summary, keywords) VALUES ($1, $2, $3, $4, $5)",
            source_type, source_identifier, title, accumulated_summary, keywords
        )
    yield sse_event("status", "Completed successfully!")


# ==========================================================================
# ROUTES
# ==========================================================================

# @app.post("/api/summarize-url")
# async def summarize_url(data: ScrapeRequest):
#     # URL-only summarization endpoint. All validation that can fail with a
#     # proper HTTP status happens here, before StreamingResponse begins —
#     # once the SSE stream starts, the 200 status is already committed and
#     # failures can only be reported as "error" events.
#     await validate_url_or_raise(data.url)

#     async def event_generator():
#         client: ollama.AsyncClient = app.state.ollama_client
#         pool: asyncpg.Pool = app.state.db_pool
#         try:
#             # Stage: crawl the page into markdown, then hand off to the
#             # shared summarize -> title -> keywords -> save pipeline.
#             yield sse_event("status", "Crawling URL content...")
#             extracted_content = await crawl_url(data.url)

#             async for event in run_summarization_pipeline(
#                 client, pool, extracted_content, source_type="url", source_identifier=data.url
#             ):
#                 yield event

#         except Exception:
#             # Catch-all: log the full traceback server-side but only leak a
#             # generic error message to the client over SSE (avoids
#             # exposing internals like DB errors or stack traces).
#             logger.exception("Summarization pipeline failed for url=%s", data.url)
#             yield sse_event("error", "Something went wrong while generating the summary.")
#         finally:
#             # Always terminate the stream, success or failure, so the
#             # client's SSE reader knows to stop listening.
#             yield SSE_DONE

#     return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/summarize")
async def summarize_content(
    url: str | None = Form(None),
    file: UploadFile | None = File(None)
):
    # General-purpose endpoint: accepts either a URL (multipart form field)
    # or a file upload (image/PDF), and runs the same summarize -> title ->
    # keywords -> save pipeline regardless of source, via
    # run_summarization_pipeline(). All validation happens up front, in
    # this synchronous section, for the same reason as summarize_url above:
    # a bad request should get a clean 4xx, not just an "error" SSE event.
    if not url and not file:
        raise HTTPException(status_code=400, detail="Provide either a URL or a file upload.")

    if url:
        source_type = "url"
        source_identifier = url
        await validate_url_or_raise(url)
    else:
        # `if not url and not file: raise` above guarantees file is set
        # here, but that's a fact about two *different* variables, so
        # Pyright can't narrow `file` just from `url` being falsy. This
        # assert makes the guarantee explicit so the type checker (and
        # anyone reading this later) can see file is no longer Optional.
        assert file is not None
        source_type = "file"
        # UploadFile.filename is itself typed as `str | None` (a client
        # can omit it), so fall back to a placeholder rather than ever
        # storing None as the source_identifier.
        source_identifier = file.filename or "uploaded-file"
        if not file.content_type or not file.content_type.startswith(("image/", "application/pdf")):
            raise HTTPException(status_code=400, detail="Invalid file type. Upload an image or PDF.")

    async def event_generator():
        client: ollama.AsyncClient = app.state.ollama_client
        pool: asyncpg.Pool = app.state.db_pool
        try:
            if url:
                # --- URL branch: identical to summarize_url's extraction
                # step, sharing crawl_url() and the same validated `url`.
                yield sse_event("status", "Crawling URL content...")
                print("before Crawling")
                extracted_content = await crawl_url(url)
                print("after Crawling")

            else:
                # --- File branch: OCR the upload with live progress. Each
                # per-page/per-frame message from ocr_with_progress is
                # forwarded to the client as it happens.
                # Narrowing `file` in the outer function's else-branch
                # doesn't extend into this nested closure, so it has to be
                # re-asserted here too (see the comment above for why).
                assert file is not None
                yield sse_event("status", "Reading uploaded file frames...")
                result_holder: dict = {}
                async for event in ocr_with_progress(client, file, result_holder):
                    yield event

                if "error" in result_holder:
                    raise result_holder["error"]

                extracted_content = result_holder.get("content", "")
                yield sse_event("status", "OCR parsing completed successfully.")

                if not extracted_content.strip():
                    raise HTTPException(status_code=400, detail="OCR could not extract any text from the file.")

            # From here on both branches share the same summarize -> title
            # -> keywords -> save logic.
            async for event in run_summarization_pipeline(
                client, pool, extracted_content, source_type=source_type, source_identifier=source_identifier
            ):
                yield event

        except Exception:
            logger.exception("Summarization pipeline failed for source=%s", source_identifier)
            yield sse_event("error", "Something went wrong while generating the summary.")
        finally:
            yield SSE_DONE

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/health")
async def health():
    # Trivial liveness probe for container orchestration / uptime checks.
    return {"status": "ok"}


@app.get("/api/summaries")
async def get_summaries():
    # List view: returns lightweight metadata for every stored summary (no
    # `summary`/`keywords` body text) ordered newest-first, for a
    # history/sidebar UI. Returns an empty list with 200 when there are no
    # rows yet — "nothing stored" is a normal, valid state for a list
    # endpoint, not an error condition.
    pool: asyncpg.Pool = app.state.db_pool
    
    print("get_summaries")

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, source_type, source_identifier as url, created_at
            FROM summaries
            ORDER BY created_at DESC
        """)
    print("get_summaries rows")

    return [
        {
            "id": row["id"],
            "title": row["title"],
            "source_type": row["source_type"],
            "url": row["url"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


@app.get("/api/summaries/{summary_id}")
async def get_summary(summary_id: int):
    # Detail view: full record including summary text and keywords for a
    # single stored entry, used when the user clicks into a past result.
    # Unlike the list endpoint, a missing specific id genuinely is a 404.
    pool: asyncpg.Pool = app.state.db_pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, title, source_type, source_identifier as url, summary, keywords, created_at
            FROM summaries
            WHERE id = $1
        """, summary_id)

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Summary not found"
        )

    return {
        "id": row["id"],
        "title": row["title"],
        "source_type": row["source_type"],
        "url": row["url"],
        "summary": row["summary"],
        "keywords": row["keywords"],
        "created_at": row["created_at"].isoformat(),
    }


if __name__ == "__main__":
    # Convenience entry point for `python main.py` during local dev; in
    # production this would typically be run via `uvicorn main:app` from
    # a process manager instead.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)