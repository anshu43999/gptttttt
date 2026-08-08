import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Users, CheckCircle, XCircle, Star, ArrowRight } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn, formatRelative } from '@/lib/utils'
import { getStatsOverview, getTasks } from '@/lib/api'
import type { StatsOverview, Task } from '@/lib/types'

interface StatItem {
  label: string
  value: number
  icon: React.ComponentType<{ className?: string }>
  color: string
}

export default function Dashboard() {
  const [stats, setStats] = useState<StatsOverview | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [s, tasksRes] = await Promise.all([getStatsOverview(), getTasks({ limit: 10 })])
        if (!cancelled) {
          setStats(s)
          const items = Array.isArray(tasksRes) ? tasksRes : (tasksRes.items || [])
          setTasks(items)
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : '加载失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [])

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="text-zinc-500 animate-pulse">正在加载概览…</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center p-8 gap-4">
        <p className="text-red-400">概览加载失败: {error}</p>
        <Button variant="outline" onClick={() => window.location.reload()}>重试</Button>
      </div>
    )
  }

  const statCards: StatItem[] = [
    {
      label: '账号总数',
      value: stats?.total_accounts ?? 0,
      icon: Users,
      color: 'text-blue-400',
    },
    {
      label: 'Plus 账号',
      value: stats?.active_plus ?? 0,
      icon: CheckCircle,
      color: 'text-emerald-400',
    },
    {
      label: '今日成功',
      value: stats?.today_success ?? 0,
      icon: XCircle,
      color: 'text-green-400',
    },
    {
      label: '今日失败',
      value: stats?.today_fail ?? 0,
      icon: Star,
      color: 'text-red-400',
    },
  ]

  const statusBadge = (status: string) => {
    const map: Record<string, 'success' | 'warning' | 'danger' | 'default'> = {
      running: 'warning',
      complete: 'success',
      completed: 'success',
      failed: 'danger',
      queued: 'default',
    }
    return map[status] ?? 'default'
  }

  const statusLabel = (status: string) => ({
    running: '运行中',
    complete: '已完成',
    completed: '已完成',
    succeeded: '成功',
    failed: '失败',
    queued: '排队中',
    cancelled: '已取消',
  }[status] ?? status)

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-xl font-semibold text-zinc-100">概览</h2>
        <p className="text-sm text-zinc-400 mt-1">账号自动化系统运行总览。</p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {statCards.map(({ label, value, icon: Icon, color }) => (
          <Card key={label}>
            <CardContent className="p-4">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-zinc-400">{label}</p>
                  <p className="text-2xl font-bold text-zinc-100 mt-1">{value.toLocaleString()}</p>
                </div>
                <div className={cn('rounded-lg bg-zinc-800 p-2.5', color)}>
                  <Icon className="h-5 w-5" />
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>最近任务</CardTitle>
          <Link to="/tasks">
            <Button variant="ghost" size="sm">
              查看全部
              <ArrowRight className="h-4 w-4" />
            </Button>
          </Link>
        </CardHeader>
        <CardContent>
          {tasks.length === 0 ? (
            <p className="text-sm text-zinc-500 py-8 text-center">暂无任务。</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-white/5 text-left text-xs text-zinc-500 uppercase">
                    <th className="pb-3 pr-4 font-medium">ID</th>
                    <th className="pb-3 pr-4 font-medium">类型</th>
                    <th className="pb-3 pr-4 font-medium">状态</th>
                    <th className="pb-3 pr-4 font-medium">创建时间</th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((t) => (
                    <tr key={t.id} className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]">
                      <td className="py-2.5 pr-4 text-zinc-300">#{t.id}</td>
                      <td className="py-2.5 pr-4 text-zinc-300">{t.task_type ?? t.type ?? '—'}</td>
                      <td className="py-2.5 pr-4">
                        <Badge variant={statusBadge(t.status)}>{statusLabel(t.status)}</Badge>
                      </td>
                      <td className="py-2.5 pr-4 text-zinc-500">{formatRelative(t.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
