// import { Button } from "@/components/ui/button"
import { useCallback, useEffect, useRef, useState } from "react"
// import Markdown from "react-markdown"
import { cn } from "./lib/utils"
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable"
import type { PanelImperativeHandle } from "react-resizable-panels"

import {
  EllipsisVertical,
  PanelLeftClose,
  Settings,
  //   Sparkles,
  Star,
  StarPlus,
} from "lucide-react"
import { Outlet, useLocation, useNavigate } from "react-router-dom"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

// type SSEEvent = {
//   type: "summary_chunk" | "title" | "error"
//   content: string
// }

type SummaryItem = {
  id: number
  title: string
  url: string
  created_at: string
}

// Shape handed down to child routes via <Outlet context={...} />. Any route
// rendered inside SidebarLayout (SummarizerLayout, SummaryPage, ...) can pull
// this with useOutletContext<SidebarOutletContext>() to ask the sidebar to
// refresh its history list — e.g. right after a new summary finishes saving.
export type SidebarOutletContext = {
  refreshTitles: () => void
}

// Sidebar sizing, in percent of the panel group's width. Kept as constants
// so the collapse/expand logic and the JSX below agree on the same numbers.
const SIDEBAR_DEFAULT_SIZE = 20
const SIDEBAR_MIN_SIZE = 15
const SIDEBAR_MAX_SIZE = 35
const SIDEBAR_COLLAPSED_SIZE = 4

