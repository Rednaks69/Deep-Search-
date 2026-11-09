import { useRef, useState } from "react"
import { useOutletContext } from "react-router-dom"
import Markdown from "react-markdown"
import { Button } from "@/components/ui/button"
import { cn } from "./lib/utils"
import { Paperclip, Sparkles, X } from "lucide-react"

import { useTheme } from "@/components/theme-provider"
import type { SidebarOutletContext } from "./SideBarLayout"
import {
  GiAbstract010,
  GiAbstract029,
  GiAbstract038,
  GiAbstract051,
  GiAbstract075,
  GiAbstract076,
} from "react-icons/gi"
import { BreedingRhombusSpinner } from "react-epic-spinners"
import { Separator } from "@/components/ui/separator"
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable"
import { OctajuanShader } from "./OctajuanShader"
import TrueFocus from "./components/TrueFocus.jsx"
import LiquidOrb from "./components/LiquidOrb.js"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

// Mirrors every event type the backend's SSE stream can send (see
// sse_event() calls in main.py) — "status" and "keywords" were previously
// missing here, so those events were silently ignored by the parser below.
type SSEEvent = {
  type:
    | "status"
    | "summary_chunk"
    | "title"
    | "keywords"
    | "error"
    | "search_results"
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  content: any
}
type SearchResultItem = {
  title: string
  url: string
  snippet: string
  engine: string
  engines?: string[]
}

