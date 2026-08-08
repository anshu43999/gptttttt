import type { ReactNode } from "react"
import { Sidebar, type SidebarProps } from "@/components/layout/Sidebar"

export interface ShellProps {
  children: ReactNode
  sidebar: SidebarProps
}

export function Shell({ children, sidebar }: ShellProps) {
  return (
    <div className="flex h-screen bg-zinc-950 text-zinc-200">
      <Sidebar {...sidebar} />
      <main className="flex-1 overflow-auto">
        {children}
      </main>
    </div>
  )
}
