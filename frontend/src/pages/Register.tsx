import { useCallback, useEffect, useRef, useState } from 'react'
import { Cpu, Mail, Phone, Rocket, X } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { getProviders, startRegistration, getRegistrationStatus, cancelRegistration, getTaskBatches, exportTaskBatchesAtTxt } from '@/lib/api'
import type { ProviderInfo, RegistrationRun, RunStatus } from '@/lib/types'

const SMS_COUNTRIES = [
  { value: 'BR', label: '巴西 (+55)' },
  { value: 'US', label: '美国 (+1)' },
  { value: 'IN', label: '印度 (+91)' },
] as const

const PROXY_MODES = [
  { value: 'credentials', label: '账号密码模式' },
  { value: 'api', label: 'API 模式' },
] as const

const PROXY_PROTOCOLS = [
  { value: 'socks5', label: 'SOCKS5（推荐）' },
  { value: 'http', label: 'HTTP CONNECT' },
] as const

const PROXY_REGIONS = [
  { value: 'auto', label: '按账号 Zone 自动（推荐）' },
  { value: 'TR', label: '土耳其 TR' },
  { value: 'BR', label: '巴西 BR' },
  { value: 'VN', label: '越南 VN' },
  { value: 'JP', label: '日本 JP' },
  { value: 'US', label: '美国 US' },
  { value: 'IN', label: '印度 IN' },
] as const

const STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  running: '运行中',
  complete: '已完成',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
}

const MODE_COPY = {
  phone: {
    title: '手机号注册',
    description: '当前主路径。先用手机号注册 ChatGPT，成功后生成手动 Plus 交接文件。',
    badge: '推荐稳定',
  },
  email: {
    title: '邮箱注册',
    description: '邮箱首登注册。只创建账号并保存手动 Plus 交接文件；不会使用绑定手机号池。',
    badge: '邮箱注册',
  },
  email_protocol: {
    title: '邮箱协议注册',
    description: '协议 HTTP 注册。Python 后端固定使用项目内置 Mailat 运行时；也可切换 Go Worker，不影响浏览器邮箱注册。',
    badge: '协议双后端',
  },
} as const

const ENGINE_COPY = {
  simulated: {
    title: '指纹浏览器注册',
    description: 'Camoufox 指纹浏览器真实页面流程：手机号注册、接码、保存原生 storage_state。',
    badge: '指纹浏览器',
  },
  protocol: {
    title: '协议注册（调试）',
    description: '纯协议 POST 路径，仅用于调试；不作为最终试用/指纹账号交付。',
    badge: '调试',
  },
} as const

const PROTOCOL_BACKEND_COPY = {
  python: {
    title: 'Python（项目内置 Mailat/Node）',
    description: '项目内置运行时。每任务使用内部 Mailat 源码与 tsx 启动，成功率基线对照。',
    badge: '内置稳定',
  },
  go: {
    title: 'Go（协议 Worker）',
    description: '调用本机 Go daemon，热 worker + 高并发；需先启动 email-protocol-worker。',
    badge: '高并发',
  },
} as const

type ProtocolBackend = keyof typeof PROTOCOL_BACKEND_COPY

type RegistrationEngine = keyof typeof ENGINE_COPY

type RegisterMode = keyof typeof MODE_COPY

function providerLabel(provider: ProviderInfo): string {
  return provider.definition?.label || provider.provider_name
}

