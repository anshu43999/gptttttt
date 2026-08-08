import { useEffect, useMemo, useState } from 'react'
import { ArrowRight, Link2, RefreshCw, Save } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  checkResourceCapacity,
  getAccounts,
  getConfig,
  protocolBindAccount,
  resumeOAuthAccount,
  saveConfig,
} from '@/lib/api'
import type { Account } from '@/lib/types'

const STATUS_LABELS: Record<string, string> = {
  email_registered: '邮箱已注册，待 Plus',
  manual_plus_required: '等待手动 Plus',
  manual_plus_confirmed: '已手动确认 Plus',
  plus_verified_needs_oauth: 'Plus 已验证，待绑定',
  cpa_bound: '已提交 CPA',
  complete: '已完成',
  archived: '已归档',
}

const BIND_COUNTRIES = [
  { value: 'US', code: '1', label: '美国 (+1)' },
  { value: 'BR', code: '55', label: '巴西 (+55)' },
  { value: 'JP', code: '81', label: '日本 (+81)' },
] as const

function isBindable(account: Account): boolean {
  const stage = String(account.stage || '').toLowerCase()
  const status = String(account.status || '').toLowerCase()
  const key = String(account.key || account.account_key || '')
  const hasIdentity = Boolean(account.phone_number || account.sms_phone || account.email)
  const failed = stage === 'failed' || status === 'failed' || status === 'error'
  const finished = ['cpa_bound', 'complete', 'archived'].includes(stage) || ['cpa_bound', 'complete', 'archived'].includes(status)
  const binding = String(account.binding_status || '').toLowerCase()
  const bindingDoneOrBusy = ['binding_queued', 'binding_started', 'cpa_submitted', 'bound', 'archived'].includes(binding)
  const timestampKeyOnly = /^\d{4}-\d{2}-\d{2}T/.test(key) && !hasIdentity
  return hasIdentity && !failed && !finished && !bindingDoneOrBusy && !timestampKeyOnly
}


function registrationMode(account: Account): 'phone' | 'email' {
  const mode = String(account.registration_mode || '').toLowerCase()
  if (mode === 'email') return 'email'
  if (mode === 'phone') return 'phone'
  return account.phone_number || account.sms_phone ? 'phone' : 'email'
}

function modeLabel(mode: 'phone' | 'email'): string {
  return mode === 'phone' ? '手机号 → 邮箱' : '邮箱 → 手机号'
}