const SidebarLayout = () => {
  const [titles, setTitles] = useState<SummaryItem[]>([])
  const [sidebarVisibility, setSidebarVisibility] = useState(true)
  const [logoSideBar, setLogoSideBar] = useState(false)

  // Handle to the sidebar's ResizablePanel so the collapse/expand button can
  // drive it imperatively (react-resizable-panels doesn't take a controlled
  // size prop — collapse()/expand() are the supported way to do this).
  // NOTE: this version of react-resizable-panels wires the imperative
  // handle through a `panelRef` prop on <ResizablePanel>, not the standard
  // `ref` prop — see the JSX below.
  const sidebarPanelRef = useRef<PanelImperativeHandle>(null)

  const navigate = useNavigate()

  // react-router gives every history entry a fresh `key`, even when the
  // destination pathname is identical to the current one — navigate("/")
  // while already on "/" still produces a new location.key. Keying the
  // Outlet on it forces the routed component (SummarizerLayout) to fully
  // unmount/remount on every "New chat" click, root route or not, so its
  // own useState() calls re-initialize and reset everything on their own.
  // This is what handleNewChat below relies on instead of reaching into
  // SummarizerLayout's state directly, which isn't possible from up here —
  // outlet context only flows parent → child, never the other way.
  const location = useLocation()

  // Pure fetch, no state update — kept separate from the setState call so
  // the mount effect below can guard that call with an `ignore` flag
  // instead of invoking a state-setting function directly in the effect
  // body (the pattern react.dev's own data-fetching guide recommends, and
  // what the "set state in effect" lint rule is checking for).
  const fetchTitles = useCallback(async (): Promise<SummaryItem[] | null> => {
    try {
      const response = await fetch(`${API_URL}/api/summaries`)

      if (!response.ok) {
        throw new Error(`Failed to fetch summaries (${response.status})`)
      }

      return (await response.json()) as SummaryItem[]
    } catch (err) {
      console.error("Failed to fetch titles:", err)
      return null
    }
  }, [])

  // Exposed to child routes via outlet context so a page like
  // SummarizerLayout can ask for a refresh outside of any effect (e.g.
  // right after an SSE stream finishes) — a plain event-driven call, not
  // an effect, so there's nothing for that lint rule to flag here.
  const refreshTitles = useCallback(() => {
    fetchTitles().then((data) => {
      if (data) setTitles(data)
    })
  }, [fetchTitles])

  const deleteSummary = useCallback(
    async (id: number) => {
      try {
        const response = await fetch(`${API_URL}/api/summaries/${id}`, {
          method: "DELETE",
        })

        if (!response.ok) {
          throw new Error(`Failed to delete summary (${response.status})`)
        }

        // Optimistically drop it from local state instead of refetching
        setTitles((prev) => prev.filter((item) => item.id !== id))

        // If the deleted summary is the one currently open, bounce back home
        if (location.pathname === `/summary/${id}`) {
          navigate("/")
        }
      } catch (err) {
        console.error("Failed to delete summary:", err)
      }
    },
    [location.pathname, navigate]
  )

  // Initial load only — live updates after this come from child routes
  // calling refreshTitles() via outlet context, not from this effect
  // re-firing on some dependency change.
  useEffect(() => {
    let ignore = false

    fetchTitles().then((data) => {
      if (!ignore && data) setTitles(data)
    })

    return () => {
      ignore = true
    }
  }, [fetchTitles])

  // Just navigates — the actual reset of SummarizerLayout's url/summary/
  // error/statuses/searchResults/loading state happens for free because
  // the Outlet below remounts it on every navigation (see the `location`
  // comment above). No state to clear here; there never was any in scope.
  const handleNewChat = () => {
    navigate("/")
  }

  // Drives the ResizablePanel's collapse state. This version of
  // react-resizable-panels has no onCollapse/onExpand props, so
  // sidebarVisibility isn't set here directly — it's synced from the
  // panel's onResize handler below (via isCollapsed()), which fires both
  // for this imperative call and for a manual drag-to-collapse.
  const toggleSidebar = (nextVisible: boolean) => {
    const panel = sidebarPanelRef.current
    if (!panel) return
    if (nextVisible) {
      panel.expand()
    } else {
      panel.collapse()
    }
  }

  // Single source of truth for sidebarVisibility: read straight off the
  // panel's own imperative state after any resize (drag OR programmatic).
  const syncSidebarVisibility = () => {
    const panel = sidebarPanelRef.current
    if (panel) setSidebarVisibility(!panel.isCollapsed())
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      <ResizablePanelGroup orientation="horizontal" className="flex-1">
        <ResizablePanel
          panelRef={sidebarPanelRef}
          defaultSize={`${SIDEBAR_DEFAULT_SIZE}%`}
          minSize={`${SIDEBAR_MIN_SIZE}%`}
          maxSize={`${SIDEBAR_MAX_SIZE}%`}
          collapsible
          collapsedSize={`${SIDEBAR_COLLAPSED_SIZE}%`}
          onResize={syncSidebarVisibility}
          className="flex flex-col overflow-hidden bg-accent/20 p-4 shadow-2xs"
        >
          <div className="relative mt-2 flex h-1/4 items-center justify-between">
            <div className="flex flex-col">
              <div
                className={cn(
                  "absolute top-0 font-bold whitespace-nowrap transition-opacity duration-200",
                  sidebarVisibility
                    ? "opacity-100"
                    : "pointer-events-none hidden opacity-0"
                )}
              >
                Deep Search
              </div>

              {sidebarVisibility ? (
                <PanelLeftClose
                  className="absolute top-0 right-[0.48rem] shrink-0 cursor-pointer hover:scale-110"
                  onClick={() => toggleSidebar(false)}
                />
              ) : (
                <div
                  className="absolute top-0 right-1 mx-auto flex cursor-pointer items-center justify-center"
                  onMouseEnter={() => setLogoSideBar(true)}
                  onMouseLeave={() => setLogoSideBar(false)}
                  onClick={() => toggleSidebar(true)}
                >
                  {logoSideBar ? (
                    <PanelLeftClose className="hover:scale-110" />
                  ) : (
                    <Star />
                  )}
                </div>
              )}
              <div
                className={cn(
                  "absolute top-15 flex w-full justify-between truncate rounded-md pt-2 pb-2 shadow-md transition-all duration-200 ease-in-out hover:bg-primary/20 dark:hover:bg-primary",
                  sidebarVisibility ? "px-2" : "px-1"
                )}
                onClick={handleNewChat}
              >
                {sidebarVisibility && (
                  <button
                    className={cn(
                      "font-semibold",
                      sidebarVisibility
                        ? "opacity-100"
                        : "pointer-events-none hidden opacity-0"
                    )}
                  >
                    New chat
                  </button>
                )}
                <StarPlus
                  className={cn(
                    sidebarVisibility
                      ? "right-0 hover:scale-110"
                      : "right-2 hover:scale-110"
                  )}
                />
              </div>
            </div>
          </div>

          <div
            className={cn(
              "truncate transition-all duration-200 ease-in-out",
              sidebarVisibility
                ? "h-px w-full bg-gray-200 dark:bg-white"
                : "pointer-events-none hidden opacity-0"
            )}
          ></div>

          <h2
            className={cn(
              "my-4 font-sans text-lg font-bold",
              sidebarVisibility
                ? "opacity-100"
                : "pointer-events-none hidden opacity-0"
            )}
          >
            History
          </h2>

          <div
            className={cn(
              "truncate transition-all duration-200 ease-in-out",
              sidebarVisibility
                ? "h-px w-full bg-gray-200 dark:bg-white"
                : "pointer-events-none hidden opacity-0"
            )}
          ></div>

          <div
            className={cn(
              "mt-6 h-1/2 flex-1 scroll-fade scrollbar-thin scrollbar-thumb-accent overflow-y-auto transition-opacity duration-200",
              sidebarVisibility
                ? "opacity-100"
                : "pointer-events-none opacity-0"
            )}
          >
            <div className="flex flex-col gap-2">
              {titles.map((item) => (
                <button
                  key={item.id}
                  className="truncate rounded-md p-2 text-left hover:bg-primary/20 dark:hover:bg-primary"
                  onClick={() => navigate(`/summary/${item.id}`)}
                >
                  <div className="flex min-w-0 items-center justify-between gap-4">
                    <span className="min-w-0 truncate">
                      {item.title.length > 25
                        ? item.title.slice(0, 25) + "..."
                        : item.title}
                    </span>
                    <EllipsisVertical
                      onClick={(e) => {
                        e.stopPropagation()
                        deleteSummary(item.id)
                      }}
                      className="h-4 w-4 shrink-0 transition-all duration-300 ease-in-out hover:scale-110 hover:rotate-90"
                    />
                  </div>
                </button>
              ))}
            </div>
          </div>
          <div className="flex h-16 items-center justify-between truncate pt-5">
            <div
              className={cn(
                "ml-2 font-semibold",
                sidebarVisibility
                  ? "opacity-100"
                  : "pointer-events-none hidden opacity-0"
              )}
            >
              userName
            </div>
            <Settings
              className={cn(
                "shrink-0",
                sidebarVisibility
                  ? "right-0 mr-2 hover:scale-110"
                  : "right-2 hover:scale-110"
              )}
            />
          </div>
        </ResizablePanel>

        <ResizableHandle withHandle />

        <ResizablePanel defaultSize={`${100 - SIDEBAR_DEFAULT_SIZE}%`}>
          <main className="h-full min-w-0 overflow-y-auto">
            <Outlet
              key={location.key}
              context={{ refreshTitles } satisfies SidebarOutletContext}
            />
          </main>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  )
}

export default SidebarLayout
