import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Activity, CheckCircle2, Clock, Download, Eye, ListChecks, RefreshCw, RotateCcw, Search, ShieldCheck, Trash2, XCircle } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { cancelPlusVerification, exportPlusActivationBatch, getPlusVerification, listActivationTasks, listPlusActivationBatches, listPlusActivationBatchItems, refreshActivationTasks, refreshPlusActivationBatch, releaseActivations, releasePlusActivationBatch, retryActivationTasks, retryPlusActivationBatch, showPlusActivationBatchAccounts, startPlusVerification } from '@/lib/api'
import { formatDate } from '@/lib/utils'
import type { Account, ActivationQueueStats, PlusActivationBatch, PlusActivationBatchItem, PlusVerificationProgress } from '@/lib/types'
import {
  forgetPlusVerificationTask,
  readStoredPlusVerificationTasks,
  rememberPlusVerificationTask,
  rememberPlusVerificationTaskId,
} from '@/lib/plusProgressStorage'
import type { StoredPlusVerificationTask } from '@/lib/plusProgressStorage'

const ACTIVE_ACTIVATION_STATUSES: Record<string, true> = {
  queued: true,
  submitting: true,
  submit_unknown: true,
  submitted: true,
  processing: true,
  verifying: true,
}

function progressPercent(progress?: PlusVerificationProgress | StoredPlusVerificationTask | null): number {
  if (!progress?.total) return 0
  return Math.min(100, Math.round((progress.completed / progress.total) * 100))
}

function taskBadge(progress?: PlusVerificationProgress | StoredPlusVerificationTask | null) {
  if (!progress) return { label: '未选择', variant: 'secondary' as const }
  if (progress.running && progress.cancelled) return { label: '取消中', variant: 'warning' as const }
  if (progress.running) return { label: '校验中', variant: 'warning' as const }
  if (progress.cancelled) return { label: '已取消', variant: 'secondary' as const }
  if (progress.failed > 0) return { label: '有失败', variant: 'danger' as const }
  return { label: '已完成', variant: 'success' as const }
}

function resultText(item: PlusVerificationProgress['results'][number]): string {
  if (item.ok) return item.plan_type || (item.paid ? 'Plus/Team' : 'ok')
  return item.message || item.error_code || '失败'
}

function activationBadge(status?: string) {
  const value = String(status || 'idle')
  if (['success', 'verified', 'active', 'exported', 'archived'].includes(value)) return { label: value === 'verified' ? '已开通' : value, variant: 'success' as const }
  if (value === 'replace_account') return { label: '需换号', variant: 'danger' as const }
  if (['failed', 'expired', 'releasable'].includes(value)) return { label: value === 'releasable' ? '可释放' : value === 'expired' ? '已过期' : '失败', variant: 'danger' as const }
  if (['cancelled', 'released', 'skipped'].includes(value)) return { label: value === 'released' ? '已释放' : value, variant: 'secondary' as const }
  if (ACTIVE_ACTIVATION_STATUSES[value] || value === 'reserved') return { label: '进行中', variant: 'warning' as const }
  return { label: value || '空闲', variant: 'secondary' as const }
}

function batchBadge(status?: string) {
  const value = String(status || '')
  if (value === 'completed') return { label: '完成', variant: 'success' as const }
  if (value === 'completed_with_failures') return { label: '有失败', variant: 'danger' as const }
  if (value === 'archived') return { label: '已归档', variant: 'secondary' as const }
  if (value === 'running' || value === 'queued') return { label: '运行中', variant: 'warning' as const }
  return { label: value || '未知', variant: 'secondary' as const }
}

function accountLabel(account: Account): string {
  return account.billing_email || account.codex_email || account.email || account.login_identifier || account.key
}

