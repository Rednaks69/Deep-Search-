import asyncio
import base64
import ipaddress
import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import re 

import asyncpg
import httpx
import ollama
from crawl4ai import AsyncWebCrawler
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pdf2image import convert_from_bytes
from pydantic import BaseModel, field_validator


LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:3b")
OCR_MODEL = os.getenv("OCR_MODEL", "deepseek-ocr")

# SearXNG instance host base URL
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")

# Maximum number of search hits kept from SearXNG (SearXNG has no "limit" param,
# so we cap the results ourselves).
SEARXNG_MAX_RESULTS = int(os.getenv("SEARXNG_MAX_RESULTS", "10"))

MAX_CONTENT_CHARS = int(os.getenv("MAX_CONTENT_CHARS", "500000"))

CRAWL_TIMEOUT_SECONDS = int(os.getenv("CRAWL_TIMEOUT_SECONDS", "30"))

MAX_CONCURRENT_MODEL_CALLS = int(os.getenv("MAX_CONCURRENT_MODEL_CALLS", "1"))

ALLOWED_URL_SCHEMES = {"http", "https"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

GENERATION_TIMEOUT_SECONDS = int(os.getenv("GENERATION_TIMEOUT_SECONDS", "120"))

# Postgres connection string for the results store.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/deepsearch_db")


# ?----------------------------------------------------------------
# Matches markdown image syntax: ![alt text](url "optional title")
IMAGE_MARKDOWN_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
# ?----------------------------------------------------------------

# ?----------------------------------------------------------------
def extract_images_from_markdown(content: str) -> list[dict[str, str]]:
    """Pull every markdown image reference out of the raw crawled content,
    before it gets truncated to MAX_CONTENT_CHARS."""
    images = []
    seen_urls = set()
    for match in IMAGE_MARKDOWN_RE.finditer(content):
        alt, url = match.group(1).strip(), match.group(2).strip()
        # Skip tiny tracking pixels / spacer gifs a lot of sites embed
        if url in seen_urls:
            continue
        seen_urls.add(url)
        images.append({"alt": alt or "Image", "url": url})
    return images
# ?----------------------------------------------------------------


SUMMARY_PROMPT_TEMPLATE = (
    """
      <task-context>
      You are a helpful assistant that summarizes the provided content or search results.
      </task-context>
      <available-images>
      {images_block}
      </available-images>
      <rules>
      - Use quotes from the content to your output.
      - Use paragraphs in your output to separate content by including an empty line between paragraphs.
      - Use bullet points in your output.
      - do not use numbered lists in your output.
      - Use code blocks in your output if the content is code snippets and color them.
      - Not use the word "summary" or "Summary" or "SUMMARY" in your output.
      - break a line when there is new subtitle ### or ## or # in the content.
      - When relevant, reference an image from <available-images> using its exact markdown syntax near the text it illustrates.
      </rules>
      """
    " it's very important to highlight key takeaways:\n\n{content}"
)




TITLE_PROMPT_TEMPLATE = (
    "Based on the following summary, generate a short, punchy title. "
    "Return ONLY the title text with no extra commentary, prefixes, or markdown formatting.\n\n"
    "Summary:\n{summary}"
)

KEYWORD_PROMPT_TEMPLATE = (
    "Based on the following summary, extract 3 to 6 core comma-separated keywords. "
    "Return ONLY the comma-separated keywords with no extra commentary or markdown formatting.\n\n"
    "Summary:\n{summary}"
)


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


model_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MODEL_CALLS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared async Ollama client for the whole app's lifetime.
    app.state.ollama_client = ollama.AsyncClient()
    # Shared HTTPX async client for SearXNG queries
    app.state.httpx_client = httpx.AsyncClient(timeout=15.0)

    # Initialize PostgreSQL connection pool and ensure table exists.
    app.state.db_pool = await asyncpg.create_pool(DATABASE_URL)
    # Don't log the DSN: it contains the database password.
    logger.info("Connected to Postgres")

    # Idempotent schema bootstrap
    async with app.state.db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id SERIAL PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_identifier TEXT NOT NULL,
                title TEXT,
                summary TEXT,
                keywords TEXT,
                search_results JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # CREATE TABLE IF NOT EXISTS does not touch an existing table, so add the
        # new column for databases created by an earlier version of this file.
        await conn.execute(
            "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS search_results JSONB"
        )
        logger.info("Ensured summaries table exists")

    yield

    await app.state.httpx_client.aclose()
    await app.state.db_pool.close()


app = FastAPI(lifespan=lifespan)


frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
origins = [origin.strip() for origin in frontend_url.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)





async def resolve_and_check_host(hostname: str) -> None:
    try:
        loop = asyncio.get_event_loop()
        addrinfo = await loop.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host: {hostname}") from exc

    for _family, _, _, _, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"URL host resolves to a disallowed address ({ip_str})"
            )


