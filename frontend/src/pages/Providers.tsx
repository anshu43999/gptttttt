import { useEffect, useMemo, useState } from 'react'
import { Smartphone, Mail, Wifi } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { SmsProviderCard } from '@/components/providers/SmsProviderCard'
import { MailboxProviderCard } from '@/components/providers/MailboxProviderCard'
import { ProxyProviderCard } from '@/components/providers/ProxyProviderCard'
import { getProviders, saveProvider, testProvider, getResources, importResources, getResourceCategories } from '@/lib/api'
import type { ProviderInfo, ProviderTestResult, ResourceItem, ResourceCategoryOption, ResourceImportType } from '@/lib/types'

const GROUP_CONFIG: Record<string, { label: string; icon: React.ComponentType<{ className?: string }> }> = {
  sms: { label: '接码服务商', icon: Smartphone },
  mailbox: { label: '邮箱服务商', icon: Mail },
  proxy: { label: '代理服务商', icon: Wifi },
}

type FieldSpec = {
  key: string
  label: string
  help?: string
  placeholder?: string
  secret?: boolean
  multiline?: boolean
  required?: boolean
}

type PoolMeta = {
  resource_type: ResourceImportType
  list_type: string
  provider: string
  title: string
  placeholder: string
  help: string
}

const PROVIDER_TITLES: Record<string, string> = {
  herosms_api: 'HeroSMS 接码',
  user_phone_url: '自备手机号 API',
  bind_user_phone_url: '绑定手机号 API',
  lajiao_credentials: '账密代理池',
  lajiao_api: '代理 API',
  outlook_token: 'Outlook Token 邮箱池',
  icloud_api: 'iCloud API 邮箱池',
  icloud_privacy: 'iCloud 隐私邮箱池',
  forwarded_domain: '转发域名邮箱',
  cfworker_admin_api: 'CFWorker / Cloud Mail',
}

const FIELD_SPECS: Record<string, FieldSpec[]> = {
  'sms/herosms_api': [
    { key: 'sms_api_key', label: 'HeroSMS API Key', secret: true, required: true },
    { key: 'sms_service', label: '服务代码', placeholder: 'dr', help: 'ChatGPT 通常使用 dr。' },
    { key: 'sms_country', label: '国家代码', placeholder: '73', help: '巴西为 73。' },
    { key: 'country_code', label: '手机号国家码', placeholder: '55' },
    { key: 'country_name', label: '国家名称', placeholder: 'Brazil' },
    { key: 'herosms_max_price', label: '最高单价美元', placeholder: '0.0999', help: '系统会强制限制在 0.1 美刀以下。' },
  ],
  'proxy/lajiao_api': [
    { key: 'lajiao_proxy_api_url', label: '代理 API URL', required: true, placeholder: 'https://proxy.example.invalid/endpoint' },
    { key: 'lajiao_proxy_regions', label: '出口地区', placeholder: 'JP,IN,US' },
    { key: 'lajiao_proxy_timeout', label: '检测超时秒数', placeholder: '15' },
  ],
  'mailbox/forwarded_domain': [
    { key: 'mailbox_domain', label: '转发域名', required: true, placeholder: 'mail.example.invalid' },
    { key: 'mailbox_imap_user', label: 'IMAP 收件箱账号', required: true, placeholder: 'mailbox@example.invalid' },
    { key: 'mailbox_imap_pass', label: 'IMAP 授权码', secret: true, required: true },
    { key: 'mailbox_imap_host', label: 'IMAP 主机', placeholder: 'imap.example.invalid' },
    { key: 'mailbox_imap_port', label: 'IMAP 端口', placeholder: '993' },
  ],
  'mailbox/icloud_privacy': [
    { key: 'icloud_privacy_order_text', label: 'iCloud 隐私邮箱账号', required: true, placeholder: 'alias@example.invalid', help: '每行一个 iCloud 隐私邮箱；验证码从下面 IMAP 收件箱读取。' },
    { key: 'icloud_privacy_order_file', label: '或使用已有文件路径', placeholder: 'data/imports/icloud_privacy.txt' },
    { key: 'mailbox_imap_user', label: 'IMAP 收件箱账号', required: true, placeholder: 'mailbox@example.invalid' },
    { key: 'mailbox_imap_pass', label: 'IMAP 授权码', secret: true, required: true },
    { key: 'mailbox_imap_host', label: 'IMAP 主机', placeholder: 'imap.example.invalid' },
    { key: 'mailbox_imap_port', label: 'IMAP 端口', placeholder: '993' },
  ],
  'mailbox/cfworker_admin_api': [
    { key: 'cfworker_api_url', label: 'CFWorker / Cloud Mail API URL', required: true, placeholder: 'https://mail.example.invalid/api' },
    { key: 'cfworker_admin_token', label: 'Admin/Open API Token', secret: true, required: true },
    { key: 'cfworker_domain', label: '邮箱域名', placeholder: 'mail.example.invalid' },
  ],
}

