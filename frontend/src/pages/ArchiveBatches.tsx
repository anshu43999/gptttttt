import { useCallback, useEffect, useState } from 'react'
import { Archive, RefreshCw, RotateCcw } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { listArchiveBatches, restoreArchiveBatch, archiveAccountsOlderThan } from '@/lib/api'
import type { ArchiveBatch } from '@/lib/types'
import { formatDate } from '@/lib/utils'

export default function ArchiveBatches() {
  const [items, setItems] = useState<ArchiveBatch[]>([])
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      const res = await listArchiveBatches({ limit: 100, signal })
      setItems(res.items || [])
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      setError(err instanceof Error ? err.message : '加载归档批次失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const handleRestore = async (batch: ArchiveBatch) => {
    const key = batch.batch_key
    const active = Number(batch.active_count || 0)
    if (active <= 0) {
      setMessage(`批次 ${key} 没有可恢复的账号（可能已全部恢复）。`)
      return
    }
    if (!window.confirm(`确定恢复批次「${batch.name || key}」中仍归档的 ${active} 个账号到账号列表吗？\n\n不展示账号详情，仅整批恢复。`)) {
      return
    }
    setActionLoading(key)
    setError(null)
    setMessage(null)
    try {
      const res = await restoreArchiveBatch(key)
      setMessage(`已恢复 ${res.restored || 0} 个账号到账号列表。`)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复失败')
    } finally {
      setActionLoading(null)
    }
  }

  const handleArchiveOlder = async () => {
    if (!window.confirm('将 3 天前创建、且尚未归档的账号整批归档。\n\n仅生成批次统计，不在本页展示账号详情。继续？')) {
      return
    }
    setActionLoading('older')
    setError(null)
    setMessage(null)
    try {
      const res = await archiveAccountsOlderThan(3)
      const batch = res.batch
      setMessage(
        `已归档 ${res.archived || 0} 个账号到批次 ${batch?.batch_key || ''}：成品 ${res.product_count || 0} / Plus ${res.plus_count || 0} / Free ${res.free_count || 0} / 其他 ${res.other_count || 0}`,
      )
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '归档失败')
    } finally {
      setActionLoading(null)
    }
  }

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold text-zinc-100">归档批次</h2>
          <p className="mt-1 text-sm text-zinc-500">
            仅看批次汇总：成品数 / Plus / Free。不展示账号详情以保持快速。可整批恢复到账号列表。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => void load()} disabled={loading || Boolean(actionLoading)}>
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} /> 刷新
          </Button>
          <Button onClick={() => void handleArchiveOlder()} disabled={Boolean(actionLoading)}>
            <Archive size={15} /> {actionLoading === 'older' ? '归档中…' : '归档 3 天前'}
          </Button>
        </div>
      </div>

      {error ? <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}
      {message ? <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">{message}</div> : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardContent className="p-4">
            <div className="text-xs text-zinc-500">批次数</div>
            <div className="mt-1 text-2xl font-semibold text-zinc-100">{items.length}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="text-xs text-zinc-500">累计归档（批次 total）</div>
            <div className="mt-1 text-2xl font-semibold text-zinc-100">
              {items.reduce((sum, item) => sum + Number(item.total_count || 0), 0)}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="text-xs text-zinc-500">仍在归档中</div>
            <div className="mt-1 text-2xl font-semibold text-amber-300">
              {items.reduce((sum, item) => sum + Number(item.active_count || 0), 0)}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="text-xs text-zinc-500">已恢复</div>
            <div className="mt-1 text-2xl font-semibold text-emerald-300">
              {items.reduce((sum, item) => sum + Number(item.restored_count || 0), 0)}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card className="min-h-0 flex-1 overflow-hidden">
        <CardContent className="p-0">
          {loading ? (
            <div className="flex h-48 items-center justify-center text-sm text-zinc-500">加载归档批次…</div>
          ) : items.length === 0 ? (
            <div className="flex h-48 flex-col items-center justify-center gap-2 text-sm text-zinc-500">
              <Archive size={22} className="text-zinc-600" />
              暂无归档批次。可用右上角「归档 3 天前」生成。
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="w-full min-w-[880px] text-left text-sm">
                <thead className="sticky top-0 bg-zinc-950/95 text-xs uppercase tracking-wide text-zinc-500">
                  <tr className="border-b border-white/5">
                    <th className="px-4 py-3 font-medium">批次</th>
                    <th className="px-3 py-3 font-medium">时间</th>
                    <th className="px-3 py-3 font-medium">总数</th>
                    <th className="px-3 py-3 font-medium">成品</th>
                    <th className="px-3 py-3 font-medium">Plus</th>
                    <th className="px-3 py-3 font-medium">Free</th>
                    <th className="px-3 py-3 font-medium">其他</th>
                    <th className="px-3 py-3 font-medium">仍归档</th>
                    <th className="px-3 py-3 font-medium">已恢复</th>
                    <th className="px-3 py-3 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((batch) => {
                    const active = Number(batch.active_count || 0)
                    const canRestore = active > 0
                    return (
                      <tr key={batch.batch_key} className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]">
                        <td className="px-4 py-3">
                          <div className="font-medium text-zinc-200">{batch.name || batch.batch_key}</div>
                          <div className="mt-0.5 font-mono text-[11px] text-zinc-500">{batch.batch_key}</div>
                          {batch.reason ? <div className="mt-1 text-[11px] text-zinc-600">{batch.reason}</div> : null}
                        </td>
                        <td className="whitespace-nowrap px-3 py-3 text-zinc-400">
                          {formatDate(batch.created_at)}
                          {batch.cutoff_at ? (
                            <div className="mt-0.5 text-[11px] text-zinc-600">截止 {formatDate(batch.cutoff_at)}</div>
                          ) : null}
                        </td>
                        <td className="px-3 py-3 font-semibold text-zinc-100">{batch.total_count ?? 0}</td>
                        <td className="px-3 py-3 text-sky-300">{batch.product_count ?? 0}</td>
                        <td className="px-3 py-3 text-emerald-300">{batch.plus_count ?? 0}</td>
                        <td className="px-3 py-3 text-zinc-300">{batch.free_count ?? 0}</td>
                        <td className="px-3 py-3 text-zinc-500">{batch.other_count ?? 0}</td>
                        <td className="px-3 py-3">
                          <Badge variant={active > 0 ? 'warning' : 'secondary'}>{active}</Badge>
                        </td>
                        <td className="px-3 py-3 text-zinc-400">{batch.restored_count ?? 0}</td>
                        <td className="px-3 py-3">
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={!canRestore || actionLoading === batch.batch_key}
                            onClick={() => void handleRestore(batch)}
                            title={canRestore ? '整批恢复到账号列表' : '无可恢复账号'}
                          >
                            <RotateCcw size={14} />
                            {actionLoading === batch.batch_key ? '恢复中…' : '恢复到列表'}
                          </Button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
