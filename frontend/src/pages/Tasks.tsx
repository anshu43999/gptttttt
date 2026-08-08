import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { StopCircle, Activity, Clock, ListChecks, CheckCircle2, XCircle, Layers, Download } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn, formatRelative } from '@/lib/utils'
import { exportTaskBatches, getAccountExportFields, getTaskBatches, stopAllTasks } from '@/lib/api'
import type { AccountExportField } from '@/lib/types'

type TaskBatch = {
  batch_id: string
  task_type?: string
  started_at?: string
  latest_at?: string
  total: number
  succeeded: number
  failed: number
  failed_raw?: number
  interrupted?: number
  cancelled?: number
  running: number
  queued: number
  active: number
  finished?: number
  progress_pct?: number
  completion_rate_pct?: number
  success_rate_pct?: number
}

type TaskSummary = {
  total: number
  running: number
  queued: number
  succeeded: number
  failed: number
  active: number
}

const TASK_TYPE_LABELS: Record<string, string> = {
  'register-token': '手机号注册',
  'email-register-token': '邮箱注册',
  'email-protocol-register-token': '邮箱协议注册',
  'protocol-register-token': '协议注册',
  'resume-oauth': 'OAuth/CPA 绑定',
  'protocol-cpa-bind': '协议绑定',
  'billing-email-bind': '账单邮箱绑定',
}

function batchLabel(batch: TaskBatch) {
  const type = TASK_TYPE_LABELS[String(batch.task_type || '')] || batch.task_type || '任务批次'
  const id = String(batch.batch_id || '')
  if (id.startsWith('legacy_')) return `${type} · ${id.replace('legacy_', '')}`
  if (id === 'all') return '全部历史任务'
  return `${type} · ${id}`
}

function batchState(batch: TaskBatch) {
  if ((batch.active || 0) > 0) return { label: '进行中', variant: 'warning' as const }
  if ((batch.failed || 0) > 0 && (batch.succeeded || 0) === 0) return { label: '失败', variant: 'danger' as const }
  if ((batch.failed || 0) > 0) return { label: '部分失败', variant: 'warning' as const }
  if ((batch.succeeded || 0) > 0) return { label: '已完成', variant: 'success' as const }
  return { label: '空闲', variant: 'default' as const }
}

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