async def validate_url_or_raise(url: str) -> None:
    hostname = urlparse(url).hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL format.")
    try:
        await resolve_and_check_host(hostname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ScrapeRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url_format(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ALLOWED_URL_SCHEMES:
            raise ValueError(f"URL scheme must be one of {ALLOWED_URL_SCHEMES}")
        if not parsed.hostname:
            raise ValueError("URL must include a hostname")
        if parsed.hostname.lower() in BLOCKED_HOSTS:
            raise ValueError("URL host is not allowed")
        return value


def sse_event(event_type: str, content: Any) -> str:
    # `content` can be a plain string (status, title, tokens...) or any
    # JSON-serializable value (e.g. the list of search results).
    return f"data: {json.dumps({'type': event_type, 'content': content})}\n\n"


SSE_DONE = "data: [DONE]\n\n"


async def search_searxng_content(
    client: httpx.AsyncClient, query: str
) -> list[dict[str, Any]]:
    """
    Search the web through the SearXNG instance and return the hits as a list of
    {title, url, snippet, engine, engines} dicts.

    In the pipeline, `query` is built from the keywords extracted from the summary.
    The list is streamed to the frontend as a "search_results" SSE event and stored
    in the database next to the summary.

    Raises HTTPException:
      - 404 if SearXNG returned no usable results
      - 502 if SearXNG is unreachable, errors out, or returns invalid JSON
    """
    endpoint = f"{SEARXNG_URL.rstrip('/')}/search"
    params = {
        "q": query,
        "format": "json",  # must be enabled under search.formats in SearXNG's settings.yml
        # No "engines" override: let SearXNG use its default engine set.
    }

    try:
        response = await client.get(endpoint, params=params)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # httpx.HTTPError covers network errors and non-2xx statuses;
        # ValueError covers a body that isn't valid JSON.
        logger.exception("SearXNG search query failed")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch search results from SearXNG: {exc}",
        ) from exc

    sources: list[dict[str, Any]] = []

    for item in data.get("results", []):
        snippet = (item.get("content") or "").strip()
        if not snippet:
            continue  # a hit without text is useless to display, so skip it

        sources.append({
            "title": item.get("title") or "Untitled",
            "url": item.get("url", ""),
            "snippet": snippet,
            "engine": item.get("engine", ""),
            "engines": item.get("engines", []),  # every engine that returned this hit
        })

        # Cap after filtering, so empty hits don't use up the quota
        if len(sources) >= SEARXNG_MAX_RESULTS:
            break

    if not sources:
        raise HTTPException(status_code=404, detail="No search results returned from SearXNG.")

    return sources


async def crawl_url(url: str) -> str:
    logger.info("CRAWL: starting URL=%s", url)

    try:
        async with AsyncWebCrawler() as crawler:
            logger.info("CRAWL: crawler initialized")

            result = await asyncio.wait_for(
                crawler.arun(url=url),
                timeout=CRAWL_TIMEOUT_SECONDS
            )

            logger.info("CRAWL: arun() returned")

    except asyncio.TimeoutError as exc:
        logger.error("CRAWL: TIMEOUT after %s seconds", CRAWL_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=504,
            detail="Crawl timed out."
        ) from exc

    except Exception:
        logger.exception("CRAWL: unexpected exception")
        raise

    if not result.success:
        raise HTTPException(status_code=400, detail="Crawl failed.")

    if not result.markdown:
        raise HTTPException(status_code=400, detail="No content found.")

    return result.markdown


async def process_single_ocr_frame(client: ollama.AsyncClient, base64_image: str) -> str:
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
    file_bytes = await file.read()

    if file.content_type == "application/pdf":
        if status_callback:
            await status_callback("Converting PDF pages to images...")
        images = convert_from_bytes(file_bytes)

        async def process_page(pil_img, page_num):
            buffered = BytesIO()
            pil_img.save(buffered, format="JPEG")
            b64_img = base64.b64encode(buffered.getvalue()).decode("utf-8")
            if status_callback:
                await status_callback(f"Running OCR on PDF page {page_num + 1} of {len(images)}...")
            text = await process_single_ocr_frame(client, b64_img)
            return page_num, text

        tasks = [process_page(img, i) for i, img in enumerate(images)]
        results = await asyncio.gather(*tasks)

        sorted_results = sorted(results, key=lambda x: x[0])
        return "\n\n".join([res[1] for res in sorted_results])

    else:
        if status_callback:
            await status_callback("Processing image frame for OCR...")
        base64_data = base64.b64encode(file_bytes).decode("utf-8")
        text = await process_single_ocr_frame(client, base64_data)
        return text


async def ocr_with_progress(client: ollama.AsyncClient, file: UploadFile, result_holder: dict):
    queue: asyncio.Queue = asyncio.Queue()

    async def status_callback(msg: str) -> None:
        await queue.put(msg)

    async def run_ocr() -> None:
        try:
            result_holder["content"] = await parse_file_with_ocr(client, file, status_callback)
        except Exception as exc:
            result_holder["error"] = exc
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_ocr())
    while True:
        msg = await queue.get()
        if msg is None:
            break
        yield sse_event("status", msg)

    await task