export default function Register() {
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const [mode, setMode] = useState<RegisterMode>('phone')
  const [registrationEngine, setRegistrationEngine] = useState<RegistrationEngine>('simulated')
  const [smsProvider, setSmsProvider] = useState('')
  const [smsCountry, setSmsCountry] = useState('BR')
  const [mailboxProvider, setMailboxProvider] = useState('')
  const [autoBindBillingEmail, setAutoBindBillingEmail] = useState(false)
  const [billingEmailProvider, setBillingEmailProvider] = useState('icloud_api')
  const [proxyMode, setProxyMode] = useState('credentials')
  const [proxyRegion, setProxyRegion] = useState('auto')
  const [proxyCredentialProtocol, setProxyCredentialProtocol] = useState('socks5')
  const [headed, setHeaded] = useState(true)
  const [skipPrecheck, setSkipPrecheck] = useState(false)
  const [forceSignup, setForceSignup] = useState(false)
  const [registerCount, setRegisterCount] = useState(100)
  const [registerThreads, setRegisterThreads] = useState(100)
  const [emailProtocolBackend, setEmailProtocolBackend] = useState<ProtocolBackend>('go')
  const [autoExportAt, setAutoExportAt] = useState(true)
  const [atChunkSize, setAtChunkSize] = useState(50)
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null)
  const [batchProgress, setBatchProgress] = useState<{
    total: number
    succeeded: number
    failed: number
    active: number
    finished: number
    progress_pct: number
  } | null>(null)
  const [exportInfo, setExportInfo] = useState<string | null>(null)
  const [exportingAt, setExportingAt] = useState(false)
  const exportStampRef = useRef<string>('')
  const lastExportFlushRef = useRef<number>(0)
  const exportInFlightRef = useRef(false)
  const exportedTotalRef = useRef<number>(0)

  const [run, setRun] = useState<RegistrationRun | null>(null)
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null)
  const pollRef = useRef<number | null>(null)

  useEffect(() => {
    let cancelled = false
    getProviders()
      .then((items) => {
        if (cancelled) return
        setProviders(items)
        const sms = items.find((p) => p.provider_type === 'sms' && p.provider_name === 'herosms_api') || items.find((p) => p.provider_type === 'sms')
        const mailbox = items.find((p) => p.provider_type === 'mailbox' && p.provider_name === 'outlook_token') || items.find((p) => p.provider_type === 'mailbox')
        if (sms) setSmsProvider(sms.provider_name)
        if (mailbox) setMailboxProvider(mailbox.provider_name)
      })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : '服务商加载失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearTimeout(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const flushAtExport = useCallback(
    async (batchId: string) => {
      if (!autoExportAt || !batchId) return
      const now = Date.now()
      if (exportInFlightRef.current || now - lastExportFlushRef.current < 2500) return
      exportInFlightRef.current = true
      lastExportFlushRef.current = now
      setExportingAt(true)
      try {
        if (!exportStampRef.current) {
          exportStampRef.current = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
        }
        const result = await exportTaskBatchesAtTxt({
          batchIds: [batchId],
          onlySucceeded: true,
          chunkSize: Math.max(1, Math.min(10000, atChunkSize || 50)),
          stamp: exportStampRef.current,
          onlyUnexported: true,
        })
        const neu = Number(result.new_count ?? result.count ?? 0)
        if (neu > 0) {
          exportedTotalRef.current += neu
        }
        const files = result.files || []
        const fileHint = files.length
          ? files.map((f) => f.name).slice(-3).join(', ')
          : ''
        setExportInfo(
          [
            result.message || `已增量导出 AT`,
            `累计写出约 ${exportedTotalRef.current} 条`,
            result.dir ? `目录 ${result.dir}` : '',
            fileHint ? `文件 ${fileHint}` : '',
          ]
            .filter(Boolean)
            .join(' · '),
        )
      } catch (err) {
        setExportInfo(err instanceof Error ? `AT 导出：${err.message}` : 'AT 导出失败')
      } finally {
        exportInFlightRef.current = false
        setExportingAt(false)
      }
    },
    [autoExportAt, atChunkSize],
  )

  const pollStatus = useCallback(
    (runId: string, batchId?: string) => {
      stopPolling()
      const bid = (batchId || activeBatchId || '').trim()
      const tick = async () => {
        try {
          // Prefer batch aggregate when we have batch_id (true multi-task progress).
          if (bid) {
            const batchesRes = await getTaskBatches({ limit: 50 })
            const row = (batchesRes.batches || []).find((b) => b.batch_id === bid)
            if (row) {
              const total = Number(row.total || 0)
              const succeeded = Number(row.succeeded || 0)
              const failed = Number(row.failed || 0)
              const active = Number(row.active || 0)
              const finished = Number(row.finished ?? succeeded + failed + Number(row.cancelled || 0))
              const progress = Number(row.progress_pct ?? (total ? (finished / total) * 100 : 0))
              setBatchProgress({
                total,
                succeeded,
                failed,
                active,
                finished,
                progress_pct: progress,
              })
              setRunStatus({
                run_id: runId,
                status: active > 0 || finished < total ? 'running' : succeeded > 0 ? 'complete' : 'failed',
                message: `批次 ${bid}：成功 ${succeeded} / 失败 ${failed} / 进行中 ${active} / 共 ${total}`,
              } as RunStatus)

              // Realtime: every poll flush newly succeeded ATs to disk.
              if (autoExportAt && succeeded > 0) {
                void flushAtExport(bid)
              }

              if (finished >= total && total > 0 && active <= 0) {
                // final flush then stop
                if (autoExportAt) {
                  await flushAtExport(bid)
                }
                stopPolling()
                return
              }
            } else {
              // batch row not visible yet — fall back to single-task status
              const status = await getRegistrationStatus(runId)
              setRunStatus(status)
            }
          } else {
            const status = await getRegistrationStatus(runId)
            setRunStatus(status)
            if (status.status === 'complete' || status.status === 'failed' || status.status === 'cancelled') {
              stopPolling()
              return
            }
          }
        } catch {
          // 网络瞬断时继续下一轮轮询。
        }
        pollRef.current = window.setTimeout(tick, 2500)
      }
      pollRef.current = window.setTimeout(tick, 0)
    },
    [stopPolling, activeBatchId, autoExportAt, flushAtExport],
  )

  useEffect(() => {
    return () => stopPolling()
  }, [stopPolling])

  const smsProviders = providers.filter((p) => p.provider_type === 'sms' && p.provider_name !== 'bind_user_phone_url')
  const mailboxProviders = providers.filter((p) => p.provider_type === 'mailbox')

  useEffect(() => {
    if (smsProvider === 'bind_user_phone_url' || (smsProvider && !smsProviders.some((provider) => provider.provider_name === smsProvider))) {
      setSmsProvider(smsProviders[0]?.provider_name ?? '')
    }
  }, [smsProvider, smsProviders])

  useEffect(() => {
    if (mode === 'phone' && registrationEngine === 'protocol') {
      const preferred = mailboxProviders.find((provider) => provider.provider_name === 'forwarded_domain') || mailboxProviders.find((provider) => provider.provider_name === 'cfworker_admin_api')
      if (preferred && mailboxProvider !== preferred.provider_name) setMailboxProvider(preferred.provider_name)
    }
    if (mode === 'email_protocol') {
      const preferred =
        mailboxProviders.find((provider) => provider.provider_name === 'outlook_token') ||
        mailboxProviders.find((provider) => provider.provider_name === 'icloud_api') ||
        mailboxProviders[0]
      if (preferred && mailboxProvider !== preferred.provider_name) setMailboxProvider(preferred.provider_name)
    }
  }, [mode, registrationEngine, mailboxProvider, mailboxProviders])
  const needsMailbox = mode === 'email' || mode === 'email_protocol' || (mode === 'phone' && registrationEngine === 'protocol')

  const handleStart = async () => {
    setSubmitting(true)
    setError(null)
    setExportInfo(null)
    setBatchProgress(null)
    exportedTotalRef.current = 0
    lastExportFlushRef.current = 0
    exportStampRef.current = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
    try {
      if (needsMailbox && !mailboxProvider) {
        throw new Error(registrationEngine === 'protocol' ? '协议手机号注册需要选择邮箱服务商，用于补绑邮箱' : '邮箱注册需要选择邮箱服务商')
      }
      const started = await startRegistration({
        mode: mode === 'email_protocol' ? 'email_protocol' : registrationEngine === 'protocol' && mode === 'phone' ? 'phone_protocol' : mode,
        registration_engine: registrationEngine,
        sms_provider: mode === 'phone' ? smsProvider || undefined : undefined,
        sms_country: mode === 'phone' ? smsCountry || undefined : undefined,
        mailbox_provider: needsMailbox ? mailboxProvider || undefined : undefined,
        auto_bind_billing_email: mode === 'phone' && registrationEngine === 'simulated' ? autoBindBillingEmail : undefined,
        billing_email_provider: mode === 'phone' && registrationEngine === 'simulated' ? billingEmailProvider : undefined,
        proxy_mode: proxyMode || undefined,
        proxy_region: proxyRegion || undefined,
        lajiao_proxy_credential_protocol: proxyMode === 'credentials' ? proxyCredentialProtocol : undefined,
        browser_no_viewport: mode === 'email' ? true : undefined,
        email_register_flow: mode === 'email' ? 'fast' : undefined,
        email_otp_timeout: mode === 'email' || mode === 'email_protocol' ? 200 : undefined,
        email_otp_poll_interval: mode === 'email' || mode === 'email_protocol' ? 3 : undefined,
        mailat_protocol_use_local_bridge: mode === 'email_protocol' ? true : undefined,
        mailat_protocol_timeout_seconds: mode === 'email_protocol' ? 900 : undefined,
        mailat_protocol_proxy_precheck_enabled: mode === 'email_protocol' ? true : undefined,
        mailat_protocol_proxy_attempts: mode === 'email_protocol' ? 6 : undefined,
        mailat_protocol_proxy_preflight_timeout_seconds: mode === 'email_protocol' ? 12 : undefined,
        email_protocol_backend: mode === 'email_protocol' ? emailProtocolBackend : undefined,
        go_email_protocol_timeout_seconds: mode === 'email_protocol' && emailProtocolBackend === 'go' ? 900 : undefined,
        headed: headed || undefined,
        skip_precheck: skipPrecheck || undefined,
        force_signup: forceSignup || undefined,
        register_count: registerCount,
        register_threads: registerThreads,
      })
      const batchId = String(started.batch_id || '').trim()
      setActiveBatchId(batchId || null)
      setRun({ run_id: started.run_id, status: 'pending' } as RegistrationRun)
      setRunStatus({
        run_id: started.run_id,
        status: 'pending',
        message: started.message || (started.async_create
          ? `已接收 ${started.accepted || started.count || registerCount} 个任务，后台入队中`
          : '注册任务已创建'),
      } as RunStatus)
      if (autoExportAt) {
        setExportInfo(`实时 AT 导出已开启 → at-file/${exportStampRef.current}/ （每 ${Math.max(1, atChunkSize)} 条一个文件，成功即写）`)
      }
      pollStatus(started.run_id, batchId || undefined)
    } catch (err) {
      setError(err instanceof Error ? err.message : '注册启动失败')
    } finally {
      setSubmitting(false)
    }
  }

  const handleCancel = async () => {
    if (!run) return
    try {
      await cancelRegistration(run.run_id)
      stopPolling()
      if (runStatus) setRunStatus({ ...runStatus, status: 'cancelled' })
    } catch (err) {
      setError(err instanceof Error ? err.message : '取消失败')
    }
  }

  const handleReset = () => {
    stopPolling()
    setRun(null)
    setRunStatus(null)
    setActiveBatchId(null)
    setBatchProgress(null)
    setExportInfo(null)
    exportedTotalRef.current = 0
    exportStampRef.current = ''
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="text-zinc-500 animate-pulse">正在加载服务商…</div>
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-xl font-semibold text-zinc-100">注册</h2>
        <p className="text-sm text-zinc-400 mt-1">
          启动新的账号注册任务。邮箱注册是独立板块，会复用接码、代理和任务队列。
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {!run && (
        <Card>
          <CardHeader>
            <CardTitle>新建注册任务</CardTitle>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="grid gap-4 lg:grid-cols-3">
              {(['phone', 'email', 'email_protocol'] as RegisterMode[]).map((item) => {
                const active = mode === item
                const Icon = item === 'phone' ? Phone : item === 'email_protocol' ? Cpu : Mail
                return (
                  <button
                    key={item}
                    type="button"
                    onClick={() => setMode(item)}
                    aria-pressed={active}
                    aria-label={`切换到${MODE_COPY[item].title}`}
                    className={`rounded-2xl border p-4 text-left transition-colors ${active ? 'border-blue-500/60 bg-blue-500/10' : 'border-white/10 bg-zinc-950/40 hover:border-white/20 hover:bg-white/5'}`}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex gap-3">
                        <span className={`mt-0.5 rounded-xl border p-2 ${active ? 'border-blue-400/30 bg-blue-500/20 text-blue-200' : 'border-white/10 bg-zinc-900 text-zinc-400'}`}>
                          <Icon className="h-5 w-5" />
                        </span>
                        <span>
                          <span className="block text-sm font-semibold text-zinc-100">{MODE_COPY[item].title}</span>
                          <span className="mt-1 block text-xs leading-relaxed text-zinc-500">{MODE_COPY[item].description}</span>
                        </span>
                      </div>
                      <Badge variant={active ? 'success' : 'default'}>{MODE_COPY[item].badge}</Badge>
                    </div>
                  </button>
                )
              })}
            </div>

            {mode === 'phone' && (
              <div className="grid gap-4 lg:grid-cols-2">
                {(['simulated', 'protocol'] as RegistrationEngine[]).map((item) => {
                  const active = registrationEngine === item
                  return (
                    <button
                      key={item}
                      type="button"
                      onClick={() => setRegistrationEngine(item)}
                      aria-pressed={active}
                      aria-label={`切换到${ENGINE_COPY[item].title}`}
                      className={`rounded-2xl border p-4 text-left transition-colors ${active ? 'border-emerald-500/60 bg-emerald-500/10' : 'border-white/10 bg-zinc-950/40 hover:border-white/20 hover:bg-white/5'}`}
                    >
                      <div className="flex items-start justify-between gap-4">
                        <span>
                          <span className="block text-sm font-semibold text-zinc-100">{ENGINE_COPY[item].title}</span>
                          <span className="mt-1 block text-xs leading-relaxed text-zinc-500">{ENGINE_COPY[item].description}</span>
                        </span>
                        <Badge variant={active ? 'success' : 'default'}>{ENGINE_COPY[item].badge}</Badge>
                      </div>
                    </button>
                  )
                })}
              </div>
            )}

            {mode === 'email_protocol' && (
              <div className="space-y-4">
                <div className="rounded-2xl border border-cyan-400/20 bg-cyan-400/5 p-4 text-sm text-cyan-50">
                  <div className="font-semibold">协议注册板块</div>
                  <p className="mt-1 text-xs leading-relaxed text-cyan-100/70">
                    Python 后端固定使用项目内置 Mailat 运行时，由邮箱池和代理池提供资源，并保存交接文件；不读取外部 Mailat 目录。
                    也可在下方切换 Go 协议后端，详见 docs/EMAIL_PROTOCOL_GO_PLAN.md。
                  </p>
                </div>
                <div className="grid gap-4 lg:grid-cols-2">
                  {(['python', 'go'] as ProtocolBackend[]).map((item) => {
                    const active = emailProtocolBackend === item
                    return (
                      <button
                        key={item}
                        type="button"
                        onClick={() => setEmailProtocolBackend(item)}
                        className={`rounded-2xl border p-4 text-left transition-colors ${active ? 'border-cyan-400/60 bg-cyan-400/10' : 'border-white/10 bg-zinc-950/40 hover:border-white/20 hover:bg-white/5'}`}
                      >
                        <div className="flex items-start justify-between gap-4">
                          <span>
                            <span className="block text-sm font-semibold text-zinc-100">{PROTOCOL_BACKEND_COPY[item].title}</span>
                            <span className="mt-1 block text-xs leading-relaxed text-zinc-500">{PROTOCOL_BACKEND_COPY[item].description}</span>
                          </span>
                          <Badge variant={active ? 'success' : 'default'}>{PROTOCOL_BACKEND_COPY[item].badge}</Badge>
                        </div>
                      </button>
                    )
                  })}
                </div>
                {emailProtocolBackend === 'go' && (
                  <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-amber-100/90">
                    Go 后端默认连接 <code className="text-amber-50">http://127.0.0.1:18765</code>。daemon 未启动时任务会失败；可随时切回 Python。
                  </div>
                )}
              </div>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              {mode === 'phone' && (
                <>
                  <div className="space-y-1.5">
                    <label className="text-sm text-zinc-400">注册接码服务商</label>
                    <Select value={smsProvider} onValueChange={setSmsProvider}>
                      <SelectTrigger className="w-full">
                        <SelectValue placeholder="选择注册接码服务商" />
                      </SelectTrigger>
                      <SelectContent>
                        {smsProviders.map((provider) => (
                          <SelectItem key={provider.provider_name} value={provider.provider_name}>
                            {providerLabel(provider)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className="text-xs text-zinc-500">只用于手机号注册；Plus 后绑定手机号池在账号页单独使用。</p>
                  </div>

                  <div className="space-y-1.5">
                    <label className="text-sm text-zinc-400">注册手机号国家/地区</label>
                    <Select value={smsCountry} onValueChange={setSmsCountry}>
                      <SelectTrigger className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {SMS_COUNTRIES.map((country) => (
                          <SelectItem key={country.value} value={country.value}>
                            {country.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5 sm:col-span-2 rounded-lg border border-white/10 bg-black/20 p-3">
                    <label className="flex items-center gap-2 text-sm text-zinc-300">
                      <input
                        type="checkbox"
                        checked={autoBindBillingEmail}
                        onChange={(e) => setAutoBindBillingEmail(e.target.checked)}
                        className="h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
                      />
                      注册成功后自动启动账单邮箱绑定（显示窗口）
                    </label>
                    <div className="mt-3 space-y-1.5">
                      <label className="text-sm text-zinc-400">账单邮箱服务商</label>
                      <Select value={billingEmailProvider} onValueChange={setBillingEmailProvider}>
                        <SelectTrigger className="w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="icloud_api">iCloud API 邮箱</SelectItem>
                          <SelectItem value="icloud_privacy">iCloud 转发邮箱（旧）</SelectItem>
                          <SelectItem value="forwarded_domain">转发域名</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-zinc-500">仅用于注册完成后的账单邮箱绑定任务；手机号注册本身仍使用 HeroSMS。</p>
                    </div>
                  </div>
                </>
              )}

              {needsMailbox && (
                <div className="space-y-1.5 sm:col-span-2">
                  <label className="text-sm text-zinc-400">邮箱服务商</label>
                  <Select value={mailboxProvider} onValueChange={setMailboxProvider}>
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="选择邮箱服务商" />
                    </SelectTrigger>
                    <SelectContent>
                      {mailboxProviders.map((provider) => (
                        <SelectItem key={provider.provider_name} value={provider.provider_name}>
                          {providerLabel(provider)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-zinc-500">{registrationEngine === 'protocol' && mode === 'phone' ? '协议手机号注册会在手机号注册后补绑邮箱；邮箱已关联时会换下一个邮箱。' : '邮箱注册只使用邮箱资源；不会显示或租用接码手机号。'}</p>
                </div>
              )}

              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">代理模式</label>
                <Select value={proxyMode} onValueChange={setProxyMode}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PROXY_MODES.map((item) => (
                      <SelectItem key={item.value} value={item.value}>
                        {item.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">出口校验地区</label>
                <Select value={proxyRegion} onValueChange={setProxyRegion}>
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="选择出口校验地区" />
                  </SelectTrigger>
                  <SelectContent>
                    {PROXY_REGIONS.map((region) => (
                      <SelectItem key={region.value} value={region.value}>
                        {region.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {proxyMode === 'credentials' && (
                <div className="space-y-1.5">
                  <label className="text-sm text-zinc-400">账号密码代理协议</label>
                  <Select value={proxyCredentialProtocol} onValueChange={setProxyCredentialProtocol}>
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {PROXY_PROTOCOLS.map((protocol) => (
                        <SelectItem key={protocol.value} value={protocol.value}>
                          {protocol.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-zinc-500">账密代理统一进入代理池；认证 SOCKS5/HTTP 上游都会通过本地 bridge 给浏览器使用。</p>

                </div>
              )}

              {mode === 'email_protocol' && (
                <div className="space-y-1.5 sm:col-span-2 rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 text-xs text-emerald-200">
                  {proxyRegion === 'auto'
                    ? '协议注册会从代理账号的 zone_XX / custom_zone_XX / region-XX 推导并校验真实出口国家；账号没有标记时不强制国家。'
                    : `协议注册会校验出口 IP、OpenAI/ChatGPT 可达性，并强制要求真实出口为 ${proxyRegion}。`}
                </div>
              )}

              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">注册数量</label>
                <Input
                  type="number"
                  min={1}
                  value={registerCount}
                  onChange={(event) => setRegisterCount(Math.max(1, Number(event.target.value) || 1))}
                />
                <p className="text-xs text-zinc-500">本次要启动几个注册任务（不设上限，按你填的数量入队）。</p>
              </div>

              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">注册并发（线程）</label>
                <Input
                  type="number"
                  min={1}
                  value={registerThreads}
                  onChange={(event) => setRegisterThreads(Math.max(1, Number(event.target.value) || 1))}
                />
                <p className="text-xs text-zinc-500">
                  同时跑几个注册。日常推荐 <span className="text-zinc-300">100</span>（压测金线）；
                  <span className="text-zinc-300">不设硬上限</span>，提交后写入 max_register_tasks / max_parallel_tasks 并抬高 Go worker 座位。
                  高并发请自己控节奏，避免一次齐射把代理/TUN 打崩。
                </p>
              </div>

              <div className="sm:col-span-2 space-y-3 rounded-xl border border-cyan-500/20 bg-cyan-500/5 p-4">
                <label className="flex items-center gap-2 text-sm text-zinc-200 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={autoExportAt}
                    onChange={(event) => setAutoExportAt(event.target.checked)}
                    className="rounded border-white/10 bg-zinc-900 accent-cyan-600"
                  />
                  实时自动导出 AT 到 at-file/
                </label>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <label className="text-sm text-zinc-400">每个文件账号数（分割）</label>
                    <Input
                      type="number"
                      min={1}
                      max={10000}
                      value={atChunkSize}
                      onChange={(event) => setAtChunkSize(Math.max(1, Math.min(10000, Number(event.target.value) || 50)))}
                      disabled={!autoExportAt}
                    />
                    <p className="text-xs text-zinc-500">
                      格式与账号页 AT 成品一致：email----password----client_id----refresh_token----access_token。
                      目录：at-file/任务时间/；文件名 at-products-batch-N-pPart-时间.txt。
                      <span className="text-cyan-300"> 成功多少就写出多少，不用等整批结束。</span>
                    </p>
                  </div>
                  <div className="space-y-1 text-xs text-zinc-400">
                    <div>当前任务时间戳：{exportStampRef.current || '（开始后生成）'}</div>
                    <div>累计已写：{exportedTotalRef.current}</div>
                    {exportingAt && <div className="text-cyan-300">正在写出新增 AT…</div>}
                  </div>
                </div>
              </div>
              <div className="sm:col-span-2 grid gap-3 rounded-xl border border-white/10 bg-zinc-950/40 p-4 sm:grid-cols-3">
                <label className="flex items-center gap-2 text-sm text-zinc-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={headed}
                    onChange={(event) => setHeaded(event.target.checked)}
                    className="rounded border-white/10 bg-zinc-900 accent-blue-600"
                  />
                  {mode === 'email' ? '显示浏览器窗口（Patchright 快速邮箱注册建议保持开启）' : mode === 'email_protocol' ? '邮箱协议注册不启动浏览器，此项会被忽略' : '显示浏览器窗口'}
                </label>
                {mode === 'phone' && (
                  <label className="flex items-center gap-2 text-sm text-zinc-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={skipPrecheck}
                      onChange={(event) => setSkipPrecheck(event.target.checked)}
                      className="rounded border-white/10 bg-zinc-900 accent-blue-600"
                    />
                    跳过手机号预检
                  </label>
                )}
                {mode === 'phone' && (
                  <label className="flex items-center gap-2 text-sm text-zinc-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={forceSignup}
                      onChange={(event) => setForceSignup(event.target.checked)}
                      className="rounded border-white/10 bg-zinc-900 accent-blue-600"
                    />
                    从密码页强制注册
                  </label>
                )}
              </div>

              <div className="sm:col-span-2 flex flex-wrap items-center gap-3">
                <Button
                  variant="accent"
                  onClick={handleStart}
                  disabled={submitting}
                  className="w-full sm:w-auto"
                >
                  <Rocket className="h-4 w-4" />
                  {submitting ? '启动中…' : `开始${MODE_COPY[mode].title}`}
                </Button>
                <p className="text-xs text-zinc-500">
                  {mode === 'email_protocol'
                    ? `任务类型：email-protocol-register-token。协议后端：${emailProtocolBackend === 'go' ? 'Go worker' : 'Python/mailat'}；不触碰现有浏览器邮箱注册。`
                    : mode === 'email'
                      ? '任务类型：email-register-token。只完成邮箱注册并保存手动 Plus 交接文件；不会执行 add-phone/OAuth，也不会使用绑定手机号池。'
                      : '任务类型：register-token。成功后等待手动 Plus。'}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {run && runStatus && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle>任务 #{run.run_id.slice(0, 8)}</CardTitle>
            <div className="flex items-center gap-2">
              <Badge
                variant={
                  runStatus.status === 'running' ? 'warning' :
                  runStatus.status === 'complete' ? 'success' :
                  runStatus.status === 'failed' ? 'danger' : 'default'
                }
              >
                {STATUS_LABELS[runStatus.status] ?? runStatus.status}
              </Badge>
              {runStatus.status === 'running' && (
                <Button variant="outline" size="sm" onClick={handleCancel}>
                  <X className="h-4 w-4" />
                  取消
                </Button>
              )}
              <Button variant="ghost" size="sm" onClick={handleReset}>
                新建任务
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {activeBatchId && (
              <p className="text-xs text-zinc-500">批次 ID: {activeBatchId}</p>
            )}
            {batchProgress && (
              <div>
                <div className="h-1.5 rounded-full bg-zinc-800 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-blue-600 transition-all duration-500"
                    style={{ width: `${Math.min(100, batchProgress.progress_pct)}%` }}
                  />
                </div>
                <p className="text-xs text-zinc-500 mt-1">
                  成功 {batchProgress.succeeded} · 失败 {batchProgress.failed} · 进行中 {batchProgress.active} · 共 {batchProgress.total}
                  （{Math.round(batchProgress.progress_pct)}%）
                </p>
              </div>
            )}
            {runStatus.stage && (
              <p className="text-sm text-zinc-400">阶段: {runStatus.stage}</p>
            )}
            {runStatus.progress != null && !batchProgress && (
              <div>
                <div className="h-1.5 rounded-full bg-zinc-800 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-blue-600 transition-all duration-500"
                    style={{ width: `${Math.min(100, runStatus.progress * 100)}%` }}
                  />
                </div>
                <p className="text-xs text-zinc-500 mt-1">
                  {Math.round(runStatus.progress * 100)}%
                </p>
              </div>
            )}
            {runStatus.message && (
              <p className="text-sm text-zinc-300">{runStatus.message}</p>
            )}
            {exportInfo && (
              <p className="text-sm text-cyan-300/90 break-all">{exportInfo}</p>
            )}
            {runStatus.error && (
              <p className="text-sm text-red-400">{runStatus.error}</p>
            )}
          </CardContent>
        </Card>
      )}

      {run && runStatus?.steps_completed && runStatus.steps_completed.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>执行步骤</CardTitle>
          </CardHeader>
          <CardContent>
            <ol className="space-y-2 text-sm text-zinc-300">
              {runStatus.steps_completed.map((step) => (
                <li key={step} className="flex items-center gap-2">
                  <span className="h-1.5 w-1.5 rounded-full bg-blue-500" />
                  {step}
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