function providerKey(provider: ProviderInfo) {
  return `${provider.provider_type}/${provider.provider_name}`
}

function displayValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(',')
  if (value == null) return ''
  return String(value)
}

function resourceMeta(provider: ProviderInfo | null): PoolMeta | null {
  if (!provider) return null
  if (provider.provider_type === 'sms' && provider.provider_name === 'user_phone_url') {
    return { resource_type: 'phone', list_type: 'phone', provider: 'user_phone_url', title: '注册手机号资源池', placeholder: '15555550100|https://sms.example.invalid/messages/placeholder', help: '每行一个：手机号|取码URL。只用于手机号注册。' }
  }
  if (provider.provider_type === 'sms' && provider.provider_name === 'bind_user_phone_url') {
    return { resource_type: 'bind_phone', list_type: 'phone', provider: 'bind_user_phone_url', title: '绑定手机号资源池', placeholder: '15555550101|https://sms.example.invalid/messages/placeholder', help: '每行一个：手机号|取码URL。只用于 Plus 后绑定。' }
  }
  if (provider.provider_type === 'proxy' && provider.provider_name === 'lajiao_credentials') {
    return { resource_type: 'proxy', list_type: 'proxy', provider: 'lajiao_credentials', title: '账密代理池', placeholder: 'proxy-user:proxy-password@proxy.example.invalid:1080', help: '每行一个代理账号；支持显式 socks5:// 或 http://；导入后进入 SQLite 代理资源池。' }
  }
  if (provider.provider_type === 'mailbox' && provider.provider_name === 'outlook_token') {
    return { resource_type: 'email', list_type: 'email', provider: 'outlook_token', title: 'Outlook Token 邮箱池', placeholder: 'email----password----client_id----refresh_token', help: '每行一个 Outlook Token 邮箱。' }
  }
  if (provider.provider_type === 'mailbox' && provider.provider_name === 'icloud_api') {
    return { resource_type: 'icloud_email', list_type: 'email', provider: 'icloud_api', title: 'iCloud API 邮箱池', placeholder: 'mailbox@example.invalid----https://mail.example.invalid/inbox/placeholder----code:https://mail.example.invalid/code/placeholder----mail:https://mail.example.invalid/messages/placeholder', help: '每行一个 iCloud 邮箱；支持 show/code/mail 三种链接，存在 code API 时优先直接取码。' }
  }
  if (provider.provider_type === 'mailbox' && provider.provider_name === 'icloud_privacy') {
    return { resource_type: 'email', list_type: 'email', provider: 'icloud_privacy', title: 'iCloud 隐私邮箱池', placeholder: 'alias@example.invalid', help: '每行一个 iCloud 隐私邮箱账号；验证码会转发到 IMAP 收件箱。' }
  }
  return null
}

function exportLine(item: ResourceItem, meta: PoolMeta | null): string {
  if (meta?.list_type === 'phone') return `${item.resource_key}|${String(item.payload?.sms_url ?? '')}`
  if (meta?.provider === 'outlook_token') return `${String(item.payload?.email ?? item.resource_key)}----${String(item.payload?.password ?? '')}----${String(item.payload?.client_id ?? '')}----${String(item.payload?.refresh_token ?? '')}`
  if (meta?.provider === 'icloud_api') return `${String(item.payload?.email ?? item.resource_key)}----${String(item.payload?.inbox_url ?? '')}${item.payload?.code_url ? `----code:${String(item.payload.code_url)}` : ''}${item.payload?.mail_url ? `----mail:${String(item.payload.mail_url)}` : ''}`
  if (meta?.provider === 'icloud_privacy') return String(item.payload?.email ?? item.resource_key)
  return String(item.payload?.url ?? item.resource_key)
}