async def generate_title(client: ollama.AsyncClient, summary: str) -> str:
    prompt = TITLE_PROMPT_TEMPLATE.format(summary=summary)
    async with model_semaphore:
        response = await client.chat(
            model=LOCAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    return response["message"]["content"].strip().strip('"')


async def extract_keywords(client: ollama.AsyncClient, summary: str) -> str:
    prompt = KEYWORD_PROMPT_TEMPLATE.format(summary=summary)
    async with model_semaphore:
        response = await client.chat(
            model=LOCAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    return response["message"]["content"].strip()


async def run_summarization_pipeline(
    client: ollama.AsyncClient,
    httpx_client: httpx.AsyncClient,
    pool: asyncpg.Pool,
    extracted_content: str,
    source_type: str,
    source_identifier: str,
):
    """
    Pipeline (input is the text extracted from a URL or an image/PDF):

      1. Stream the summary.
      2. Start the title and the keywords generation at the same time.
      3. As soon as the keywords are ready, search them with SearXNG. The search
         runs while the title is still being generated.
      4. Save title + summary + keywords + search results in Postgres.

    SSE events, in order: status, summary_chunk..., keywords, title,
    search_results (plus search_error if the search failed), status.
    """
    accumulated_summary = ""
    # ? --------------------------------------------------------------
    # Extract images from the FULL content, before truncation drops them
    extracted_images = extract_images_from_markdown(extracted_content)
    images_block = "\n".join(f"![{img['alt']}]({img['url']})" for img in extracted_images) or "(none found)"
    # ? --------------------------------------------------------------
    
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
       images_block=images_block,
       content=extracted_content[:MAX_CONTENT_CHARS],
   )
    
    async def run_stream():
        nonlocal accumulated_summary
        async with model_semaphore:
            stream = await client.chat(
                model=LOCAL_MODEL,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                options={
                    "num_predict": 800,
                    "repeat_penalty": 1.3,
                    "repeat_last_n": 256,
                    "num_ctx": 16384,   # or whatever fits your VRAM + qwen2.5:3b's supported window
                },
            )
            async for chunk in stream:
                token = chunk["message"]["content"]
                accumulated_summary += token
                yield sse_event("summary_chunk", token)

    # ---- Step 1: stream the summary -------------------------------------------
    yield sse_event("status", "Starting summarization pipeline...")
    yield sse_event("images", extracted_images)
    try:
        async with asyncio.timeout(GENERATION_TIMEOUT_SECONDS):
            async for event in run_stream():
                yield event
    except TimeoutError:
        logger.warning("Generation timed out after %ss", GENERATION_TIMEOUT_SECONDS)
        yield sse_event("summary_chunk", "\n\n[Response truncated: generation took too long.]")

    if not accumulated_summary.strip():
        raise HTTPException(status_code=502, detail="The model returned an empty summary.")

    # ---- Step 2: title and keywords, started together --------------------------
    yield sse_event("status", "Generating title and keywords...")
    tasks: list[asyncio.Task] = []
    title_task = asyncio.create_task(generate_title(client, accumulated_summary))
    keywords_task = asyncio.create_task(extract_keywords(client, accumulated_summary))
    tasks.extend([title_task, keywords_task])

    search_results: list[dict[str, Any]] = []
    try:
        keywords = await keywords_task
        yield sse_event("keywords", keywords)

        # ---- Step 3: search the keywords while the title is still generating ----
        # The model returns "a, b, c"; SearXNG expects plain space-separated terms.
        search_query = " ".join(k.strip() for k in keywords.split(",") if k.strip())
        yield sse_event("status", f"Searching the web for: {search_query}")
        search_task = asyncio.create_task(search_searxng_content(httpx_client, search_query))
        tasks.append(search_task)

        title = await title_task
        yield sse_event("title", title)

        # The search is best-effort: if it fails we still keep the summary,
        # the title and the keywords, and just store an empty list of results.
        try:
            search_results = await search_task
        except Exception as exc:
            logger.warning("Web search failed for query=%r: %s", search_query, exc)
            detail = exc.detail if isinstance(exc, HTTPException) else "Web search failed."
            yield sse_event("search_error", detail)
        print("search_results:", search_results)
        yield sse_event("search_results", search_results)
        
    finally:
        # If the client disconnects or a step fails, don't leave tasks running.
        for task in tasks:
            if not task.done():
                task.cancel()

    # ---- Step 4: save everything ---------------------------------------------
    yield sse_event("status", "Saving results to database...")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO summaries
                (source_type, source_identifier, title, summary, keywords, search_results)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            source_type,
            source_identifier,
            title,
            accumulated_summary,
            keywords,
            json.dumps(search_results),
        )
    yield sse_event("status", "Completed successfully!")