export default function PlusProgress() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [trackedTasks, setTrackedTasks] = useState<StoredPlusVerificationTask[]>([])
  const [activeTaskId, setActiveTaskId] = useState('')
  const [manualTaskId, setManualTaskId] = useState('')
  const [progress, setProgress] = useState<PlusVerificationProgress | null>(null)
  const [loading, setLoading] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [retryRegion, setRetryRegion] = useState<'JP' | 'VN'>('JP')
  const [activationTasks, setActivationTasks] = useState<Account[]>([])
  const [activationStats, setActivationStats] = useState<ActivationQueueStats | null>(null)
  const [activationLoading, setActivationLoading] = useState(false)
  const [activationActionLoading, setActivationActionLoading] = useState(false)
  const [activationFilter, setActivationFilter] = useState('')
  const [batches, setBatches] = useState<PlusActivationBatch[]>([])
  const [activeBatchKey, setActiveBatchKey] = useState('')
  const [batchItems, setBatchItems] = useState<PlusActivationBatchItem[]>([])
  const [batchLoading, setBatchLoading] = useState(false)
  const [batchActionLoading, setBatchActionLoading] = useState(false)
  const lastBatchRemoteRefreshRef = useRef(0)
  const [batchItemFilter, setBatchItemFilter] = useState('')

  const refreshTrackedTasks = useCallback(() => {
    setTrackedTasks(readStoredPlusVerificationTasks())
  }, [])

  const selectTask = useCallback((taskId: string) => {
    const nextTaskId = taskId.trim()
    setActiveTaskId(nextTaskId)
    setProgress(null)
    setError(null)
    if (nextTaskId) setSearchParams({ task: nextTaskId })
    else setSearchParams({})
  }, [setSearchParams])

  const loadTask = useCallback(async (taskId: string, silent = false, signal?: AbortSignal) => {
    const nextTaskId = taskId.trim()
    if (!nextTaskId) return null
    if (!silent) setLoading(true)
    try {
      const next = await getPlusVerification(nextTaskId, { signal })
      setProgress(next)
      rememberPlusVerificationTask(next)
      refreshTrackedTasks()
      setError(null)
      return next
    } catch (err) {
      if ((err as DOMException)?.name !== 'AbortError') {
        setError(err instanceof Error ? err.message : '读取 Plus 校验进度失败')
      }
      return null
    } finally {
      if (!silent) setLoading(false)
    }
  }, [refreshTrackedTasks])

  const loadActivationProgress = useCallback(async (silent = false, signal?: AbortSignal) => {
    if (!silent) setActivationLoading(true)
    try {
      const res = await listActivationTasks({ status: activationFilter || undefined, limit: 500, signal })
      setActivationTasks(res.items)
      if (res.stats) setActivationStats(res.stats)
      setError(null)
      return res
    } catch (err) {
      if ((err as DOMException)?.name !== 'AbortError') {
        setError(err instanceof Error ? err.message : '读取 UPI 开通进度失败')
      }
      return null
    } finally {
      if (!silent) setActivationLoading(false)
    }
  }, [activationFilter])

  const loadBatches = useCallback(async (silent = false, signal?: AbortSignal) => {
    if (!silent) setBatchLoading(true)
    try {
      const res = await listPlusActivationBatches({ status: 'active', limit: 50, signal })
      setBatches(res.items || [])
      setError(null)
      return res.items || []
    } catch (err) {
      if ((err as DOMException)?.name !== 'AbortError') {
        setError(err instanceof Error ? err.message : '读取 Plus 批次失败')
      }
      return []
    } finally {
      if (!silent) setBatchLoading(false)
    }
  }, [])

  const loadBatchItems = useCallback(async (batchKey: string, silent = false, signal?: AbortSignal) => {
    const key = batchKey.trim()
    if (!key) {
      setBatchItems([])
      return null
    }
    if (!silent) setBatchLoading(true)
    try {
      const res = await listPlusActivationBatchItems(key, { status: batchItemFilter || undefined, limit: 400, signal })
      setBatchItems(res.items || [])
      setError(null)
      return res
    } catch (err) {
      if ((err as DOMException)?.name !== 'AbortError') {
        setError(err instanceof Error ? err.message : '读取批次明细失败')
      }
      return null
    } finally {
      if (!silent) setBatchLoading(false)
    }
  }, [batchItemFilter])

  useEffect(() => {
    const stored = readStoredPlusVerificationTasks()
    setTrackedTasks(stored)
    const queryTask = (searchParams.get('task') || '').trim()
    const initialTask = queryTask || stored[0]?.task_id || ''
    if (initialTask) {
      if (queryTask) rememberPlusVerificationTaskId(queryTask)
      setActiveTaskId(initialTask)
    }
  }, [searchParams])

  useEffect(() => {
    if (!activeTaskId) return
    const controller = new AbortController()
    loadTask(activeTaskId, false, controller.signal)
    return () => controller.abort()
  }, [activeTaskId, loadTask])

  useEffect(() => {
    if (!activeTaskId) return
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      if (stopped) return
      const next = await loadTask(activeTaskId, true)
      if (stopped) return
      timer = window.setTimeout(poll, next?.running ? 2000 : 8000)
    }
    timer = window.setTimeout(poll, progress?.task_id === activeTaskId && progress.running ? 2000 : 8000)
    return () => {
      stopped = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [activeTaskId, loadTask, progress?.running, progress?.task_id])

  useEffect(() => {
    const controller = new AbortController()
    loadActivationProgress(false, controller.signal)
    return () => controller.abort()
  }, [loadActivationProgress])

  useEffect(() => {
    const hasActiveActivation = activationTasks.some((account) => ACTIVE_ACTIVATION_STATUSES[String(account.activation_status || '')])
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      if (stopped) return
      await loadActivationProgress(true)
      if (!stopped) timer = window.setTimeout(poll, hasActiveActivation ? 2500 : 8000)
    }
    timer = window.setTimeout(poll, hasActiveActivation ? 2500 : 8000)
    return () => {
      stopped = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [activationTasks, loadActivationProgress])

  useEffect(() => {
    const controller = new AbortController()
    loadBatches(false, controller.signal).then((items) => {
      const queryBatch = (searchParams.get('batch') || '').trim()
      const nextKey = queryBatch || activeBatchKey || items[0]?.batch_key || ''
      if (nextKey && nextKey !== activeBatchKey) setActiveBatchKey(nextKey)
    })
    return () => controller.abort()
  }, [loadBatches, searchParams])

  useEffect(() => {
    if (!activeBatchKey) return
    const controller = new AbortController()
    loadBatchItems(activeBatchKey, false, controller.signal)
    return () => controller.abort()
  }, [activeBatchKey, loadBatchItems])

  useEffect(() => {
    const hasActiveBatch = batches.some((batch) => ['queued', 'running'].includes(String(batch.status || '')))
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      if (stopped) return
      const selected = batches.find((batch) => batch.batch_key === activeBatchKey)
      const selectedActive = selected && ['queued', 'running'].includes(String(selected.status || ''))
      const now = Date.now()
      if (activeBatchKey && selectedActive && now - lastBatchRemoteRefreshRef.current >= 15000) {
        lastBatchRemoteRefreshRef.current = now
        try {
          await refreshPlusActivationBatch(activeBatchKey)
        } catch {
          // Best-effort remote sync; list polling below still keeps local state moving.
        }
      }
      await loadBatches(true)
      if (activeBatchKey) await loadBatchItems(activeBatchKey, true)
      if (!stopped) timer = window.setTimeout(poll, hasActiveBatch ? 3000 : 10000)
    }
    timer = window.setTimeout(poll, hasActiveBatch ? 3000 : 10000)
    return () => {
      stopped = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [batches, activeBatchKey, loadBatches, loadBatchItems])

  const activeBatch = batches.find((batch) => batch.batch_key === activeBatchKey) || null
  const activeBatchStats = activeBatch ? batchBadge(activeBatch.status) : batchBadge('')
  const batchVisibleItems = useMemo(() => batchItems.slice(0, 120), [batchItems])
  const batchReleasableCount = useMemo(() => batchItems.filter((item) => item.can_release || ['failed', 'releasable'].includes(String(item.status || ''))).length, [batchItems])
  const batchExportableCount = activeBatch ? activeBatch.verified_count + activeBatch.exported_count + activeBatch.archived_count : 0

  const failedResults = useMemo(() => (progress?.results || []).filter((item) => !item.ok), [progress])
  const latestResults = useMemo(() => [...(progress?.results || [])].reverse().slice(0, 80), [progress])
  const activeStoredTask = trackedTasks.find((item) => item.task_id === activeTaskId)
  const visibleProgress = progress || activeStoredTask || null
  const badge = taskBadge(visibleProgress)
  const pendingCount = progress?.pending_keys?.length ?? 0
  const inFlightCount = progress?.in_flight_keys?.length ?? 0

  const handleTrackTask = async () => {
    const taskId = manualTaskId.trim()
    if (!taskId) return
    rememberPlusVerificationTaskId(taskId)
    refreshTrackedTasks()
    selectTask(taskId)
    setManualTaskId('')
  }

  const handleCancel = async () => {
    if (!progress?.task_id || !progress.running || progress.cancelled) return
    setActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const next = await cancelPlusVerification(progress.task_id)
      setProgress(next)
      rememberPlusVerificationTask(next)
      refreshTrackedTasks()
      setMessage('已请求取消 Plus 校验；正在运行的请求结束后会停止。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '取消 Plus 校验失败')
    } finally {
      setActionLoading(false)
    }
  }

  const handleRetryFailed = async () => {
    const keys = failedResults.map((item) => item.key).filter(Boolean)
    if (keys.length === 0) return
    setActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const next = await startPlusVerification(keys, retryRegion)
      rememberPlusVerificationTask(next, `重试失败项 ${new Date().toLocaleString()}`)
      refreshTrackedTasks()
      selectTask(next.task_id)
      setProgress(next)
      setMessage(`已创建失败重试任务：${keys.length} 个账号，出口 ${retryRegion}。`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重试失败项失败')
    } finally {
      setActionLoading(false)
    }
  }

  const handleForgetTask = (taskId: string) => {
    forgetPlusVerificationTask(taskId)
    refreshTrackedTasks()
    if (taskId === activeTaskId) selectTask('')
  }

  const handleRefreshActivation = async () => {
    setActivationActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const res = await refreshActivationTasks([])
      if (res.stats) setActivationStats(res.stats)
      await loadActivationProgress(true)
      setMessage(res.message || `已手动轮询 ${res.checked} 个远端任务，更新 ${res.updated} 个。`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '手动轮询 UPI 任务失败')
    } finally {
      setActivationActionLoading(false)
    }
  }

  const handleRetryActivationFailed = async () => {
    setActivationActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const res = await retryActivationTasks([], 'upi')
      await loadActivationProgress(true)
      setMessage(res.message || `已重新提交失败/已取消/已释放任务：accepted=${res.accepted || 0} queued=${res.queued || 0} skipped=${res.skipped || 0} failed=${res.failed || 0}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重试 UPI 失败任务失败')
    } finally {
      setActivationActionLoading(false)
    }
  }

  const handleReleaseReleasableActivations = async () => {
    const keys = activationTasks.filter((account) => Boolean(account.activation_can_release)).map((account) => account.key)
    if (keys.length === 0) return
    setActivationActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const res = await releaseActivations(keys)
      await loadActivationProgress(true)
      setMessage(res.message || `已释放 ${res.released} 个可释放 UPI 任务，失败 ${res.failed} 个。`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '释放可释放 UPI 任务失败')
    } finally {
      setActivationActionLoading(false)
    }
  }

  const selectBatch = (batchKey: string) => {
    const key = batchKey.trim()
    setActiveBatchKey(key)
    setBatchItems([])
    if (key) setSearchParams({ batch: key })
  }

  const handleRefreshBatch = async () => {
    if (!activeBatchKey) return
    setBatchActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const res = await refreshPlusActivationBatch(activeBatchKey)
      await loadBatches(true)
      await loadBatchItems(activeBatchKey, true)
      setMessage(`已刷新批次：${res.batch.progress_percent}% · ${res.batch.status}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '刷新批次失败')
    } finally {
      setBatchActionLoading(false)
    }
  }

  const handleRetryBatch = async () => {
    if (!activeBatchKey) return
    setBatchActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const res = await retryPlusActivationBatch(activeBatchKey, { statuses: ['failed', 'releasable', 'released', 'submit_unknown'], channel: 'upi' })
      await loadBatches(true)
      await loadBatchItems(activeBatchKey, true)
      setMessage(res.message || `已重试 ${res.retried || 0} 个批次失败项。`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '批次重试失败')
    } finally {
      setBatchActionLoading(false)
    }
  }

  const handleReleaseBatch = async () => {
    if (!activeBatchKey) return
    setBatchActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const res = await releasePlusActivationBatch(activeBatchKey, { statuses: ['failed', 'releasable', 'submit_unknown', 'submitted', 'processing'] })
      await loadBatches(true)
      await loadBatchItems(activeBatchKey, true)
      setMessage(res.message || `已释放 ${res.released || 0} 个批次项。`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '批次释放失败')
    } finally {
      setBatchActionLoading(false)
    }
  }

  const handleShowBatchAccounts = async () => {
    if (!activeBatchKey) return
    setBatchActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const res = await showPlusActivationBatchAccounts(activeBatchKey)
      await loadBatches(true)
      await loadBatchItems(activeBatchKey, true)
      setMessage(res.message || `已允许 ${res.visible || 0} 个账号重新显示在账号列表。`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复账号列表显示失败')
    } finally {
      setBatchActionLoading(false)
    }
  }

  const handleExportBatch = async () => {
    if (!activeBatchKey) return
    setBatchActionLoading(true)
    setError(null)
    setMessage(null)
    try {
      const includeAlreadyExported = Boolean(activeBatch && activeBatch.verified_count === 0 && activeBatch.exported_count + activeBatch.archived_count > 0)
      const res = await exportPlusActivationBatch(activeBatchKey, { format: 'txt', include_already_exported: includeAlreadyExported, archive_after_export: true })
      if (!res.count) {
        throw new Error(res.message || '没有可导出的 Plus 成品号')
      }
      const fileName = res.file_name || `plus-batch-${res.count}.txt`
      const text = res.text || ''
      if (text) {
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
        const url = URL.createObjectURL(blob)
        const anchor = document.createElement('a')
        anchor.href = url
        anchor.download = fileName
        document.body.appendChild(anchor)
        anchor.click()
        anchor.remove()
        URL.revokeObjectURL(url)
      } else if (res.download_url) {
        // Fallback for older backends that only return a download URL.
        const fallback = document.createElement('a')
        fallback.href = res.download_url
        fallback.download = fileName
        fallback.rel = 'noopener'
        document.body.appendChild(fallback)
        fallback.click()
        fallback.remove()
      } else {
        throw new Error('导出成功但未返回文件内容，请刷新后重试')
      }
      await loadBatches(true)
      await loadBatchItems(activeBatchKey, true)
      setMessage(res.message || `已导出 ${res.count} 个 Plus 成品号：${fileName}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '导出批次 Plus 成品号失败')
    } finally {
      setBatchActionLoading(false)
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="mb-2 inline-flex items-center gap-2 rounded-full border border-cyan-400/20 bg-cyan-950/25 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-cyan-300">
            <Activity size={13} /> Plus Progress
          </div>
          <h2 className="text-xl font-semibold text-zinc-100">Plus 进度</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-zinc-400">
            跟踪 UPI 开通与异步 Plus 校验：轮询进度、释放可释放 CDK、重试失败项。任务号只保存在本机浏览器，不包含 access token 或 actk。
          </p>
        </div>
        <Button variant="outline" asChild>
          <Link to="/accounts">返回账号池</Link>
        </Button>
      </div>

      <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <Card className="border-white/10 bg-zinc-950">
          <CardContent className="space-y-4 p-4">
            <div>
              <div className="text-sm font-medium text-zinc-100">Plus 批次</div>
              <p className="mt-1 text-xs leading-5 text-zinc-500">批次是数据库持久记录；400 个账号只显示一个批次卡片，明细在右侧查看、重试、释放、导出。</p>
            </div>
            <div className="space-y-2">
              {batches.length === 0 ? (
                <div className="rounded-lg border border-dashed border-white/10 bg-black/20 px-4 py-6 text-center text-xs text-zinc-500">暂无 Plus 批次。到账号池选择账号后点击“批量 UPI 开通”。</div>
              ) : batches.map((batch) => {
                const state = batchBadge(batch.status)
                const selected = batch.batch_key === activeBatchKey
                return (
                  <button
                    key={batch.batch_key}
                    type="button"
                    onClick={() => selectBatch(batch.batch_key)}
                    className={`w-full rounded-xl border p-3 text-left transition-colors ${selected ? 'border-emerald-400/50 bg-emerald-950/30' : 'border-white/10 bg-black/20 hover:border-white/20'}`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-zinc-100" title={batch.name || batch.batch_key}>{batch.name || batch.batch_key}</div>
                        <div className="mt-1 truncate font-mono text-[11px] text-zinc-500" title={batch.batch_key}>{batch.batch_key}</div>
                      </div>
                      <Badge variant={state.variant}>{state.label}</Badge>
                    </div>
                    <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-zinc-900">
                      <div className="h-full rounded-full bg-emerald-300" style={{ width: `${Math.max(0, Math.min(100, Number(batch.progress_percent || 0)))}%` }} />
                    </div>
                    <div className="mt-2 flex items-center justify-between text-[11px] text-zinc-500">
                      <span>{batch.total_count} 个 · 成功 {batch.verified_count + batch.exported_count + batch.archived_count} · 失败 {batch.failed_count + batch.releasable_count}</span>
                      <span>{formatDate(batch.updated_at)}</span>
                    </div>
                  </button>
                )
              })}
            </div>

            <div className="border-t border-white/10 pt-4">
              <div className="text-sm font-medium text-zinc-100">Plus 校验任务</div>
              <p className="mt-1 text-xs leading-5 text-zinc-500">仅保留浏览器本地 plus-verify-* 历史，用于旧校验任务跟踪。</p>
            </div>
            <div className="flex gap-2">
              <Input
                value={manualTaskId}
                onChange={(event) => setManualTaskId(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') void handleTrackTask() }}
                placeholder="plus-verify-…"
                className="font-mono text-xs"
              />
              <Button variant="outline" size="icon" onClick={() => void handleTrackTask()} disabled={!manualTaskId.trim()} aria-label="跟踪任务">
                <Search size={15} />
              </Button>
            </div>
            <div className="space-y-2">
              {trackedTasks.length === 0 ? (
                <div className="rounded-lg border border-dashed border-white/10 bg-black/20 px-4 py-6 text-center text-xs text-zinc-500">暂无 Plus 校验任务。到账号页选择账号后点击“批量校验 Plus”。</div>
              ) : trackedTasks.map((task) => {
                const state = taskBadge(task)
                const selected = task.task_id === activeTaskId
                return (
                  <button
                    key={task.task_id}
                    type="button"
                    onClick={() => selectTask(task.task_id)}
                    className={`w-full rounded-xl border p-3 text-left transition-colors ${selected ? 'border-cyan-400/50 bg-cyan-950/30' : 'border-white/10 bg-black/20 hover:border-white/20'}`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-zinc-100" title={task.label}>{task.label}</div>
                        <div className="mt-1 truncate font-mono text-[11px] text-zinc-500" title={task.task_id}>{task.task_id}</div>
                      </div>
                      <Badge variant={state.variant}>{state.label}</Badge>
                    </div>
                    <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-zinc-900">
                      <div className="h-full rounded-full bg-cyan-300" style={{ width: `${progressPercent(task)}%` }} />
                    </div>
                    <div className="mt-2 flex items-center justify-between text-[11px] text-zinc-500">
                      <span>{task.completed}/{task.total || '—'} · Plus {task.paid} · 失败 {task.failed}</span>
                      <span>{formatDate(task.updated_at)}</span>
                    </div>
                  </button>
                )
              })}
            </div>
          </CardContent>
        </Card>

        <div className="space-y-4">
          {error && (
            <div role="alert" className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
              {error}
              <button className="ml-3 underline" onClick={() => setError(null)}>关闭</button>
            </div>
          )}
          {message && (
            <div role="status" className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-300">
              {message}
              <button className="ml-3 underline" onClick={() => setMessage(null)}>关闭</button>
            </div>
          )}

          <Card className="overflow-hidden border-fuchsia-400/20 bg-[radial-gradient(circle_at_top_left,rgba(134,25,143,0.24),rgba(9,9,11,0.96)_45%)]">
            <CardContent className="space-y-4 p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="truncate text-lg font-semibold text-zinc-100">{activeBatch?.name || activeBatchKey || '未选择 Plus 批次'}</h3>
                    <Badge variant={activeBatchStats.variant}>{activeBatchStats.label}</Badge>
                    {batchLoading && <RefreshCw size={15} className="animate-spin text-fuchsia-300" />}
                  </div>
                  <p className="mt-1 max-w-3xl text-sm leading-6 text-zinc-400">
                    {activeBatch ? `${activeBatch.batch_key} · ${activeBatch.total_count} 个账号 · 进度 ${activeBatch.progress_percent}%` : '选择左侧批次查看批次内账号状态；列表不再把 400 个账号平铺成 400 行。'}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <select value={batchItemFilter} onChange={(event) => setBatchItemFilter(event.target.value)} className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-fuchsia-600">
                    <option value="">全部明细状态</option>
                    <option value="queued,submitting,submit_unknown,submitted,processing,verifying">进行中</option>
                    <option value="verified">可导出</option>
                    <option value="failed,releasable">失败/可释放</option>
                    <option value="exported,archived">已导出/归档</option>
                  </select>
                  <Button variant="outline" onClick={handleRefreshBatch} disabled={!activeBatchKey || batchActionLoading}>刷新批次</Button>
                  <Button variant="outline" onClick={handleRetryBatch} disabled={!activeBatchKey || batchActionLoading}>重试批次失败</Button>
                  <Button variant="outline" onClick={handleReleaseBatch} disabled={!activeBatchKey || batchActionLoading || batchReleasableCount === 0}>释放 {batchReleasableCount}</Button>
                  <Button variant="outline" onClick={handleShowBatchAccounts} disabled={!activeBatchKey || batchActionLoading} title="只恢复账号列表显示，不释放远端 UPI 任务">
                    <Eye size={15} /> 显示到账号列表
                  </Button>
                  <Button onClick={handleExportBatch} disabled={!activeBatchKey || batchActionLoading || !activeBatch || batchExportableCount === 0} title={activeBatch?.verified_count ? '导出未导出的 Plus 成品号' : '重新导出已导出/已归档的 Plus 成品号'}>
                    <Download size={15} /> {activeBatch?.verified_count ? '导出 Plus' : '重新导出 Plus'}
                  </Button>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-6">
                {[
                  { label: '总数', value: activeBatch?.total_count ?? '—', tone: 'text-zinc-100' },
                  { label: '进行中', value: activeBatch ? activeBatch.queued_count + activeBatch.submitting_count + activeBatch.submit_unknown_count + activeBatch.submitted_count + activeBatch.processing_count + activeBatch.verifying_count : '—', tone: 'text-amber-300' },
                  { label: '已开通', value: activeBatch?.verified_count ?? '—', tone: 'text-emerald-300' },
                  { label: '失败/可释放', value: activeBatch ? activeBatch.failed_count + activeBatch.releasable_count : '—', tone: 'text-red-300' },
                  { label: '已导出', value: activeBatch ? activeBatch.exported_count + activeBatch.archived_count : '—', tone: 'text-fuchsia-300' },
                  { label: 'CDK核销', value: activeBatch?.cdk_consumed_count ?? '—', tone: 'text-cyan-300' },
                ].map((metric) => (
                  <div key={metric.label} className="rounded-xl border border-white/10 bg-black/25 p-3">
                    <div className="text-[11px] uppercase tracking-[0.16em] text-zinc-600">{metric.label}</div>
                    <div className={`mt-1 font-mono text-xl font-semibold tabular-nums ${metric.tone}`}>{metric.value}</div>
                  </div>
                ))}
              </div>

              {activeBatch && (
                <div>
                  <div className="mb-2 flex items-center justify-between text-xs text-zinc-500">
                    <span>批次进度 {activeBatch.progress_percent}% · 成功率 {activeBatch.success_rate_percent}%</span>
                    <span>创建 {formatDate(activeBatch.created_at)} · 更新 {formatDate(activeBatch.updated_at)}</span>
                  </div>
                  <div className="h-3 overflow-hidden rounded-full bg-zinc-900 ring-1 ring-white/10">
                    <div className="h-full rounded-full bg-[linear-gradient(90deg,#d946ef,#22d3ee,#34d399)] transition-all" style={{ width: `${Math.max(0, Math.min(100, Number(activeBatch.progress_percent || 0)))}%` }} />
                  </div>
                </div>
              )}

              {!activeBatchKey ? (
                <div className="rounded-lg border border-dashed border-white/10 bg-black/20 px-4 py-8 text-center text-sm text-zinc-500">请选择一个 Plus 批次。</div>
              ) : batchVisibleItems.length === 0 ? (
                <div className="rounded-lg border border-dashed border-white/10 bg-black/20 px-4 py-8 text-center text-sm text-zinc-500">当前筛选下没有批次明细。</div>
              ) : (
                <div className="max-h-96 overflow-auto rounded-xl border border-white/10">
                  <table className="w-full min-w-[980px] text-sm">
                    <thead>
                      <tr className="border-b border-white/5 bg-black/20 text-left text-xs uppercase tracking-[0.12em] text-zinc-500">
                        <th className="px-4 py-3 font-medium">账号</th>
                        <th className="px-4 py-3 font-medium">批次状态</th>
                        <th className="px-4 py-3 font-medium">远端任务</th>
                        <th className="px-4 py-3 font-medium">重试</th>
                        <th className="px-4 py-3 font-medium">提示</th>
                        <th className="px-4 py-3 font-medium">更新时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {batchVisibleItems.map((item) => {
                        const state = activationBadge(item.status)
                        return (
                          <tr key={item.item_key} className="border-b border-white/5 last:border-0">
                            <td className="max-w-[240px] truncate px-4 py-3" title={item.email || item.account_key}>{item.email || item.account_key}</td>
                            <td className="px-4 py-3"><Badge variant={state.variant}>{state.label}</Badge></td>
                            <td className="max-w-[180px] truncate px-4 py-3 font-mono text-xs text-zinc-500" title={item.remote_task_id || ''}>{item.remote_task_id || '—'}</td>
                            <td className="px-4 py-3 text-zinc-400">{item.retry_count || 0}</td>
                            <td className="max-w-[300px] truncate px-4 py-3 text-zinc-400" title={item.activation_error || item.activation_display || ''}>{item.activation_display || item.activation_error || '—'}</td>
                            <td className="px-4 py-3 text-xs text-zinc-500">{formatDate(item.updated_at)}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="overflow-hidden border-emerald-400/20 bg-[linear-gradient(135deg,rgba(6,78,59,0.28),rgba(9,9,11,0.96)_44%)]">
            <CardContent className="space-y-4 p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-lg font-semibold text-zinc-100">UPI 开通进度</h3>
                    <Badge variant={activationStats?.config.enabled && activationStats.config.has_key ? 'success' : 'warning'}>
                      {activationStats?.config.enabled && activationStats.config.has_key ? '可提交' : '需配置'}
                    </Badge>
                    {activationLoading && <RefreshCw size={15} className="animate-spin text-emerald-300" />}
                  </div>
                  <p className="mt-1 text-sm leading-6 text-zinc-400">按账号公开投影显示 UPI 远端任务、CDK 消耗和释放状态；不暴露 actk/accessToken。</p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <select value={activationFilter} onChange={(event) => setActivationFilter(event.target.value)} className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-emerald-600">
                    <option value="">全部开通状态</option>
                    <option value="queued,submit_unknown,submitted,processing,verifying,success">进行中/待验收</option>
                    <option value="failed,expired,replace_account">失败/过期/需换号</option>
                    <option value="cancelled,released">已取消/已释放</option>
                  </select>
                  <Button variant="outline" onClick={() => loadActivationProgress(false)} disabled={activationLoading || activationActionLoading}>刷新列表</Button>
                  <Button variant="outline" onClick={handleRefreshActivation} disabled={activationActionLoading}>轮询远端</Button>
                  <Button variant="outline" onClick={handleRetryActivationFailed} disabled={activationActionLoading}>重试失败</Button>
                  <Button variant="outline" onClick={handleReleaseReleasableActivations} disabled={activationActionLoading || !activationTasks.some((account) => account.activation_can_release)}>释放可释放</Button>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-4">
                {[
                  { label: '列表', value: activationTasks.length, tone: 'text-zinc-100' },
                  { label: '活动', value: activationStats?.active ?? '—', tone: 'text-amber-300' },
                  { label: '成功', value: Number(activationStats?.counts.success || 0) + Number(activationStats?.counts.verified || 0) + Number(activationStats?.counts.active || 0), tone: 'text-emerald-300' },
                  { label: '失败', value: Number(activationStats?.counts.failed || 0) + Number(activationStats?.counts.replace_account || 0), tone: 'text-red-300' },
                ].map((metric) => (
                  <div key={metric.label} className="rounded-xl border border-white/10 bg-black/25 p-3">
                    <div className="text-[11px] uppercase tracking-[0.16em] text-zinc-600">{metric.label}</div>
                    <div className={`mt-1 font-mono text-xl font-semibold tabular-nums ${metric.tone}`}>{metric.value}</div>
                  </div>
                ))}
              </div>

              {activationTasks.length === 0 ? (
                <div className="rounded-lg border border-dashed border-white/10 bg-black/20 px-4 py-8 text-center text-sm text-zinc-500">暂无 UPI 开通任务。</div>
              ) : (
                <div className="max-h-80 overflow-auto rounded-xl border border-white/10">
                  <table className="w-full min-w-[860px] text-sm">
                    <thead>
                      <tr className="border-b border-white/5 bg-black/20 text-left text-xs uppercase tracking-[0.12em] text-zinc-500">
                        <th className="px-4 py-3 font-medium">账号</th>
                        <th className="px-4 py-3 font-medium">状态</th>
                        <th className="px-4 py-3 font-medium">任务</th>
                        <th className="px-4 py-3 font-medium">CDK</th>
                        <th className="px-4 py-3 font-medium">提示</th>
                        <th className="px-4 py-3 font-medium">更新时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activationTasks.map((account) => {
                        const state = activationBadge(account.activation_status)
                        return (
                          <tr key={account.key} className="border-b border-white/5 last:border-0">
                            <td className="max-w-[240px] truncate px-4 py-3" title={accountLabel(account)}>{accountLabel(account)}</td>
                            <td className="px-4 py-3"><Badge variant={state.variant}>{state.label}</Badge></td>
                            <td className="max-w-[180px] truncate px-4 py-3 font-mono text-xs text-zinc-500" title={account.activation_task_id || ''}>{account.activation_task_id || '—'}</td>
                            <td className="px-4 py-3 text-zinc-400">{account.activation_cdk_consumed ? '已核销' : account.activation_can_release ? '可释放' : '未核销/未知'}</td>
                            <td className="max-w-[260px] truncate px-4 py-3 text-zinc-400" title={account.activation_error || account.activation_display || ''}>{account.activation_error || account.activation_display || '—'}</td>
                            <td className="px-4 py-3 text-xs text-zinc-500">{formatDate(account.activation_updated_at)}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="overflow-hidden border-cyan-400/20 bg-[linear-gradient(135deg,rgba(8,47,73,0.34),rgba(9,9,11,0.96)_42%)]">
            <CardContent className="space-y-5 p-5">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="truncate text-lg font-semibold text-zinc-100">{activeTaskId || '未选择任务'}</h3>
                    <Badge variant={badge.variant}>{badge.label}</Badge>
                    {loading && <RefreshCw size={15} className="animate-spin text-cyan-300" />}
                  </div>
                  <p className="mt-1 max-w-3xl text-sm leading-6 text-zinc-400">{visibleProgress?.message || '从左侧选择任务，或粘贴 plus-verify-* 任务号开始跟踪。'}</p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" onClick={() => activeTaskId && loadTask(activeTaskId)} disabled={!activeTaskId || loading}>
                    <RefreshCw size={15} /> 刷新
                  </Button>
                  <Button variant="outline" onClick={handleCancel} disabled={!progress?.running || progress.cancelled || actionLoading}>
                    <XCircle size={15} /> 取消
                  </Button>
                  <Button variant="ghost" onClick={() => activeTaskId && handleForgetTask(activeTaskId)} disabled={!activeTaskId || actionLoading}>
                    <Trash2 size={15} /> 移除记录
                  </Button>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-5">
                {[
                  { label: '总数', value: visibleProgress?.total ?? '—', icon: ListChecks, tone: 'text-zinc-100' },
                  { label: '已完成', value: visibleProgress?.completed ?? '—', icon: CheckCircle2, tone: 'text-cyan-200' },
                  { label: 'Plus/Team', value: visibleProgress?.paid ?? '—', icon: ShieldCheck, tone: 'text-emerald-300' },
                  { label: '失败', value: visibleProgress?.failed ?? '—', icon: XCircle, tone: 'text-red-300' },
                  { label: '运行中', value: inFlightCount || (visibleProgress?.running ? '启动中' : 0), icon: Clock, tone: 'text-amber-300' },
                ].map(({ label, value, icon: Icon, tone }) => (
                  <div key={label} className="rounded-xl border border-white/10 bg-black/25 p-4">
                    <div className="flex items-center gap-2 text-xs uppercase tracking-[0.16em] text-zinc-600"><Icon size={14} /> {label}</div>
                    <div className={`mt-2 font-mono text-2xl font-semibold tabular-nums ${tone}`}>{value}</div>
                  </div>
                ))}
              </div>

              <div>
                <div className="mb-2 flex items-center justify-between text-xs text-zinc-500">
                  <span>进度 {progressPercent(visibleProgress)}%</span>
                  <span>待处理 {pendingCount} · 运行中 {inFlightCount}</span>
                </div>
                <div className="h-3 overflow-hidden rounded-full bg-zinc-900 ring-1 ring-white/10">
                  <div className="h-full rounded-full bg-[linear-gradient(90deg,#22d3ee,#34d399)] transition-all" style={{ width: `${progressPercent(visibleProgress)}%` }} />
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-white/10 bg-black/20 px-4 py-3">
                <div>
                  <div className="text-sm font-medium text-zinc-100">失败项重试</div>
                  <p className="mt-1 text-xs text-zinc-500">用当前任务失败结果的账号 key 创建新的异步校验任务。</p>
                </div>
                <div className="flex items-center gap-2">
                  <select
                    value={retryRegion}
                    onChange={(event) => setRetryRegion(event.target.value as 'JP' | 'VN')}
                    className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-cyan-600"
                  >
                    <option value="JP">出口 JP</option>
                    <option value="VN">出口 VN</option>
                  </select>
                  <Button onClick={handleRetryFailed} disabled={failedResults.length === 0 || actionLoading}>
                    <RotateCcw size={15} /> 重试失败 {failedResults.length}
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-0">
              {latestResults.length === 0 ? (
                <div className="py-16 text-center text-sm text-zinc-500">暂无结果；任务开始后这里会显示最近 80 条结果。</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[760px] text-sm">
                    <thead>
                      <tr className="border-b border-white/5 text-left text-xs uppercase tracking-[0.12em] text-zinc-500">
                        <th className="px-4 py-3 font-medium">账号</th>
                        <th className="px-4 py-3 font-medium">结果</th>
                        <th className="px-4 py-3 font-medium">计划</th>
                        <th className="px-4 py-3 font-medium">来源</th>
                        <th className="px-4 py-3 font-medium">HTTP</th>
                      </tr>
                    </thead>
                    <tbody>
                      {latestResults.map((item) => (
                        <tr key={`${item.key}:${item.status_code}:${item.message || item.plan_type || ''}`} className="border-b border-white/5 last:border-0">
                          <td className="max-w-[260px] truncate px-4 py-3 font-mono text-xs text-zinc-300" title={item.key}>{item.key}</td>
                          <td className="px-4 py-3">
                            <Badge variant={item.ok ? 'success' : 'danger'}>{resultText(item)}</Badge>
                          </td>
                          <td className="px-4 py-3 text-zinc-400">{item.plan_type || (item.paid ? 'paid' : '—')}</td>
                          <td className="px-4 py-3 text-zinc-400">{item.source || '—'}</td>
                          <td className="px-4 py-3 font-mono text-xs text-zinc-500">{item.status_code || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
