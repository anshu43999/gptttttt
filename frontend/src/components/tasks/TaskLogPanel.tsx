import { useEffect, useRef, useState, useCallback, useMemo } from "react"
import { X, ChevronDown, ChevronRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export interface LogEvent {
  id?: number
  timestamp?: string
  level?: string
  event_type?: string
  message?: string
}

interface SubTaskGroup {
  name: string
  events: LogEvent[]
  collapsed: boolean
}

export interface TaskLogPanelProps {
  taskId: string
  onClose?: () => void
}

const EVENT_SOURCE_PATH = (taskId: string) =>
  `/api/tasks/${taskId}/logs/stream`

function groupEvents(events: LogEvent[]): SubTaskGroup[] {
  const groups: SubTaskGroup[] = []
  let current: SubTaskGroup | null = null

  for (const event of events) {
    const subTask = event.event_type || ""
    if (subTask && (!current || current.name !== subTask)) {
      current = { name: subTask, events: [], collapsed: false }
      groups.push(current)
    }
    if (current) {
      current.events.push(event)
    } else {
      // orphan event before any sub-task grouping
      current = { name: "general", events: [], collapsed: false }
      groups.push(current)
      current.events.push(event)
    }
  }

  return groups
}

function fmtTime(ts?: string) {
  if (!ts) return ""
  try {
    const d = new Date(ts)
    return d.toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
  } catch {
    return ts.slice(0, 8)
  }
}

function levelColor(level?: string) {
  switch (level?.toLowerCase()) {
    case "error":
      return "text-red-400"
    case "warn":
    case "warning":
      return "text-amber-400"
    case "info":
      return "text-blue-400"
    case "success":
      return "text-emerald-400"
    default:
      return "text-zinc-400"
  }
}

export function TaskLogPanel({ taskId, onClose }: TaskLogPanelProps) {
  const [events, setEvents] = useState<LogEvent[]>([])
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set())
  const [streamStatus, setStreamStatus] = useState<'connecting' | 'live' | 'closed' | 'error'>('connecting')
  const scrollRef = useRef<HTMLDivElement>(null)
  const autoScrollRef = useRef(true)
  const pendingEventsRef = useRef<LogEvent[]>([])
  const flushTimerRef = useRef<number | null>(null)

  const flushPendingEvents = useCallback(() => {
    flushTimerRef.current = null
    const pending = pendingEventsRef.current
    if (pending.length === 0) return
    pendingEventsRef.current = []
    setEvents((prev) => [...prev, ...pending].slice(-800))
  }, [])

  const enqueueEvent = useCallback((event: LogEvent) => {
    pendingEventsRef.current.push(event)
    if (flushTimerRef.current == null) {
      flushTimerRef.current = window.setTimeout(flushPendingEvents, 150)
    }
  }, [flushPendingEvents])


  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    autoScrollRef.current = atBottom
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (el && autoScrollRef.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [events])
  useEffect(() => {
    const controller = new AbortController()
    setEvents([])
    setStreamStatus('connecting')
    const loadInitial = async () => {
      try {
        const [logRes, evRes] = await Promise.all([
          fetch(`/api/tasks/${taskId}/logs?tail_bytes=120000`, { signal: controller.signal }),
          fetch(`/api/tasks/${taskId}/events`, { signal: controller.signal }),
        ])
        const logText = await logRes.text().catch(() => "")
        const evData = await evRes.json().catch(() => ({ items: [] }))
        if (controller.signal.aborted) return
        const logEvents: LogEvent[] = logText
          ? logText
              .split("\n")
              .filter(Boolean)
              .slice(-250)
              .map((line, i) => ({
                id: i,
                timestamp: "",
                level: "info",
                event_type: "log",
                message: line,
              }))
          : []
        const eventRows = ((evData.items || []) as LogEvent[]).filter((item) => item.event_type !== 'log')
        setEvents([...logEvents, ...eventRows].slice(-500))
      } catch (error) {
        if (controller.signal.aborted) return
        setEvents([{ id: 0, timestamp: "", level: "error", message: "任务日志加载失败" }])
      }
    }
    loadInitial()
    return () => controller.abort()
  }, [taskId])

  useEffect(() => {
    setStreamStatus('connecting')
    const es = new EventSource(EVENT_SOURCE_PATH(taskId))
    es.onopen = () => setStreamStatus('live')
    es.onmessage = (event) => {
      try {
        enqueueEvent(JSON.parse(event.data) as LogEvent)
      } catch {
        // ignore unparseable SSE chunks
      }
    }
    es.addEventListener("done", () => {
      setStreamStatus('closed')
      es.close()
    })
    es.onerror = () => {
      setStreamStatus('error')
      es.close()
    }
    return () => {
      es.close()
      if (flushTimerRef.current != null) {
        window.clearTimeout(flushTimerRef.current)
        flushTimerRef.current = null
      }
      pendingEventsRef.current = []
    }
  }, [taskId, enqueueEvent])
  const toggleGroup = (name: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(name)) {
        next.delete(name)
      } else {
        next.add(name)
      }
      return next
    })
  }

  const groups = useMemo(() => groupEvents(events), [events])

  return (
    <div className="flex h-full flex-col border-l border-white/5 bg-zinc-950">
      <div className="flex items-center justify-between border-b border-white/5 px-4 py-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-zinc-100">
            任务日志
          </h3>
          <p className="truncate text-[11px] text-zinc-500">{taskId}</p>
          <p className={cn("mt-1 text-[11px]", streamStatus === 'live' ? 'text-emerald-400' : streamStatus === 'error' ? 'text-red-400' : 'text-zinc-500')}>
            {streamStatus === 'live' ? '实时日志已连接' : streamStatus === 'connecting' ? '正在连接实时日志…' : streamStatus === 'closed' ? '日志流已结束' : '实时日志连接断开'}
          </p>
        </div>
        {onClose && (
          <Button variant="ghost" size="icon" onClick={onClose}>
            <X size={16} />
          </Button>
        )}
      </div>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-auto p-3"
      >
        {groups.length === 0 && (
          <p className="text-center text-xs text-zinc-600">
            选择任务查看日志。
          </p>
        )}
        {groups.map((group) => (
          <div key={group.name} className="mb-3">
            <button
              onClick={() => toggleGroup(group.name)}
              className="mb-1 flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left text-xs font-medium text-zinc-400 hover:bg-white/5"
            >
              {collapsedGroups.has(group.name) ? (
                <ChevronRight size={12} />
              ) : (
                <ChevronDown size={12} />
              )}
              {group.name}
              <span className="ml-auto text-[10px] text-zinc-600">
                {group.events.length}
              </span>
            </button>
            {!collapsedGroups.has(group.name) && (
              <div className="space-y-0.5">
                {group.events.map((ev, i) => (
                  <div
                    key={ev.id ?? i}
                    className={cn(
                      "rounded px-2 py-0.5 text-xs leading-relaxed",
                      "font-mono",
                    )}
                  >
                    {ev.timestamp && (
                      <span className="mr-2 select-none text-zinc-600">
                        {fmtTime(ev.timestamp)}
                      </span>
                    )}
                    <span className={levelColor(ev.level)}>
                      {ev.message || JSON.stringify(ev)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {!autoScrollRef.current && events.length > 0 && (
        <div className="border-t border-white/5 px-3 py-1.5">
          <button
            onClick={() => {
              autoScrollRef.current = true
              const el = scrollRef.current
              if (el) el.scrollTop = el.scrollHeight
            }}
            className="w-full rounded py-1 text-center text-[11px] text-zinc-500 hover:text-zinc-300"
          >
            ↓ 恢复自动滚动
          </button>
        </div>
      )}
    </div>
  )
}
