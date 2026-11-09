import { useEffect, useState } from "react"
import { useParams } from "react-router-dom"
import Markdown from "react-markdown"
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "./components/ui/resizable"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

type SearchResultItem = {
  title: string
  url: string
  snippet: string
  engine: string
  engines?: string[]
}

type Summary = {
  id: number
  title: string
  url: string
  summary: string
  created_at: string
  search_results: SearchResultItem[]
}

export default function SummaryPage() {
  const { id } = useParams<{ id: string }>()

  const [summary, setSummary] = useState<Summary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!id) return

    const fetchSummary = async () => {
      try {
        setLoading(true)
        setError("")

        const response = await fetch(`${API_URL}/api/summaries/${id}`)

        if (!response.ok) {
          throw new Error("Summary not found")
        }

        const data: Summary = await response.json()
        setSummary(data)
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load summary")
      } finally {
        setLoading(false)
      }
    }

    fetchSummary()
  }, [id])

  if (loading) {
    return <div className="p-6">Loading summary...</div>
  }

  if (error) {
    return <div className="p-6 text-red-500">{error}</div>
  }

  if (!summary) return null

  return (
    <ResizablePanelGroup orientation="horizontal" className="h-full">
      <ResizablePanel
        defaultSize="65%"
        minSize="40%"
        className="flex scroll-fade scrollbar-thin scrollbar-thumb-accent flex-col items-start justify-baseline overflow-y-auto py-6 pr-6 pl-8"
      >
        <a className="cursor-pointer text-sm text-gray-400" href={summary.url}>
          {summary.url}
        </a>
        <h1 className="mt-12 mb-6 text-center text-3xl font-bold">
          {summary.title}
        </h1>

        <div className="prose dark:prose-invert max-w-none rounded-sm bg-accent p-6">
          <Markdown>{summary.summary}</Markdown>
        </div>
      </ResizablePanel>
      <ResizableHandle withHandle className="ml-2" />

      <ResizablePanel
        defaultSize="35%"
        minSize="20%"
        maxSize="50%"
        className="mr-2 scroll-fade scrollbar-thin scrollbar-thumb-accent overflow-y-auto"
      >
        <div className="flex flex-col gap-2 p-2">
          {summary.search_results.map((item, i) => (
            <a
              key={i}
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="group flex flex-col gap-1 rounded-lg border border-border bg-card p-3 text-card-foreground shadow-sm transition-colors hover:bg-accent/50"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="line-clamp-1 text-[14px] font-medium text-foreground group-hover:underline">
                  {item.title}
                </span>
                <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[12px] text-muted-foreground uppercase">
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
    </ResizablePanelGroup>
  )
}
