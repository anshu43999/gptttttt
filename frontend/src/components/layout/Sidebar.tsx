import {
  Gauge,
  Smartphone,
  Boxes,
  History,
  Settings,
  BarChart3,
  type LucideIcon,
} from "lucide-react"
import { cn } from "@/lib/utils"

export interface NavItem {
  id: string
  label: string
  icon: LucideIcon
}

const DEFAULT_NAV_ITEMS: NavItem[] = [
  { id: "overview", label: "概览", icon: Gauge },
  { id: "register", label: "注册工厂", icon: Smartphone },
  { id: "accounts", label: "账号池", icon: Boxes },
  { id: "tasks", label: "任务队列", icon: History },
  { id: "providers", label: "配置中心", icon: Settings },
  { id: "stats", label: "统计", icon: BarChart3 },
]

export interface SidebarProps {
  activeView: string
  onNavigate: (view: string) => void
  items?: NavItem[]
}

export function Sidebar({
  activeView,
  onNavigate,
  items = DEFAULT_NAV_ITEMS,
}: SidebarProps) {
  return (
    <aside className="flex h-full w-56 flex-col border-r border-white/5 bg-zinc-950">
      <div className="flex items-center gap-3 px-5 py-5">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-600 text-sm font-bold text-white">
          GR
        </div>
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold text-zinc-100">
            GPT Register
          </h1>
          <p className="truncate text-[11px] text-zinc-500">
            本地化注册机
          </p>
        </div>
      </div>

      <nav className="flex-1 space-y-0.5 px-3">
        {items.map((item) => {
          const isActive = activeView === item.id
          return (
            <button
              key={item.id}
              onClick={() => onNavigate(item.id)}
              className={cn(
                "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-blue-600/10 text-blue-400"
                  : "text-zinc-400 hover:bg-white/5 hover:text-zinc-200",
              )}
            >
              <item.icon
                size={18}
                className={cn(isActive ? "text-blue-400" : "text-zinc-500")}
              />
              <span className="flex-1 text-left">{item.label}</span>
            </button>
          )
        })}
      </nav>

      <div className="border-t border-white/5 p-4">
        <p className="text-[11px] leading-relaxed text-zinc-600">
          本地化规则 — 只学习 any-auto-register 的 UI、池化管理、Provider 配置和任务中心。
        </p>
      </div>
    </aside>
  )
}