export default function Providers() {
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [editing, setEditing] = useState<ProviderInfo | null>(null)
  const [formValues, setFormValues] = useState<Record<string, string>>({})
  const [formEnabled, setFormEnabled] = useState(true)
  const [formError, setFormError] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, ProviderTestResult>>({})
  const [categories, setCategories] = useState<ResourceCategoryOption[]>([])
  const [poolItems, setPoolItems] = useState<ResourceItem[]>([])
  const [poolImportText, setPoolImportText] = useState('')
  const [poolImportFileName, setPoolImportFileName] = useState('')
  const [poolLoading, setPoolLoading] = useState(false)
  const [lastImportCount, setLastImportCount] = useState<number | null>(null)

  const specs = useMemo(() => editing ? editing.definition?.fields ?? FIELD_SPECS[providerKey(editing)] ?? [] : [], [editing])
  const meta = resourceMeta(editing)

  const loadProviders = () => {
    setLoading(true)
    Promise.all([getProviders(), getResourceCategories()])
      .then(([providerItems, categoryItems]) => {
        setProviders(providerItems)
        setCategories(categoryItems)
      })
      .catch((err) => setError(err instanceof Error ? err.message : '加载失败'))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    loadProviders()
  }, [])

  const loadPool = async (provider: ProviderInfo) => {
    const current = resourceMeta(provider)
    if (!current) return
    setPoolLoading(true)
    try {
      setPoolItems(await getResources({ resource_type: current.list_type, provider: current.provider }))
      setCategories(await getResourceCategories())
    } finally {
      setPoolLoading(false)
    }
  }

  const openConfig = (provider: ProviderInfo) => {
    const values: Record<string, string> = {}
    const source = provider.settings ?? {}
    for (const spec of provider.definition?.fields ?? FIELD_SPECS[providerKey(provider)] ?? []) {
      values[spec.key] = displayValue(source[spec.key])
    }
    setEditing(provider)
    setFormValues(values)
    setFormEnabled(provider.enabled)
    setFormError(null)
    setPoolImportText('')
    setPoolImportFileName('')
    setLastImportCount(null)
    const current = resourceMeta(provider)
    if (current) loadPool(provider)
    else setPoolItems([])
  }

  const providerOverrides = (provider: ProviderInfo) => {
    const values: Record<string, unknown> = { ...provider.settings }
    for (const [key, value] of Object.entries(formValues)) {
      if (value === '***') continue
      if (value !== '') values[key] = value
    }
    return values
  }

  const handlePoolFile = async (file: File | null) => {
    if (!file) return
    setFormError(null)
    setPoolImportFileName(file.name)
    // Read file in browser; avoid pasting 1.7MB+ into controlled textarea.
    const text = await file.text()
    setPoolImportText(text)
  }

  const handlePoolImport = async () => {
    if (!editing || !meta || !poolImportText.trim()) return
    setSaving(true)
    setFormError(null)
    setLastImportCount(null)
    try {
      const result = await importResources({
        resource_type: meta.resource_type === 'bind_phone' ? 'phone' : (meta.resource_type === 'icloud_email' ? 'email' : meta.resource_type),
        provider: meta.provider,
        text: poolImportText,
      })
      setLastImportCount(result.count)
      setPoolImportText('')
      setPoolImportFileName('')
      // Refresh counts only (categories + light list); do not block on huge tables.
      await loadPool(editing)
    } catch (err) {
      setFormError(err instanceof Error ? err.message : '导入失败')
    } finally {
      setSaving(false)
    }
  }

  const exportPool = () => {
    const text = poolItems.map((item) => exportLine(item, meta)).join('\n')
    navigator.clipboard?.writeText(text)
    setPoolImportText(text)
  }

  const handleTest = async (providerName: string, explicitProvider?: ProviderInfo) => {
    const provider = explicitProvider ?? providers.find((p) => p.provider_name === providerName)
    if (!provider) return
    setTesting(providerName)
    try {
      const result = await testProvider({ provider_type: provider.provider_type, provider_name: provider.provider_name, settings: explicitProvider ? providerOverrides(provider) : provider.settings })
      setTestResults((prev) => ({ ...prev, [providerName]: result }))
    } catch (err) {
      setTestResults((prev) => ({ ...prev, [providerName]: { ok: false, provider_type: provider.provider_type, message: err instanceof Error ? err.message : '测试失败' } }))
    } finally {
      setTesting(null)
    }
  }

  const validateForm = () => {
    if (meta) return ''
    for (const spec of specs) {
      if (spec.required && !String(formValues[spec.key] ?? '').trim()) return `请填写${spec.label}`
    }
    return ''
  }

  const handleSave = async () => {
    if (!editing) return
    const validation = validateForm()
    if (validation) {
      setFormError(validation)
      return
    }
    setSaving(true)
    setFormError(null)
    try {
      const updated = await saveProvider({ provider_type: editing.provider_type, provider_name: editing.provider_name, enabled: formEnabled, settings: providerOverrides(editing) })
      setProviders((prev) => prev.map((p) => providerKey(p) === providerKey(updated) ? updated : p))
      setEditing(null)
    } catch (err) {
      setFormError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="flex h-full items-center justify-center p-8"><div className="text-zinc-500 animate-pulse">正在加载服务商…</div></div>
  if (error) return <div className="flex h-full flex-col items-center justify-center p-8 gap-4"><p className="text-red-400">{error}</p><Button variant="outline" onClick={loadProviders}>重试</Button></div>

  const grouped = providers.reduce<Record<string, ProviderInfo[]>>((acc, p) => {
    const type = p.provider_type || 'other'
    if (!acc[type]) acc[type] = []
    acc[type].push(p)
    return acc
  }, {})

  return (
    <div className="space-y-8 p-6">
      <div>
        <h2 className="text-xl font-semibold text-zinc-100">服务商</h2>
        <p className="text-sm text-zinc-400 mt-1">配置接码、邮箱和代理。资源型服务商会直接写入 SQLite 资源池。</p>
      </div>

      {Object.entries(GROUP_CONFIG).map(([type, { label, icon: Icon }]) => {
        const groupProviders = grouped[type]
        if (!groupProviders?.length) return null
        return (
          <div key={type}>
            <div className="flex items-center gap-2 mb-3">
              <Icon className="h-5 w-5 text-zinc-400" />
              <h3 className="text-base font-semibold text-zinc-200">{label}</h3>
              <span className="text-xs text-zinc-500">({groupProviders.length})</span>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {groupProviders.map((provider) => {
                const result = testResults[provider.provider_name]
                const props = { provider, onTest: testing ? undefined : handleTest, onConfigure: openConfig }
                return (
                  <div key={provider.provider_name} className="space-y-2">
                    {type === 'sms' && <SmsProviderCard {...props} />}
                    {type === 'mailbox' && <MailboxProviderCard {...props} />}
                    {type === 'proxy' && <ProxyProviderCard {...props} />}
                    {testing === provider.provider_name && <p className="text-xs text-zinc-500 animate-pulse">测试中…</p>}
                    {result && testing !== provider.provider_name && (
                      <div className={`rounded-md px-3 py-2 text-xs ${result.ok ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-red-500/10 text-red-400 border border-red-500/20'}`}>
                        {result.ok ? `✓ ${result.message || '测试通过'}` : `✗ ${result.message || '测试失败'}`}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )
      })}

      <Dialog open={!!editing} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent className="max-w-5xl">
          <DialogHeader>
            <DialogTitle>{editing ? `配置 ${editing.definition?.label ?? PROVIDER_TITLES[editing.provider_name] ?? editing.provider_name}` : '配置服务商'}</DialogTitle>
            <DialogDescription>{meta ? '资源会写入 SQLite resource_pool，支持批量导入、导出、状态查看。' : editing?.definition?.help ?? '保存后写入数据库配置。'}</DialogDescription>
          </DialogHeader>

          {editing && (
            <div className="grid gap-6 lg:grid-cols-[220px_1fr]">
              <aside className="rounded-xl border border-white/10 bg-zinc-950/60 p-4">
                <p className="text-xs uppercase tracking-[0.24em] text-zinc-500">Provider</p>
                <h3 className="mt-2 text-base font-semibold text-zinc-100">{editing.definition?.label ?? PROVIDER_TITLES[editing.provider_name] ?? editing.provider_name}</h3>
                <p className="mt-2 text-xs text-zinc-500">{editing.provider_type}/{editing.provider_name}</p>
                <label className="mt-5 flex items-center justify-between rounded-lg border border-white/10 bg-zinc-900/70 px-3 py-2 text-sm text-zinc-300">启用服务商<input type="checkbox" checked={formEnabled} onChange={(event) => setFormEnabled(event.target.checked)} className="accent-blue-600" /></label>
                {meta && (
                  <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
                    <div className="rounded-lg bg-emerald-500/10 p-2 text-emerald-300">可用 {poolItems.filter((item) => item.status === 'available').length}</div>
                    <div className="rounded-lg bg-zinc-800 p-2 text-zinc-300">总计 {poolItems.length}</div>
                    <div className="rounded-lg bg-amber-500/10 p-2 text-amber-300">冷却 {poolItems.filter((item) => item.status === 'cooldown').length}</div>
                    <div className="rounded-lg bg-red-500/10 p-2 text-red-300">禁用 {poolItems.filter((item) => item.status === 'disabled').length}</div>
                  </div>
                )}
                {categories.find((item) => item.provider === editing.provider_name) && <p className="mt-3 text-xs text-zinc-500">资源池可用：{categories.find((item) => item.provider === editing.provider_name)?.available ?? 0}</p>}
              </aside>

              <section className="space-y-5">
                {meta ? (
                  <div className="space-y-4">
                    <div className="rounded-xl border border-blue-500/20 bg-blue-500/5 p-4">
                      <h4 className="text-sm font-semibold text-blue-200">{meta.title}</h4>
                      <p className="mt-1 text-xs text-zinc-400">{meta.help}</p>
                      <p className="mt-1 text-xs text-zinc-500">大批量请用「选择文件」直接读取，勿粘贴进文本框。</p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800">
                        选择文件
                        <input
                          type="file"
                          accept=".txt,.csv,text/plain"
                          className="hidden"
                          onChange={(event) => {
                            const file = event.target.files?.[0] ?? null
                            void handlePoolFile(file)
                            event.target.value = ''
                          }}
                        />
                      </label>
                      {poolImportFileName && (
                        <span className="text-xs text-zinc-400">
                          已选：{poolImportFileName}（{poolImportText.split(/\r?\n/).filter(Boolean).length} 行）
                        </span>
                      )}
                      {lastImportCount != null && (
                        <span className="text-xs text-emerald-400">上次导入新增 {lastImportCount} 条</span>
                      )}
                    </div>
                    <textarea
                      value={poolImportText}
                      onChange={(event) => setPoolImportText(event.target.value)}
                      placeholder={meta.placeholder}
                      rows={8}
                      className="w-full rounded-xl border border-white/10 bg-zinc-900 px-3 py-3 font-mono text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-blue-600"
                    />
                    <div className="flex flex-wrap gap-2">
                      <Button onClick={handlePoolImport} disabled={saving || !poolImportText.trim()}>
                        {saving ? '导入中…' : '批量导入到数据库'}
                      </Button>
                      <Button variant="outline" onClick={exportPool} disabled={poolItems.length === 0}>
                        导出当前池
                      </Button>
                      <Button variant="ghost" onClick={() => editing && loadPool(editing)} disabled={poolLoading}>
                        {poolLoading ? '刷新中…' : '刷新资源池'}
                      </Button>
                    </div>
                    <div className="max-h-56 overflow-auto rounded-xl border border-white/10"><table className="w-full text-xs"><thead className="bg-zinc-950 text-zinc-500"><tr><th className="px-3 py-2 text-left">资源</th><th className="px-3 py-2 text-left">状态</th><th className="px-3 py-2 text-left">成功/失败</th></tr></thead><tbody>{poolItems.slice(0, 80).map((item) => <tr key={item.id} className="border-t border-white/5"><td className="px-3 py-2 text-zinc-300">{item.resource_key}</td><td className="px-3 py-2 text-zinc-400">{item.status}</td><td className="px-3 py-2 text-zinc-500">{item.success_count}/{item.fail_count}</td></tr>)}{poolItems.length === 0 && <tr><td className="px-3 py-8 text-center text-zinc-500" colSpan={3}>暂无资源。</td></tr>}</tbody></table></div>
                  </div>
                ) : (
                  <div className="grid gap-4 sm:grid-cols-2">{specs.map((spec) => <div key={spec.key} className={spec.multiline ? 'space-y-1.5 sm:col-span-2' : 'space-y-1.5'}><label className="text-sm text-zinc-300">{spec.label}{spec.required && <span className="text-red-400"> *</span>}</label>{spec.multiline ? <textarea value={formValues[spec.key] ?? ''} onChange={(e) => setFormValues((prev) => ({ ...prev, [spec.key]: e.target.value }))} placeholder={spec.placeholder} rows={8} className="w-full rounded-xl border border-white/10 bg-zinc-900 px-3 py-3 font-mono text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-blue-600" /> : <Input value={formValues[spec.key] ?? ''} onChange={(e) => setFormValues((prev) => ({ ...prev, [spec.key]: e.target.value }))} placeholder={spec.placeholder} type={spec.secret ? 'password' : 'text'} />}{spec.help && <p className="text-xs text-zinc-500">{spec.help}</p>}</div>)}</div>
                )}
              </section>
            </div>
          )}

          {formError && <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">{formError}</div>}
          <DialogFooter>{editing && !meta && <Button variant="outline" onClick={() => handleTest(editing.provider_name, editing)} disabled={!!testing || saving}>测试当前配置</Button>}<Button variant="ghost" onClick={() => setEditing(null)} disabled={saving}>取消</Button><Button variant="accent" onClick={handleSave} disabled={saving}>{saving ? '保存中…' : '保存配置'}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