@app.post("/api/summarize")
async def summarize_content(
    url: str | None = Form(None),
    file: UploadFile | None = File(None)
):
    if not url and not file:
        raise HTTPException(
            status_code=400, detail="Provide either a URL or a file upload."
        )

    if url:
        source_type = "url"
        source_identifier = url
        await validate_url_or_raise(url)
    else:
        assert file is not None
        source_type = "file"
        source_identifier = file.filename or "uploaded-file"
        if not file.content_type or not file.content_type.startswith(("image/", "application/pdf")):
            raise HTTPException(status_code=400, detail="Invalid file type. Upload an image or PDF.")

    async def event_generator():
        client: ollama.AsyncClient = app.state.ollama_client
        httpx_client: httpx.AsyncClient = app.state.httpx_client
        pool: asyncpg.Pool = app.state.db_pool
        try:
            if url:
                yield sse_event("status", "Crawling URL content...")
                extracted_content = await crawl_url(url)
            else:
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

            async for event in run_summarization_pipeline(
                client,
                httpx_client,
                pool,
                extracted_content,
                source_type=source_type,
                source_identifier=source_identifier,
            ):
                yield event

        except HTTPException as exc:
            # Surface the real message (e.g. "Crawl timed out.") instead of a generic one
            logger.warning("Pipeline failed for source=%s: %s", source_identifier, exc.detail)
            yield sse_event("error", exc.detail)
        except Exception:
            logger.exception("Summarization pipeline failed for source=%s", source_identifier)
            yield sse_event("error", "Something went wrong while generating the summary.")
        finally:
            yield SSE_DONE

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/summaries")
async def get_summaries():
    pool: asyncpg.Pool = app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, source_type, source_identifier as url, created_at
            FROM summaries
            ORDER BY created_at DESC
        """)

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
    pool: asyncpg.Pool = app.state.db_pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, title, source_type, source_identifier as url, summary, keywords,
                   search_results, created_at
            FROM summaries
            WHERE id = $1
        """, summary_id)

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Summary not found"
        )

    # asyncpg returns JSONB as a JSON string, so decode it (rows saved before
    # the search_results column existed have NULL there).
    raw_results = row["search_results"]
    search_results = json.loads(raw_results) if raw_results else []

    return {
        "id": row["id"],
        "title": row["title"],
        "source_type": row["source_type"],
        "url": row["url"],
        "summary": row["summary"],
        "keywords": row["keywords"],
        "search_results": search_results,
        "created_at": row["created_at"].isoformat(),
    }


@app.delete("/api/summaries/{summary_id}", status_code=204)
async def delete_summary(summary_id: int):
    pool: asyncpg.Pool = app.state.db_pool

    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM summaries WHERE id = $1",
            summary_id,
        )

    # asyncpg's execute() returns a string like "DELETE 1" or "DELETE 0"
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Summary not found")

    return None

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)