function AccountTable({
  title,
  items,
  selectedKeys,
  allSelected,
  onToggleAll,
  onToggle,
  mode,
}: {
  title: string
  items: Account[]
  selectedKeys: string[]
  allSelected: boolean
  onToggleAll: () => void
  onToggle: (key: string) => void
  mode: 'phone' | 'email'
}) {
  const primaryLabel = mode === 'phone' ? '手机号' : '邮箱'
  const secondaryLabel = mode === 'phone' ? '邮箱' : '手机号'
  const primaryValue = (account: Account) => mode === 'phone' ? (account.phone_number || account.sms_phone || '—') : (account.email || account.login_identifier || '—')
  const secondaryValue = (account: Account) => mode === 'phone' ? (account.email || '—') : (account.phone_number || account.sms_phone || account.binding_phone_number || '待绑定')
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between px-1">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
          {mode === 'phone' ? '手机号注册账号' : '邮箱注册账号'}
          <span className="text-zinc-500">/</span>
          {title}
        </h3>
        <span className="text-xs text-zinc-500">{items.length} 个</span>
      </div>
      <div className="overflow-x-auto rounded-xl border border-white/5">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-white/5 bg-zinc-950/50 text-left text-xs uppercase text-zinc-500">
              <th className="px-4 py-3"><input type="checkbox" checked={allSelected} onChange={onToggleAll} /></th>
              <th className="px-4 py-3 font-medium">{primaryLabel}</th>
              <th className="px-4 py-3 font-medium">{secondaryLabel}</th>
              <th className="px-4 py-3 font-medium">密码</th>
              <th className="px-4 py-3 font-medium">昵称</th>
              <th className="px-4 py-3 font-medium">套餐记录</th>
              <th className="px-4 py-3 font-medium">绑定状态</th>
            </tr>
          </thead>
          <tbody>
            {items.map((account) => (
              <tr key={account.key} className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]">
                <td className="px-4 py-3"><input type="checkbox" checked={selectedKeys.includes(account.key)} onChange={() => onToggle(account.key)} /></td>
                <td className="max-w-[240px] truncate px-4 py-3 text-zinc-300" title={String(primaryValue(account))}>{primaryValue(account)}</td>
                <td className="max-w-[240px] truncate px-4 py-3 text-zinc-300" title={String(secondaryValue(account))}>{secondaryValue(account)}</td>
                <td className="max-w-[180px] truncate px-4 py-3 font-mono text-xs text-zinc-300" title={account.has_password || account.password ? '已设置密码' : ''}>{account.has_password || account.password ? '••••••••' : '—'}</td>
                <td className="px-4 py-3 text-zinc-300">{account.display_name || '—'}</td>
                <td className="px-4 py-3"><Badge variant="secondary">{account.plan_type || account.plus_status || '未校验'}</Badge></td>
                <td className="px-4 py-3 text-zinc-400">{STATUS_LABELS[account.stage || ''] ?? account.stage ?? '—'}</td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={7} className="px-4 py-10 text-center text-zinc-500">暂无账号。</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function Binding() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [activeMode, setActiveMode] = useState<'phone' | 'email'>('email')
  const [bindingMethod, setBindingMethod] = useState<'protocol' | 'browser'>('protocol')
  const [oauthMode, setOauthMode] = useState('cpa')
  const [cpaBaseUrl, setCpaBaseUrl] = useState('')
  const [cpaManagementKey, setCpaManagementKey] = useState('')
  const [bindSmsProvider, setBindSmsProvider] = useState('bind_user_phone_url')
  const [bindSmsPhoneUrl, setBindSmsPhoneUrl] = useState('')
  const [bindSmsCountry, setBindSmsCountry] = useState('US')
  const [bindSmsService, setBindSmsService] = useState('dr')
  const [bindCountryCode, setBindCountryCode] = useState('1')
  const [headed, setHeaded] = useState(true)
  const [bindThreads, setBindThreads] = useState(1)
  const [maxParallelTasks, setMaxParallelTasks] = useState(1)

  const bindableAccounts = useMemo(() => accounts.filter(isBindable), [accounts])
  const phoneAccounts = useMemo(() => bindableAccounts.filter((account) => registrationMode(account) === 'phone'), [bindableAccounts])
  const emailAccounts = useMemo(() => bindableAccounts.filter((account) => registrationMode(account) === 'email'), [bindableAccounts])
  const visibleAccounts = activeMode === 'phone' ? phoneAccounts : emailAccounts
  const selectedAccounts = useMemo(
    () => visibleAccounts.filter((account) => selectedKeys.includes(account.key)),
    [visibleAccounts, selectedKeys],
  )
  const allVisibleSelected = visibleAccounts.length > 0 && visibleAccounts.every((account) => selectedKeys.includes(account.key))

  const toggleGroup = (items: Account[]) => {
    setSelectedKeys((prev) => {
      const allSelected = items.length > 0 && items.every((account) => prev.includes(account.key))
      if (allSelected) return prev.filter((key) => !items.some((account) => account.key === key))
      return Array.from(new Set([...prev, ...items.map((account) => account.key)]))
    })
  }

  const switchMode = (mode: 'phone' | 'email') => {
    setActiveMode(mode)
    setSelectedKeys([])
  }

  useEffect(() => {
    let cancelled = false
    Promise.all([getAccounts(), getConfig()])
      .then(([items, cfg]) => {
        if (cancelled) return
        setAccounts(items)
        const merged = { ...(cfg.config ?? {}), ...(cfg.file_config ?? {}), ...(cfg.db_config ?? {}) }
        setOauthMode(String(merged.oauth_callback_mode ?? 'cpa'))
        setBindingMethod(String(merged.binding_method ?? 'protocol') === 'browser' ? 'browser' : 'protocol')
        setCpaBaseUrl(String(merged.cpa_base_url ?? ''))
        setCpaManagementKey(String(merged.cpa_management_key ?? ''))
        const loadedBindProvider = String(merged.bind_sms_provider ?? merged.sms_provider ?? 'bind_user_phone_url')
        const loadedBindCountry = String(merged.bind_sms_country ?? (loadedBindProvider === 'smsbower' || loadedBindProvider === 'smsbower_api' ? 'BR' : 'US'))
        setBindSmsProvider(loadedBindProvider)
        setBindSmsPhoneUrl(String(merged.bind_sms_phone_url ?? ''))
        setBindSmsCountry(loadedBindCountry)
        setBindSmsService(String(merged.bind_sms_service ?? merged.sms_service ?? 'dr'))
        setBindCountryCode(String(merged.bind_country_code ?? (loadedBindCountry === 'BR' ? '55' : '1')))
        const loadedOauthTasks = Math.max(1, Math.min(100, Number(merged.max_oauth_tasks ?? 1) || 1))
        const loadedParallelTasks = Math.max(1, Math.min(100, Number(merged.max_parallel_tasks ?? loadedOauthTasks) || loadedOauthTasks))
        setBindThreads(loadedOauthTasks)
        setMaxParallelTasks(Math.max(loadedParallelTasks, loadedOauthTasks))
      })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : '加载绑定配置失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const reloadAccounts = async () => {
    setAccounts(await getAccounts())
  }

  const toggleSelected = (key: string) => {
    setSelectedKeys((prev) => prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key])
  }

  const bindTaskPayload = () => Object.fromEntries(Object.entries({
    oauth_callback_mode: oauthMode,
    cpa_base_url: cpaBaseUrl,
    cpa_management_key: cpaManagementKey,
    bind_sms_provider: bindSmsProvider,
    bind_sms_phone_url: ['bind_user_phone_url', 'user_phone_url', 'phone_url', 'manual_phone_url'].includes(bindSmsProvider) ? bindSmsPhoneUrl : '',
    bind_sms_country: bindSmsCountry,
    bind_sms_service: bindSmsService,
    bind_country_code: bindCountryCode,
  }).filter(([, value]) => String(value ?? '') !== '***' && String(value ?? '').trim() !== '')) as Record<string, string>

  const bindingPayload = () => ({
    ...bindTaskPayload(),
    binding_method: bindingMethod,
    max_oauth_tasks: bindThreads,
    max_parallel_tasks: Math.max(maxParallelTasks, bindThreads),
  })

  const saveBindingConfig = async () => {
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await saveConfig(bindingPayload())
      setMessage('绑定配置已保存。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存绑定配置失败')
    } finally {
      setSaving(false)
    }
  }

  const handleCountryChange = (country: string) => {
    setBindSmsCountry(country)
    const matched = BIND_COUNTRIES.find((item) => item.value === country)
    if (matched) setBindCountryCode(matched.code)
  }

  const handleBindSmsProviderChange = (provider: string) => {
    setBindSmsProvider(provider)
    if (provider === 'smsbower' || provider === 'smsbower_api') {
      setBindSmsCountry('BR')
      setBindCountryCode('55')
      setBindSmsService('dr')
    }
  }


  const startBinding = async () => {
    const candidates = selectedAccounts
    if (candidates.length === 0) {
      setError('请选择待绑定账号。')
      return
    }
    if (bindingMethod === 'protocol' && activeMode === 'phone') {
      setError('协议绑定当前只支持“邮箱 → 手机号” add-phone；手机号注册账号请切换为浏览器绑定。')
      return
    }
    setRunning(true)
    setError(null)
    setMessage(null)
    try {
      if (bindSmsProvider === 'bind_user_phone_url') {
        const capacity = await checkResourceCapacity({ need_bind_phone: candidates.length })
        const phone = capacity.resources.find((item) => item.resource_type === 'phone' && item.provider === 'bind_user_phone_url')
        if (phone && !phone.enough) {
          throw new Error(`绑定手机号不足：需要 ${phone.required}，可用 ${phone.available}`)
        }
      }
      const configPayload = bindingPayload()
      const taskPayload = bindTaskPayload()
      await saveConfig(configPayload)
      const started: string[] = []
      for (const account of candidates) {
        const res = bindingMethod === 'protocol'
          ? await protocolBindAccount(account.key, taskPayload)
          : await resumeOAuthAccount(account.key, headed, taskPayload)
        started.push(res.task.id)
      }
      const methodLabel = bindingMethod === 'protocol' ? '协议 CPA' : '浏览器 CPA/OAuth'
      setMessage(`已启动 ${started.length} 个${methodLabel}绑定任务；绑定并发已保存为 ${bindThreads} 线程，全局最大并发 ${Math.max(maxParallelTasks, bindThreads)}。`)
      await reloadAccounts()
    } catch (err) {
      setError(err instanceof Error ? err.message : '启动绑定失败')
    } finally {
      setRunning(false)
    }
  }

  if (loading) {
    return <div className="flex h-full items-center justify-center p-8 text-zinc-500">正在加载绑定页面…</div>
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-xl font-semibold text-zinc-100">账号绑定</h2>
        <p className="mt-1 text-sm text-zinc-400">默认协议执行 add-phone + CPA/OAuth；浏览器绑定仍保留为兜底入口。</p>
      </div>

      {error && <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">{error}</div>}
      {message && <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-300">{message}</div>}

      <div className="grid gap-6 xl:grid-cols-[420px_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>绑定配置</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <label className="text-sm text-zinc-400">执行方式</label>
              <Select value={bindingMethod} onValueChange={(value) => setBindingMethod(value === 'browser' ? 'browser' : 'protocol')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="protocol">协议绑定（默认）</SelectItem>
                  <SelectItem value="browser">浏览器绑定（保留兜底）</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-zinc-500">协议绑定使用已配置的协议服务；浏览器绑定复用已有会话。</p>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm text-zinc-400">绑定模式</label>
              <Select value={oauthMode} onValueChange={setOauthMode}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="cpa">CPA 绑定</SelectItem>
                  <SelectItem value="local">本地 OAuth</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {oauthMode === 'cpa' && (
              <>
                <div className="space-y-1.5">
                  <label className="text-sm text-zinc-400">CPA 地址</label>
                  <Input value={cpaBaseUrl} onChange={(event) => setCpaBaseUrl(event.target.value)} placeholder="https://service.example.invalid" />
                </div>
                <div className="space-y-1.5">
                  <label className="text-sm text-zinc-400">CPA 管理 Key</label>
                  <Input type="password" value={cpaManagementKey} onChange={(event) => setCpaManagementKey(event.target.value)} placeholder="保存后以 *** 显示" />
                </div>
              </>
            )}

            <div className="space-y-1.5">
              <label className="text-sm text-zinc-400">绑定手机号池</label>
              <Select value={bindSmsProvider} onValueChange={handleBindSmsProviderChange}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="smsbower_api">SMSBower API</SelectItem>
                  <SelectItem value="bind_user_phone_url">绑定手机号 API</SelectItem>
                  <SelectItem value="user_phone_url">注册手机号池（兼容）</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm text-zinc-400">临时绑定手机号 API</label>
              <Input value={bindSmsPhoneUrl} onChange={(event) => setBindSmsPhoneUrl(event.target.value)} placeholder="15555550101|https://sms.example.invalid/messages/placeholder" disabled={!['bind_user_phone_url', 'user_phone_url', 'phone_url', 'manual_phone_url'].includes(bindSmsProvider)} />
              <p className="text-xs text-zinc-500">服务商凭据和选项在“服务商”页管理。此字段仅用于手机号 URL 服务商。</p>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">绑定国家</label>
                <Select value={bindSmsCountry} onValueChange={handleCountryChange}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {BIND_COUNTRIES.map((country) => <SelectItem key={country.value} value={country.value}>{country.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">国家码</label>
                <Input value={bindCountryCode} onChange={(event) => setBindCountryCode(event.target.value)} placeholder="1" />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">绑定服务代码</label>
                <Input value={bindSmsService} onChange={(event) => setBindSmsService(event.target.value)} placeholder="dr" />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">绑定并发线程</label>
                <Input type="number" min={1} max={100} value={bindThreads} onChange={(event) => setBindThreads(Math.max(1, Math.min(100, Number(event.target.value) || 1)))} />
                <p className="text-xs text-zinc-500">保存为 max_oauth_tasks；批量绑定同一时间最多跑这个数。</p>
              </div>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm text-zinc-400">全局最大并发任务</label>
              <Input type="number" min={1} max={100} value={maxParallelTasks} onChange={(event) => setMaxParallelTasks(Math.max(bindThreads, Math.min(100, Number(event.target.value) || bindThreads)))} />
              <p className="text-xs text-zinc-500">保存为 max_parallel_tasks；必须 ≥ 绑定并发，否则绑定任务会被全局队列卡住。Go 协议可到 100。</p>
            </div>

            {bindingMethod === 'browser' && (
              <label className="flex items-center gap-2 text-sm text-zinc-300">
                <input type="checkbox" checked={headed} onChange={(event) => setHeaded(event.target.checked)} className="rounded border-white/10 bg-zinc-900 accent-blue-600" />
                显示浏览器窗口
              </label>
            )}

            <Button onClick={saveBindingConfig} disabled={saving} className="w-full">
              <Save className="h-4 w-4" />
              {saving ? '保存中…' : '保存绑定配置'}
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between gap-3">
            <CardTitle>待绑定账号</CardTitle>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={reloadAccounts}><RefreshCw className="h-4 w-4" />刷新</Button>
              <Button onClick={startBinding} disabled={running || selectedKeys.length === 0}><Link2 className="h-4 w-4" />开始绑定</Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="grid gap-3 md:grid-cols-2">
              {(['phone', 'email'] as const).map((mode) => {
                const active = activeMode === mode
                const count = mode === 'phone' ? phoneAccounts.length : emailAccounts.length
                const [left, right] = modeLabel(mode).split(' → ')
                return (
                  <button
                    key={mode}
                    type="button"
                    onClick={() => switchMode(mode)}
                    className={`flex items-center justify-between rounded-xl border px-4 py-3 text-left transition ${active ? 'border-blue-500/60 bg-blue-500/10 text-blue-100' : 'border-white/10 bg-white/[0.03] text-zinc-300 hover:border-white/20 hover:bg-white/[0.06]'}`}
                  >
                    <span className="flex items-center gap-2 text-sm font-semibold"><span>{left}</span><ArrowRight className="h-4 w-4" /><span>{right}</span></span>
                    <span className="text-xs text-zinc-500">{count} 个</span>
                  </button>
                )
              })}
            </div>
            <AccountTable
              title={modeLabel(activeMode)}
              mode={activeMode}
              items={visibleAccounts}
              selectedKeys={selectedKeys}
              allSelected={allVisibleSelected}
              onToggleAll={() => toggleGroup(visibleAccounts)}
              onToggle={toggleSelected}
            />
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