export default function Tasks() {
  const [batches, setBatches] = useState<TaskBatch[]>([])
  const [summary, setSummary] = useState<TaskSummary>({
    total: 0,
    running: 0,
    queued: 0,
    succeeded: 0,
    failed: 0,
    active: 0,
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionLoading, setActionLoading] = useState(false)
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const [selectedBatchIds, setSelectedBatchIds] = useState<string[]>([])
  const [exportOpen, setExportOpen] = useState(false)
  const [exportLoading, setExportLoading] = useState(false)
  const [exportFields, setExportFields] = useState<AccountExportField[]>([])
  const [selectedExportFields, setSelectedExportFields] = useState<string[]>([])
  const [exportArchive, setExportArchive] = useState(false)
  const [exportOnlySucceeded, setExportOnlySucceeded] = useState(true)
  const loadAbortRef = useRef<AbortController | null>(null)
  const pollTimeoutRef = useRef<number | null>(null)

  const loadBatches = useCallback(async (silent = false, signal?: AbortSignal) => {
    if (!silent) setLoading(true)
    try {
      const res = await getTaskBatches({ limit: 30, signal })
      const nextBatches = Array.isArray(res.batches) ? res.batches : []
      setBatches(nextBatches)
      setSelectedBatchIds((current) => {
        if (current.length === 0) return current
        const alive = new Set(nextBatches.map((batch) => String(batch.batch_id || '')))
        return current.filter((id) => alive.has(id))
      })
      if (res.summary) {
        setSummary({
          total: Number(res.summary.total || 0),
          running: Number(res.summary.running || 0),
          queued: Number(res.summary.queued || 0),
          succeeded: Number(res.summary.succeeded || 0),
          failed: Number(res.summary.failed || 0),
          active: Number(res.summary.active || 0),
        })
      }
      setError(null)
      return res
    } catch (err) {
      if ((err as DOMException)?.name !== 'AbortError') {
        setError(err instanceof Error ? err.message : '加载失败')
      }
      return null
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadAbortRef.current?.abort()
    const controller = new AbortController()
    loadAbortRef.current = controller
    loadBatches(false, controller.signal)
    return () => controller.abort()
  }, [loadBatches])

  const hasActive = summary.active > 0 || batches.some((batch) => (batch.active || 0) > 0)

  useEffect(() => {
    let stopped = false
    const poll = async () => {
      if (stopped) return
      const controller = new AbortController()
      await loadBatches(true, controller.signal)
      if (stopped) return
      pollTimeoutRef.current = window.setTimeout(poll, hasActive ? 2000 : 8000)
    }
    pollTimeoutRef.current = window.setTimeout(poll, hasActive ? 2000 : 8000)
    return () => {
      stopped = true
      if (pollTimeoutRef.current !== null) window.clearTimeout(pollTimeoutRef.current)
      pollTimeoutRef.current = null
    }
  }, [hasActive, loadBatches])

  const latestBatch = useMemo(() => batches[0] || null, [batches])
  const selectableBatches = useMemo(
    () => batches.filter((batch) => {
      const id = String(batch.batch_id || '')
      return id && id !== 'all'
    }),
    [batches],
  )
  const allSelected = selectableBatches.length > 0 && selectableBatches.every((batch) => selectedBatchIds.includes(batch.batch_id))
  const selectedSucceeded = useMemo(
    () => batches
      .filter((batch) => selectedBatchIds.includes(batch.batch_id))
      .reduce((sum, batch) => sum + Number(batch.succeeded || 0), 0),
    [batches, selectedBatchIds],
  )

  const toggleBatch = (batchId: string) => {
    setSelectedBatchIds((current) => (
      current.includes(batchId)
        ? current.filter((id) => id !== batchId)
        : [...current, batchId]
    ))
  }

  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedBatchIds([])
      return
    }
    setSelectedBatchIds(selectableBatches.map((batch) => batch.batch_id))
  }

  const openExportDialog = async () => {
    if (selectedBatchIds.length === 0) {
      setError('请先勾选要导出的任务批次。')
      return
    }
    setError(null)
    setActionMessage(null)
    setExportOpen(true)
    try {
      const fields = exportFields.length > 0 ? exportFields : await getAccountExportFields()
      setExportFields(fields)
      setSelectedExportFields((prev) => (prev.length > 0 ? prev : fields.map((field) => field.key)))
    } catch (err) {
      setError(err instanceof Error ? err.message : '导出字段加载失败')
    }
  }

  const toggleExportField = (fieldKey: string) => {
    setSelectedExportFields((prev) => (
      prev.includes(fieldKey)
        ? prev.filter((item) => item !== fieldKey)
        : [...prev, fieldKey]
    ))
  }

  const handleExportSelectedBatches = async () => {
    if (selectedBatchIds.length === 0) return
    if (selectedExportFields.length === 0) {
      setError('请至少选择一个导出字段。')
      return
    }
    setExportLoading(true)
    setError(null)
    setActionMessage(null)
    try {
      const result = await exportTaskBatches({
        batchIds: selectedBatchIds,
        fields: selectedExportFields,
        onlySucceeded: exportOnlySucceeded,
        archiveAfterExport: exportArchive,
      })
      if (!result.count || !result.products?.length) {
        throw new Error(result.message || '所选批次没有可导出的账号（仅成功注册且已入库账号可导出）')
      }
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
      const shortIds = selectedBatchIds
        .map((id) => id.replace(/^batch_/, '').slice(0, 8))
        .slice(0, 3)
        .join('_')
      downloadJson(
        `task-batches-${selectedBatchIds.length}-${result.count}-${shortIds || 'export'}-${stamp}.json`,
        {
          batch_ids: result.batch_ids,
          count: result.count,
          exported_at: new Date().toISOString(),
          by_batch: result.by_batch || {},
          products: result.products,
        },
      )
      setExportOpen(false)
      const archivePart = exportArchive
        ? `；已归档 ${Number(result.archived || 0)}${(result.archive_missing || []).length ? `，归档失败 ${(result.archive_missing || []).length}` : ''}`
        : ''
      setActionMessage(
        `${result.message || `已导出 ${result.count} 个账号`}；导出状态已写入账号库（export_status=bulk_exported）${archivePart}。`,
      )
      setSelectedBatchIds([])
      await loadBatches(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '批次导出失败')
    } finally {
      setExportLoading(false)
    }
  }

  const handleStopAll = async () => {
    setActionLoading(true)
    setActionMessage(null)
    setError(null)
    try {
      const res = await stopAllTasks()
      await loadBatches(true)
      setActionMessage(`已结束 ${res.stopped}/${res.requested} 个当前任务${res.failed ? `，失败 ${res.failed} 个` : ''}。`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '结束所有任务失败')
    } finally {
      setActionLoading(false)
    }
  }

  if (loading && batches.length === 0 && summary.total === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="text-zinc-500 animate-pulse">正在加载任务汇总…</div>
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-zinc-100">任务</h2>
          <p className="text-sm text-zinc-400 mt-1">
            按批次汇总显示：本次总数 / 成功 / 失败 / 进行中。可勾选批次批量导出账号 JSON，并同步入库 export_status。
            {latestBatch ? ` 最近批次 ${latestBatch.total} 个，成功 ${latestBatch.succeeded}，失败 ${latestBatch.failed}，进行中 ${latestBatch.active}。` : ''}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            onClick={() => void openExportDialog()}
            disabled={exportLoading || actionLoading || selectedBatchIds.length === 0}
            title="导出选中批次内成功注册账号为 JSON，并同步账号列表导出状态"
          >
            <Download size={16} />
            {exportLoading ? '正在导出…' : `批量导出 ${selectedBatchIds.length}`}
          </Button>
          <Button variant="outline" onClick={() => loadBatches(false)} disabled={loading || actionLoading || exportLoading}>
            刷新
          </Button>
          <Button
            variant="destructive"
            onClick={handleStopAll}
            disabled={actionLoading || exportLoading}
            title="停止运行中任务并取消排队/待启动任务"
          >
            <StopCircle size={16} />
            {actionLoading ? '正在结束…' : '结束所有任务'}
          </Button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Layers className="text-sky-300" size={18} />
            <div>
              <p className="text-xs text-zinc-500">全部任务</p>
              <p className="text-lg font-semibold text-zinc-100">{summary.total}</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Activity className="text-amber-300" size={18} />
            <div>
              <p className="text-xs text-zinc-500">进行中</p>
              <p className="text-lg font-semibold text-zinc-100">{summary.running + summary.queued}</p>
              <p className="text-[11px] text-zinc-500">运行 {summary.running} · 排队 {summary.queued}</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <CheckCircle2 className="text-emerald-300" size={18} />
            <div>
              <p className="text-xs text-zinc-500">成功</p>
              <p className="text-lg font-semibold text-zinc-100">{summary.succeeded}</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <XCircle className="text-red-300" size={18} />
            <div>
              <p className="text-xs text-zinc-500">失败/中断</p>
              <p className="text-lg font-semibold text-zinc-100">{summary.failed}</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Clock className="text-blue-300" size={18} />
            <div>
              <p className="text-xs text-zinc-500">最近批次</p>
              <p className="text-lg font-semibold text-zinc-100">{latestBatch?.total ?? 0}</p>
              <p className="text-[11px] text-zinc-500">
                成功 {latestBatch?.succeeded ?? 0} · 失败 {latestBatch?.failed ?? 0} · 进行中 {latestBatch?.active ?? 0}
              </p>
            </div>
          </CardContent>
        </Card>
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">
          {error}
          <button onClick={() => setError(null)} className="ml-2 underline">关闭</button>
        </div>
      )}

      {actionMessage && (
        <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-300">
          {actionMessage}
          <button onClick={() => setActionMessage(null)} className="ml-2 underline">关闭</button>
        </div>
      )}

      {selectedBatchIds.length > 0 && (
        <div className="rounded-lg border border-sky-500/20 bg-sky-500/10 px-4 py-3 text-sm text-sky-200">
          已选 {selectedBatchIds.length} 个批次 · 成功任务合计约 {selectedSucceeded}（实际导出以入库账号为准）
          <button onClick={() => setSelectedBatchIds([])} className="ml-2 underline">清空选择</button>
        </div>
      )}

      <Card>
        <CardContent className="p-0">
          {batches.length === 0 ? (
            <p className="text-sm text-zinc-500 py-12 text-center">暂无任务批次。</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-white/5 text-left text-xs text-zinc-500 uppercase">
                    <th className="py-3 px-4 font-medium w-10">
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleSelectAll}
                        className="h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
                        title="全选当前页批次"
                      />
                    </th>
                    <th className="py-3 px-4 font-medium">批次</th>
                    <th className="py-3 px-4 font-medium">状态</th>
                    <th className="py-3 px-4 font-medium">本次总数</th>
                    <th className="py-3 px-4 font-medium">成功</th>
                    <th className="py-3 px-4 font-medium">失败</th>
                    <th className="py-3 px-4 font-medium">进行中</th>
                    <th className="py-3 px-4 font-medium">完成 / 成功率</th>
                    <th className="py-3 px-4 font-medium">时间</th>
                  </tr>
                </thead>
                <tbody>
                  {batches.map((batch) => {
                    const state = batchState(batch)
                    const completion = Math.max(0, Math.min(100, Number(batch.completion_rate_pct ?? batch.progress_pct ?? 0)))
                    const successRate = Math.max(0, Math.min(100, Number(batch.success_rate_pct ?? (batch.total ? (batch.succeeded / batch.total) * 100 : 0))))
                    const selectable = Boolean(batch.batch_id) && batch.batch_id !== 'all'
                    const checked = selectedBatchIds.includes(batch.batch_id)
                    return (
                      <tr key={batch.batch_id} className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]">
                        <td className="py-3 px-4">
                          {selectable ? (
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => toggleBatch(batch.batch_id)}
                              className="h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
                            />
                          ) : (
                            <span className="text-zinc-600">—</span>
                          )}
                        </td>
                        <td className="py-3 px-4">
                          <div className="font-medium text-zinc-100">{batchLabel(batch)}</div>
                          <div className="mt-0.5 font-mono text-[11px] text-zinc-500">{batch.batch_id}</div>
                        </td>
                        <td className="py-3 px-4">
                          <Badge variant={state.variant}>{state.label}</Badge>
                        </td>
                        <td className="py-3 px-4 text-zinc-200 font-semibold">{batch.total}</td>
                        <td className="py-3 px-4 text-emerald-300">{batch.succeeded}</td>
                        <td className="py-3 px-4 text-red-300">{batch.failed}</td>
                        <td className="py-3 px-4 text-amber-200">
                          {batch.active}
                          <div className="text-[11px] text-zinc-500">运行 {batch.running} · 排队 {batch.queued}</div>
                        </td>
                        <td className="py-3 px-4 min-w-[140px]">
                          <div className="mb-1 flex items-center justify-between gap-3 text-xs">
                            <span className="text-zinc-400">完成 {completion}%</span>
                            <span className={cn('font-medium', successRate >= 80 ? 'text-emerald-300' : successRate >= 50 ? 'text-amber-200' : 'text-red-300')}>
                              成功 {successRate}%
                            </span>
                          </div>
                          <div className="h-1.5 overflow-hidden rounded-full bg-white/10" title="完成率：成功 + 失败 + 取消 / 总数">
                            <div
                              className={cn(
                                'h-full rounded-full transition-all',
                                completion >= 100 ? 'bg-emerald-400' : 'bg-sky-400',
                              )}
                              style={{ width: `${completion}%` }}
                            />
                          </div>
                        </td>
                        <td className="py-3 px-4 text-xs text-zinc-500">
                          <div>开始 {formatRelative(batch.started_at || '')}</div>
                          <div className="text-zinc-600">更新 {formatRelative(batch.latest_at || batch.started_at || '')}</div>
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

      <div className="rounded-xl border border-white/5 bg-white/[0.02] px-4 py-3 text-xs text-zinc-500">
        <div className="flex items-start gap-2">
          <ListChecks size={14} className="mt-0.5 text-zinc-400" />
          <div>
            勾选批次后点「批量导出」：按账号的 <code className="text-zinc-400">registration_task_id</code> 关联成功任务，
            导出与账号列表相同的 JSON，并写回 <code className="text-zinc-400">export_status=bulk_exported</code>。
            可选导出后归档（与账号列表一致）。
          </div>
        </div>
      </div>

      <Dialog open={exportOpen} onOpenChange={(open) => { if (!exportLoading) setExportOpen(open) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>批量导出 {selectedBatchIds.length} 个任务批次</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-zinc-400">
              导出结构与账号列表「批量导出」相同；成功后账号库导出状态会同步为「已批量导出」。
              当前勾选成功任务合计约 {selectedSucceeded}（仅已入库账号可导出）。
            </p>
            <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-white/10 bg-black/20 p-3 hover:border-white/20">
              <input
                type="checkbox"
                checked={exportOnlySucceeded}
                onChange={(event) => setExportOnlySucceeded(event.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
              />
              <span>
                <span className="block text-sm font-medium text-zinc-200">仅导出成功任务对应账号</span>
                <span className="mt-1 block text-xs leading-5 text-zinc-500">推荐开启。关闭时会尝试包含失败任务若已有账号记录。</span>
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-white/10 bg-black/20 p-3 hover:border-white/20">
              <input
                type="checkbox"
                checked={exportArchive}
                onChange={(event) => setExportArchive(event.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
              />
              <span>
                <span className="block text-sm font-medium text-zinc-200">导出后自动归档</span>
                <span className="mt-1 block text-xs leading-5 text-zinc-500">仅归档本次成功导出的账号；与账号列表行为一致。</span>
              </span>
            </label>
            <div className="max-h-72 space-y-2 overflow-auto rounded-lg border border-white/10 bg-black/20 p-3">
              {exportFields.map((field) => (
                <label key={field.key} className="flex cursor-pointer items-start gap-3 rounded-md p-2 hover:bg-white/[0.03]">
                  <input
                    type="checkbox"
                    checked={selectedExportFields.includes(field.key)}
                    onChange={() => toggleExportField(field.key)}
                    className="mt-1 h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
                  />
                  <span>
                    <span className="block text-sm text-zinc-200">{field.label}</span>
                    <span className="block text-xs text-zinc-500">{field.description}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setExportOpen(false)} disabled={exportLoading}>取消</Button>
            <Button onClick={() => void handleExportSelectedBatches()} disabled={exportLoading || selectedExportFields.length === 0}>
              {exportLoading ? '正在导出…' : '下载 JSON 并同步状态'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