export default function SummarizerLayout() {
  // const [theme, setTheme] = useState<"light" | "dark">(
  //   document.documentElement.classList.contains("dark") ? "dark" : "light"
  // )

  const { theme } = useTheme()

  // Sidebar's refetch function, handed down through the route tree so this
  // page can ask the sidebar to pick up a newly saved summary immediately,
  // without the user having to reload.
  const { refreshTitles } = useOutletContext<SidebarOutletContext>()

  const [url, setUrl] = useState("")
  const [file, setFile] = useState<File | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // What the user actually submitted, frozen at submit time so it can still
  // be displayed at the top of the chat after `url`/`file` are cleared for
  // the next request.
  const [submittedSource, setSubmittedSource] = useState("")
  const [submittedIsFile, setSubmittedIsFile] = useState(false)

  // Running log of "status" SSE events (e.g. "Crawling URL content...",
  // "Generating title...") — this is the "states" feed shown between the
  // source line and the summary.
  const [statuses, setStatuses] = useState<string[]>([])

  const [title, setTitle] = useState("")
  const [summary, setSummary] = useState("")
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)
  const [summaryClicked, setSummaryClicked] = useState(false)
  const [searchResults, setSearchResults] = useState<SearchResultItem[]>([])

  // useEffect(() => {
  //   const handleKeyDown = (e: KeyboardEvent) => {
  //     // Don't toggle while typing in the URL input
  //     if (
  //       e.target instanceof HTMLInputElement ||
  //       e.target instanceof HTMLTextAreaElement
  //     ) {
  //       return
  //     }

  //     if (e.key.toLowerCase() === "d") {
  //       const nextTheme = theme === "dark" ? "light" : "dark"

  //       document.documentElement.classList.toggle("dark", nextTheme === "dark")

  //       setTheme(nextTheme)
  //     }
  //   }

  //   window.addEventListener("keydown", handleKeyDown)

  //   return () => {
  //     window.removeEventListener("keydown", handleKeyDown)
  //   }
  // }, [theme])

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files?.[0] ?? null
    setFile(selected)
    // A file and a URL are mutually exclusive on the backend (it accepts
    // one or the other) — clear whichever the user isn't using right now.
    if (selected) setUrl("")
  }

  const handleSummarize = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!url && !file) return

    const sourceLabel = file ? file.name : url

    setLoading(true)
    setSummary("")
    setTitle("")
    setStatuses([])
    setError("")
    setSummaryClicked(true)
    setSubmittedSource(sourceLabel)
    setSubmittedIsFile(Boolean(file))
    setSearchResults([])

    try {
      // The backend's general-purpose endpoint accepts either field as
      // multipart form data — matches /api/summarize on the Python side,
      // which is what makes file uploads possible here (the old
      // /api/summarize-url endpoint only ever took a JSON { url }).
      const formData = new FormData()
      if (file) {
        formData.append("file", file)
      } else {
        formData.append("url", url)
      }

      const response = await fetch(`${API_URL}/api/summarize`, {
        method: "POST",
        body: formData,
      })

      if (!response.ok) {
        const errorBody = await response.json().catch(() => null)
        throw new Error(
          errorBody?.detail ?? `Request failed (${response.status})`
        )
      }
      if (!response.body) throw new Error("No response body")

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""
      let accumulatedSummary = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        const events = buffer.split("\n\n")
        buffer = events.pop() ?? ""

        for (const rawEvent of events) {
          const dataLine = rawEvent
            .split("\n")
            .find((line) => line.startsWith("data: "))
          if (!dataLine) continue

          const payload = dataLine.slice("data: ".length).trim()
          if (payload === "[DONE]") continue

          let parsed: SSEEvent
          try {
            parsed = JSON.parse(payload)
          } catch {
            continue
          }

          if (parsed.type === "status") {
            setStatuses((prev) => [...prev, parsed.content])
          } else if (parsed.type === "summary_chunk") {
            accumulatedSummary += parsed.content
            setSummary(accumulatedSummary)
          } else if (parsed.type === "title") {
            setTitle(parsed.content)
          } else if (parsed.type === "error") {
            setError(parsed.content)
          } else if (parsed.type === "search_results") {
            // Fix: Handle content whether it's already an object/array or a JSON string
            try {
              let incoming = parsed.content
              if (typeof incoming === "string") {
                incoming = JSON.parse(incoming)
              }
              if (Array.isArray(incoming)) {
                setSearchResults((prev) => [...prev, ...incoming])
              } else if (incoming) {
                setSearchResults((prev) => [...prev, incoming])
              }
            } catch (err) {
              console.warn(
                "Could not process search_results payload:",
                parsed.content,
                err
              )
            }
          }

          // "keywords" events aren't surfaced in this view yet.
        }
      }

      // Pipeline finished (or errored) and, on success, saved a row — pull
      // the sidebar's history list up to date right away.
      refreshTitles()
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to process request."
      )
      console.error("Error", err)
    } finally {
      setLoading(false)
      setUrl("")
      setFile(null)
    }
  }

  const hasSearchResults = searchResults.length > 0

  return (
    <div className="relative flex h-full w-full flex-1 overflow-hidden bg-background">
      {/* Full-screen WebGPU background */}
      {!summaryClicked && (
        <div className="pointer-events-none absolute inset-0 z-0">
          <OctajuanShader
            theme={theme}
            background={{
              dark: "#090909",
              light: "#ffffff",
            }}
          />
        </div>
      )}
      {/* Foreground content */}
      <div
        className={cn(
          "relative z-10 flex flex-1 flex-col p-4",
          summaryClicked
            ? "justify-between overflow-hidden"
            : "items-center justify-center"
        )}
      >
        {summaryClicked && (
          // Chat content lives inside a Resizable split so the search
          // results feed (right) can be dragged wider/narrower against the
          // status/summary feed (left). Each side scrolls independently —
          // the parent above only clips (overflow-hidden), it no longer
          // scrolls the whole column.
          <div className="min-h-0 w-full flex-1 overflow-hidden">
            <ResizablePanelGroup orientation="horizontal" className="h-full">
              <ResizablePanel
                defaultSize={hasSearchResults ? "65%" : "100%"}
                minSize="40%"
                className="flex scroll-fade scrollbar-thin scrollbar-thumb-accent flex-col items-start justify-baseline overflow-y-auto"
              >
                {/* Source line: the URL or filename the user submitted, shown
                    immediately — before the pipeline has produced anything. */}
                <div className="flex w-full justify-end bg-accent/20 p-2 text-sm text-muted-foreground">
                  {submittedIsFile ? (
                    <span className="truncate">{submittedSource}</span>
                  ) : (
                    <a
                      href={submittedSource}
                      target="_blank"
                      rel="noreferrer"
                      className="truncate hover:underline"
                    >
                      {submittedSource}
                    </a>
                  )}
                </div>

                {/* Status feed: each "status" SSE event as it streams in. */}
                {statuses.length > 0 && (
                  <ul className="w-full bg-background px-2 pb-2 text-xs text-muted-foreground">
                    <div className="flex items-baseline gap-4">
                      {loading && (
                        <BreedingRhombusSpinner
                          color="oklch(64.6% 0.222 41.116)"
                          size={12}
                          className="ml-4 text-orange-600"
                        />
                      )}
                      <div className="my-2 flex flex-col p-2">
                        {statuses.map((status, i) => (
                          <li
                            key={i}
                            className={cn(
                              i === statuses.length - 1 &&
                                loading &&
                                "animate-pulse"
                            )}
                          >
                            {status === "Crawling URL content..." && (
                              <div className="my-0.5 flex items-center gap-2 space-x-2">
                                <GiAbstract010 />
                                {status}
                              </div>
                            )}

                            {status ===
                              "Starting summarization pipeline..." && (
                              <>
                                <Separator
                                  orientation="vertical"
                                  className="ml-[0.3rem] h-4 border border-muted-foreground/20"
                                />
                                <div className="my-0.5 flex items-center gap-2 space-x-2">
                                  <GiAbstract051 />
                                  {status}
                                </div>
                              </>
                            )}
                            {status === "Generating title..." && (
                              <>
                                <Separator
                                  orientation="vertical"
                                  className="ml-[0.3rem] h-4 border border-muted-foreground/20"
                                />
                                <div className="my-0.5 flex items-center gap-2 space-x-2">
                                  <GiAbstract038 />
                                  {status}
                                </div>
                              </>
                            )}
                            {status === "Extracting keywords..." && (
                              <>
                                <Separator
                                  orientation="vertical"
                                  className="ml-[0.3rem] h-4 border border-muted-foreground/20"
                                />
                                <div className="my-0.5 flex items-center gap-2 space-x-2">
                                  <GiAbstract029 />
                                  {status}
                                </div>
                              </>
                            )}
                            {status === "Saving results to database..." && (
                              <>
                                <Separator
                                  orientation="vertical"
                                  className="ml-[0.3rem] h-4 border border-muted-foreground/20"
                                />
                                <div className="my-0.5 flex items-center gap-2 space-x-2">
                                  <GiAbstract075 />
                                  {status}
                                </div>
                              </>
                            )}
                            {status === "Completed successfully!" && (
                              <>
                                <Separator
                                  orientation="vertical"
                                  className="ml-[0.3rem] h-4 border border-muted-foreground/20"
                                />
                                <div className="my-0.5 flex items-center gap-2 space-x-2">
                                  <GiAbstract076 />
                                  {status}
                                </div>
                              </>
                            )}

                            {/* {status} */}
                          </li>
                        ))}
                      </div>
                    </div>
                  </ul>
                )}

                {summary && (
                  <div className="w-full bg-background p-2">
                    {title && (
                      <h2 className="bg-background px-4 pt-8 pb-2 text-center font-bold text-wrap text-foreground">
                        {title}
                      </h2>
                    )}

                    <div className="mt-4 items-start bg-accent/80 p-4 text-foreground">
                      <Markdown>{summary}</Markdown>
                    </div>
                  </div>
                )}
              </ResizablePanel>

              {/* Search results panel: only takes up space (and gets a
                  drag handle) once results have actually started coming
                  in — an empty resizable panel next to nothing else would
                  just be dead space. */}
              {hasSearchResults && (
                <>
                  <ResizableHandle withHandle className="ml-2" />
                  <ResizablePanel
                    defaultSize="35%"
                    minSize="20%"
                    maxSize="50%"
                    className="scroll-fade scrollbar-thin scrollbar-thumb-accent overflow-y-auto"
                  >
                    {/* 4. Render Search Results Card UI Stream */}
                    <div className="flex flex-col gap-2 p-2">
                      {searchResults.map((item, i) => (
                        <a
                          key={i}
                          href={item.url}
                          target="_blank"
                          rel="noreferrer"
                          className="group flex flex-col gap-1 rounded-lg border border-border bg-card p-3 text-card-foreground shadow-sm transition-colors hover:bg-accent/50"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="line-clamp-1 text-xs font-medium text-foreground group-hover:underline">
                              {item.title}
                            </span>
                            <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground uppercase">
                              {item.engine}
                            </span>
                          </div>
                          <p className="line-clamp-2 text-[11px] text-muted-foreground">
                            {item.snippet}
                          </p>
                          <span className="truncate font-mono text-[10px] text-blue-500/80 dark:text-white/80">
                            {item.url}
                          </span>
                        </a>
                      ))}
                    </div>
                  </ResizablePanel>
                </>
              )}
            </ResizablePanelGroup>
          </div>
        )}

        <div
          className={cn(
            "flex flex-col items-center",
            summaryClicked ? "hidden" : "w-1/2 rounded-xl p-4"
          )}
        >
          {!summaryClicked && (
            <div className="pointer-events-none">
              <TrueFocus
                sentence="Deep Search"
                manualMode={false}
                blurAmount={5}
                borderColor="#a800b7"
                animationDuration={1}
                pauseBetweenAnimations={1}
              />
            </div>
          )}
          <form
            onSubmit={handleSummarize}
            className="mt-4 flex w-full max-w-2xl items-center gap-2 rounded-2xl bg-background px-2 py-4"
          >
            <input
              type="file"
              ref={fileInputRef}
              accept="image/*,application/pdf"
              onChange={handleFileChange}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={loading}
              title="Attach an image or PDF instead of a URL"
              className="shrink-0 rounded-md p-2 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
            >
              <Paperclip className="h-4 w-4" />
            </button>

            {file ? (
              <div className="flex flex-1 items-center justify-between rounded-md border border-input bg-background p-2 text-sm font-medium text-foreground">
                <span className="truncate">{file.name}</span>
                <button
                  type="button"
                  onClick={() => setFile(null)}
                  disabled={loading}
                  className="ml-2 shrink-0 text-muted-foreground hover:text-foreground disabled:opacity-50"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            ) : (
              <input
                type="url"
                placeholder="https://example.com"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                required={!file}
                className="flex-1 rounded-md border border-input bg-background p-2 text-sm font-medium text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/30"
              />
            )}

            <Button
              type="submit"
              disabled={loading || (!url && !file)}
              style={{
                padding: "20px 16px",
                cursor: "pointer",
                background: "transparent",
              }}
            >
              {loading ? (
                <span className="flex items-center gap-1">
                  <LiquidOrb state="thinking" size={18} />
                  Streaming...
                </span>
              ) : (
                <span className="flex items-center gap-1">
                  <LiquidOrb state="thinking" size={52} />
                  <span className="text-muted-foreground">Search</span>
                </span>
              )}
            </Button>
          </form>

          {error && (
            <div
              style={{
                background: "#fdecea",
                color: "#611a15",
                padding: "12px 16px",
                borderRadius: "6px",
                marginTop: "12px",
              }}
              className="w-full max-w-2xl"
            >
              {error}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
