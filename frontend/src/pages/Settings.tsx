import { useEffect, useState } from 'react'
import { Save } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { getConfig, saveConfig } from '@/lib/api'
import type { ConfigPayload } from '@/lib/types'

interface SettingsSection {
  tab: string
  label: string
  keys: string[]
}

const SECTIONS: SettingsSection[] = [
  { tab: 'runtime', label: '运行时', keys: ['output_dir', 'headed', 'browser_mode', 'browser_engine', 'browser_channel', 'browser_profile_mode', 'browser_no_viewport', 'email_register_flow', 'locale', 'timezone_id', 'accept_language', 'use_camoufox', 'camoufox_geoip', 'camoufox_humanize', 'save_session', 'save_tokens', 'max_parallel_tasks', 'max_register_tasks', 'max_oauth_tasks'] },
  { tab: 'register', label: '注册', keys: ['precheck_phone_before_sms', 'prepare_registration_before_phone', 'force_signup_from_login_password', 'email_otp_timeout', 'email_otp_poll_interval'] },
  { tab: 'proxy', label: '代理', keys: ['proxy', 'rotate_proxy_each_attempt', 'lajiao_proxy_mode', 'lajiao_proxy_credential_protocol', 'lajiao_proxy_credentials', 'lajiao_proxy_credentials_file', 'lajiao_proxy_regions', 'lajiao_proxy_expected_country', 'lajiao_proxy_timeout'] },
  { tab: 'mailbox', label: '邮箱', keys: ['mailbox_provider', 'icloud_api_order_file', 'icloud_api_order_text', 'mailbox_domain', 'mailbox_imap_user', 'mailbox_imap_pass', 'mailbox_imap_host', 'mailbox_imap_port', 'icloud_privacy_order_file', 'cfworker_api_url', 'cfworker_admin_token', 'cfworker_domain'] },
  { tab: 'oauth', label: 'OAuth', keys: ['outlook_email', 'outlook_password', 'outlook_web_otp', 'outlook_token_order_file', 'oauth_callback_mode', 'cpa_base_url', 'cpa_management_key', 'cpa_manager_admin_key', 'cpa_auth_file_sync_url', 'cpa_sync_ssh_host', 'cpa_sync_container', 'oauth_client_id', 'oauth_redirect_uri', 'outlook_cooldown_hours'] },
  { tab: 'plus', label: '服务', keys: ['iceaix_api_key', 'iceaix_base_url', 'iceaix_sms_api', 'paypal_phone', 'iceaix_job_timeout', 'plus_verify_retries', 'manual_plus_trust_confirmation', 'sub2api_url', 'sub2api_admin_key'] },
]

const SECTION_TITLES: Record<string, string> = {
  runtime: '运行时',
  register: '注册',
  proxy: '代理',
  mailbox: '邮箱',
  oauth: 'OAuth',
  plus: 'Plus',
}

const FIELD_LABELS: Record<string, string> = {
  output_dir: '输出目录',
  headed: '显示浏览器',
  browser_mode: '浏览器模式',
  use_camoufox: '使用 Camoufox',
  browser_engine: '浏览器内核',
  browser_channel: '浏览器通道',
  browser_profile_mode: '浏览器 Profile',
  browser_no_viewport: '使用真实窗口尺寸',
  email_register_flow: '邮箱注册链路',
  locale: '浏览器语言',
  timezone_id: '浏览器时区',
  accept_language: 'Accept-Language',
  camoufox_geoip: 'Camoufox GeoIP',
  camoufox_humanize: '人类化操作',
  save_session: '保存会话',
  save_tokens: '保存 Token',
  max_parallel_tasks: '最大并发任务',
  max_register_tasks: '最大注册并发',
  max_oauth_tasks: '最大绑定并发',
  sms_provider: '默认接码服务商',
  sms_phone_url: '手机号 API 地址',
  sms_phone_url_file: '手机号 API 文件',
  sms_country: '接码国家',
  sms_service: '接码服务代码',
  country_code: '国家代码',
  country_name: '国家名称',
  bind_sms_provider: '绑定接码服务商',
  bind_sms_phone_url: '绑定手机号 API 地址',
  bind_sms_phone_url_file: '绑定手机号 API 文件',
  bind_sms_country: '绑定接码国家',
  bind_sms_service: '绑定服务代码',
  bind_country_code: '绑定国家代码',
  precheck_phone_before_sms: '发短信前预检手机号',
  prepare_registration_before_phone: '取号前准备注册环境',
  force_signup_from_login_password: '密码页强制转注册',
  email_otp_timeout: '邮箱验证码超时',
  email_otp_poll_interval: '邮箱验证码轮询间隔',
  sms_api_key: '接码 API Key',
  sms_code_timeout: '验证码超时秒数',
  phone_retry_limit: '换号重试次数',
  herosms_fixed_price: 'HeroSMS 固定价格',
  herosms_max_price: 'HeroSMS 最高价格',
  herosms_cancel_on_timeout: '超时取消 HeroSMS',
  proxy: '代理地址',
  rotate_proxy_each_attempt: '每次尝试轮换代理',
  lajiao_proxy_mode: '代理模式',
  lajiao_proxy_credential_protocol: '代理协议',
  lajiao_proxy_credentials: '代理账号',
  lajiao_proxy_credentials_file: '代理账号文件',
  lajiao_proxy_regions: '代理地区',
  lajiao_proxy_timeout: '代理超时',
  lajiao_proxy_expected_country: '期望出口国家',
  mailbox_provider: '默认邮箱服务商',
  mailbox_domain: '邮箱域名',
  mailbox_imap_user: 'IMAP 用户',
  mailbox_imap_pass: 'IMAP 密码',
  mailbox_imap_host: 'IMAP 主机',
  mailbox_imap_port: 'IMAP 端口',
  icloud_privacy_order_file: 'iCloud 隐私邮箱文件',
  cfworker_api_url: 'CF Worker API 地址',
  cfworker_admin_token: 'CF Worker 管理 Token',
  cfworker_domain: 'CF Worker 域名',
  outlook_email: 'Outlook 邮箱',
  outlook_password: 'Outlook 密码',
  outlook_web_otp: 'Outlook 网页验证码',
  outlook_token_order_file: 'Outlook Token 订单文件',
  oauth_client_id: 'OAuth Client ID',
  oauth_redirect_uri: 'OAuth 回调地址',
  oauth_callback_mode: 'OAuth 回调模式',
  cpa_base_url: 'CPA 地址',
  cpa_management_key: 'CPA 管理 Key',
  cpa_manager_admin_key: 'CPAPlus Admin Key',
  cpa_auth_file_sync_url: 'CPA Auth File 同步 URL',
  cpa_sync_ssh_host: 'CPA SSH 主机',
  cpa_sync_container: 'CPA 容器名',
  outlook_cooldown_hours: 'Outlook 冷却小时数',
  iceaix_api_key: '激活服务 API Key',
  iceaix_base_url: '激活服务地址',
  iceaix_sms_api: '激活服务短信 API',
  paypal_phone: '付款手机号',
  iceaix_job_timeout: '激活任务超时',
  plus_verify_retries: '套餐校验重试次数',
  manual_plus_trust_confirmation: '信任手动套餐确认',
  sub2api_url: '外部服务地址',
  sub2api_admin_key: '外部服务管理 Key',
}

const FIELD_HELP: Record<string, string> = {
  headed: '在可见的浏览器窗口中运行任务。',
  browser_mode: '选择浏览器运行模式。',
  browser_engine: '选择用于任务的浏览器内核。',
  browser_channel: '选择可用的浏览器通道。',
  browser_profile_mode: '控制浏览器 profile 的隔离方式。',
  browser_no_viewport: '使用浏览器的默认窗口尺寸。',
  email_register_flow: '选择邮箱注册的执行链路。',
  locale: '浏览器首选语言。',
  timezone_id: '浏览器时区。',
  accept_language: '浏览器请求的 Accept-Language 值。',
  use_camoufox: '当未指定 browser_engine 时，作为兼容的默认浏览器开关。',
  precheck_phone_before_sms: '在发送短信前检查号码状态。服务商和资源池在对应页面维护。',
  prepare_registration_before_phone: '在租用号码前准备注册任务。',
  force_signup_from_login_password: '在密码页切换到注册路径。',
  lajiao_proxy_mode: '选择代理来源模式。',
  lajiao_proxy_credential_protocol: '指定账密代理协议。',
  lajiao_proxy_credentials: '支持粘贴多条代理账号，或在资源池页面批量导入。',
  lajiao_proxy_expected_country: '用于校验代理出口地区。',
  email_otp_timeout: '等待邮箱验证码的最长时间（秒）。',
  email_otp_poll_interval: '轮询邮箱验证码的间隔（秒）。',
  mailbox_provider: '选择邮箱服务商。',
  oauth_callback_mode: '选择本地或远程回调处理方式。',
  cpa_base_url: '远程回调服务的基础地址。',
  cpa_management_key: '远程回调服务的管理凭据；保存后按密码字段隐藏。',
  cpa_manager_admin_key: '远程管理接口的管理员凭据。',
  cpa_auth_file_sync_url: '可选的受保护授权文件同步接口。',
  cpa_sync_ssh_host: '可选的 SSH 同步主机。',
  cpa_sync_container: '可选的同步容器名称。',
  outlook_web_otp: '使用网页方式读取绑定验证码；关闭时手动提供验证码。',
  oauth_redirect_uri: '本地回调模式使用。',
  oauth_client_id: '本地回调模式使用。',
  manual_plus_trust_confirmation: '允许继续处理已手动确认状态的账号。',
  max_parallel_tasks: '所有任务的总并发。日常建议 100；不再设 200 硬上限，按你填的值生效。',
  max_register_tasks: '注册任务并发（与注册页「注册并发」一致）。日常 100；不设硬上限。',
  max_oauth_tasks: 'OAuth 任务的并发上限。',
}

const BOOLEAN_KEYS: Record<string, true> = {
  headed: true,
  use_camoufox: true,
  browser_no_viewport: true,
  camoufox_geoip: true,
  camoufox_humanize: true,
  save_session: true,
  save_tokens: true,
  precheck_phone_before_sms: true,
  prepare_registration_before_phone: true,
  force_signup_from_login_password: true,
  herosms_fixed_price: true,
  herosms_cancel_on_timeout: true,
  rotate_proxy_each_attempt: true,
  outlook_web_otp: true,
}

const NUMBER_KEYS: Record<string, true> = {
  sms_code_timeout: true,
  email_otp_timeout: true,
  email_otp_poll_interval: true,
  max_parallel_tasks: true,
  max_register_tasks: true,
  max_oauth_tasks: true,
  phone_retry_limit: true,
  herosms_max_price: true,
  lajiao_proxy_timeout: true,
  mailbox_imap_port: true,
  outlook_cooldown_hours: true,
  iceaix_job_timeout: true,
  plus_verify_retries: true,
}

const TEXTAREA_KEYS: Record<string, true> = { lajiao_proxy_credentials: true, sms_phone_url: true }
const SECRET_KEYS: Record<string, true> = {
  sms_api_key: true,
  mailbox_imap_pass: true,
  cfworker_admin_token: true,
  outlook_password: true,
  iceaix_api_key: true,
  sub2api_admin_key: true,
  cpa_management_key: true,
  cpa_manager_admin_key: true,
}

const FIELD_OPTIONS: Record<string, { value: string; label: string; help?: string }[]> = {
  browser_mode: [
    { value: 'hybrid', label: 'Hybrid', help: '标准执行模式。' },
    { value: 'camoufox', label: 'Camoufox', help: '兼容执行模式。' },
    { value: 'chromium', label: 'Chromium', help: '基于 Chromium 的执行模式。' },
    { value: 'firefox', label: 'Firefox', help: '基于 Firefox 的兼容执行模式。' }
  ],
  browser_engine: [
    { value: 'patchright', label: 'Patchright / Chrome', help: '基于 Chrome 的执行模式。' },
    { value: 'playwright', label: 'Playwright Chromium', help: '基于 Chromium 的执行模式。' },
    { value: 'camoufox', label: 'Camoufox', help: '基于 Firefox 的兼容执行模式。' },
  ],
  browser_channel: [
    { value: 'msedge', label: 'Microsoft Edge', help: '使用 Edge 浏览器通道。' },
    { value: 'chrome', label: 'Google Chrome', help: '使用 Chrome 浏览器通道。' },
    { value: 'chromium', label: 'Chromium', help: '使用 Chromium 浏览器通道。' },
  ],
  browser_profile_mode: [
    { value: 'per_task', label: '每任务独立 Profile', help: '为每个任务创建独立 profile。' },
    { value: 'none', label: '临时上下文', help: '不保存完整浏览器 profile。' },
  ],
  email_register_flow: [
    { value: 'fast', label: '快速邮箱注册', help: '使用轻量邮箱注册链路。' },
    { value: 'legacy', label: '旧 Camoufox 状态机', help: '兼容注册链路。' },
  ],
  mailbox_provider: [
    { value: 'icloud_api', label: 'iCloud API 邮箱', help: '支持 show、code 和 mail 链接。' },
    { value: 'icloud_privacy', label: 'iCloud 隐私邮箱', help: '通过 IMAP 读取转发验证码。' },
    { value: 'outlook_token', label: 'Outlook Token 邮箱池' },
    { value: 'forwarded_domain', label: '转发域名邮箱' },
    { value: 'cfworker_admin_api', label: 'CFWorker / Cloud Mail' },
  ],
  sms_provider: [
    { value: 'herosms_api', label: 'HeroSMS API' },
    { value: 'user_phone_url', label: '自备手机号 API' },
  ],
  sms_country: [
    { value: 'BR', label: '巴西 BR (+55)' },
    { value: 'US', label: '美国 US (+1)' },
    { value: 'IN', label: '印度 IN (+91)' },
  ],
  sms_service: [
    { value: 'dr', label: 'ChatGPT/OpenAI (dr)' },
  ],
  lajiao_proxy_mode: [
    { value: 'credentials', label: '账密模式' },
    { value: 'api', label: 'API 提取模式' },
  ],
  lajiao_proxy_credential_protocol: [
    { value: 'auto', label: 'Auto' },
    { value: 'socks5h', label: 'SOCKS5H' },
    { value: 'socks5', label: 'SOCKS5' },
    { value: 'http', label: 'HTTP' },
  ],
  bind_sms_provider: [
    { value: 'bind_user_phone_url', label: '绑定手机号池', help: '用于绑定流程的手机号资源池。' },
    { value: 'user_phone_url', label: '注册手机号池（兼容）', help: '兼容已有配置。' },
  ],
  oauth_callback_mode: [
    { value: 'cpa', label: 'CPA 绑定', help: '由远程服务处理回调。' },
    { value: 'local', label: '本地换 RT', help: '由本地流程处理回调。' },
  ],
}

function labelFor(key: string): string {
  return FIELD_LABELS[key] ?? SECTION_TITLES[key] ?? key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function parseConfigValue(key: string, value: string): unknown {
  if (value === '***') return undefined
  if (BOOLEAN_KEYS[key]) {
    if (value === 'true') return true
    if (value === 'false') return false
    return undefined
  }
  if (NUMBER_KEYS[key] && value.trim() !== '') return Number(value)
  return value
}

export default function Settings() {
  const [config, setConfig] = useState<ConfigPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [dirtyKeys, setDirtyKeys] = useState<Set<string>>(() => new Set())

  useEffect(() => {
    let cancelled = false
    getConfig()
      .then((c) => {
        if (!cancelled) {
          setConfig(c)
          const source = { ...(c.config ?? {}), ...(c.file_config ?? {}), ...(c.db_config ?? {}) }
          const flat: Record<string, string> = {}
          for (const [k, v] of Object.entries(source)) {
            flat[k] = v == null ? '' : String(v)
          }
          setDirtyKeys(new Set())
          setValues(flat)
        }
      })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : '配置加载失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const body: Record<string, unknown> = {}
      for (const key of dirtyKeys) {
        const parsed = parseConfigValue(key, values[key] ?? '')
        if (parsed !== undefined) body[key] = parsed
      }
      if (Object.keys(body).length > 0) await saveConfig(body)
      setValues((current) => {
        const next = { ...current }
        for (const key of dirtyKeys) {
          if (SECRET_KEYS[key] && next[key]) next[key] = '***'
        }
        return next
      })
      setDirtyKeys(new Set())
      setSaved(true)
      window.setTimeout(() => setSaved(false), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const setValue = (key: string, val: string) => {
    setValues((prev) => ({ ...prev, [key]: val }))
    setDirtyKeys((prev) => new Set(prev).add(key))
    setSaved(false)
  }


  const placeholderFor = (key: string): string => {
    if (SECRET_KEYS[key]) return '••••••••'
    if (key.includes('url') || key.includes('base')) return 'https://…'
    if (NUMBER_KEYS[key]) return '输入数字…'
    return `请输入${labelFor(key)}…`
  }

  const isLocalOAuthFieldDisabled = (key: string) =>
    values.oauth_callback_mode === 'cpa' && (key === 'oauth_client_id' || key === 'oauth_redirect_uri')

  const renderField = (key: string) => {
    const label = labelFor(key)
    const help = FIELD_HELP[key]
    const value = values[key] ?? ''
    const disabled = isLocalOAuthFieldDisabled(key)
    const options = FIELD_OPTIONS[key]
    const cpaRequiredMissing = values.oauth_callback_mode === 'cpa' && (key === 'cpa_base_url' || key === 'cpa_management_key') && !value
    const commonHeader = (
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-zinc-300">{label}</span>
          {disabled && <span className="rounded-full border border-amber-400/20 bg-amber-400/10 px-2 py-0.5 text-[10px] text-amber-300">CPA 模式无需填写</span>}
        </div>
        {help && <p className="text-xs leading-relaxed text-zinc-500">{help}</p>}
      </div>
    )

    if (BOOLEAN_KEYS[key]) {
      const checked = value === 'true'
      return (
        <div key={key} className="rounded-xl border border-white/10 bg-zinc-950/40 p-3">
          <button
            type="button"
            onClick={() => setValue(key, checked ? 'false' : 'true')}
            className="flex w-full items-start justify-between gap-4 text-left"
          >
            <span className="space-y-1">
              <span className="block text-sm font-medium text-zinc-300">{label}</span>
              {help && <span className="block text-xs leading-relaxed text-zinc-500">{help}</span>}
            </span>
            <span className={`mt-0.5 inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors ${checked ? 'border-emerald-400/40 bg-emerald-500/30' : 'border-white/10 bg-zinc-800'}`}>
              <span className={`h-4 w-4 rounded-full bg-white transition-transform ${checked ? 'translate-x-5' : 'translate-x-1'}`} />
            </span>
          </button>
        </div>
      )
    }

    if (options) {
      return (
        <div key={key} className="space-y-2">
          {commonHeader}
          <Select value={value} onValueChange={(next) => setValue(key, next)}>
            <SelectTrigger className="border-white/10 bg-zinc-900 text-zinc-100">
              <SelectValue placeholder={`选择${label}`} />
            </SelectTrigger>
            <SelectContent>
              {options.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  <span className="flex flex-col">
                    <span>{option.label}</span>
                    {option.help && <span className="text-xs text-zinc-500">{option.help}</span>}
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )
    }

    if (TEXTAREA_KEYS[key]) {
      return (
        <div key={key} className="space-y-2 sm:col-span-2">
          {commonHeader}
          <textarea
            value={value}
            onChange={(e) => setValue(key, e.target.value)}
            placeholder={placeholderFor(key)}
            rows={key === 'lajiao_proxy_credentials' ? 6 : 4}
            className="min-h-24 w-full rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 shadow-sm transition-colors placeholder:text-zinc-500 focus-visible:border-blue-600 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-600"
          />
        </div>
      )
    }
    if (disabled) {
      return (
        <div key={key} className="space-y-2">
          {commonHeader}
          <div className="rounded-md border border-dashed border-amber-400/20 bg-amber-400/5 px-3 py-2 text-sm text-amber-200/80">
            CPA 模式下此项由 CPA 授权链接提供，当前配置不会被绑定流程读取。
          </div>
        </div>
      )
    }

    return (
      <div key={key} className="space-y-2">
        {commonHeader}
        <Input
          value={value}
          onChange={(e) => setValue(key, e.target.value)}
          placeholder={placeholderFor(key)}
          aria-label={label}
          aria-invalid={cpaRequiredMissing || undefined}
          className={cpaRequiredMissing ? 'border-amber-400/50 focus-visible:border-amber-400 focus-visible:ring-amber-400' : undefined}
          type={SECRET_KEYS[key] ? 'password' : NUMBER_KEYS[key] ? 'number' : 'text'}
          autoComplete={SECRET_KEYS[key] ? 'new-password' : undefined}
          step={NUMBER_KEYS[key] ? 'any' : undefined}
        />
        {cpaRequiredMissing && <p className="text-xs text-amber-300">CPA 绑定模式必填，否则无法获取授权链接或提交 callback。</p>}
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="text-zinc-500 animate-pulse">正在加载设置…</div>
      </div>
    )
  }

  if (error && !config) {
    return (
      <div className="flex h-full flex-col items-center justify-center p-8 gap-4">
        <p className="text-red-400">{error}</p>
        <Button variant="outline" onClick={() => window.location.reload()}>重试</Button>
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-zinc-100">设置</h2>
          <p className="text-sm text-zinc-400 mt-1">管理运行时和服务连接配置。枚举项使用下拉，开关项使用切换，长列表使用多行文本。</p>
        </div>
        <div className="flex items-center gap-3">
          {saved && (
            <span role="status" className="text-xs text-emerald-400">✓ 已保存</span>
          )}
          <Button variant="accent" onClick={handleSave} disabled={saving || dirtyKeys.size === 0}>
            <Save className="h-4 w-4" />
            {saving ? '保存中…' : dirtyKeys.size > 0 ? `保存 ${dirtyKeys.size} 项` : '已保存'}
          </Button>
        </div>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">
          {error}
        </div>
      )}

      <Tabs defaultValue="oauth">
        <TabsList className="mb-6 overflow-x-auto">
          {SECTIONS.map((s) => (
            <TabsTrigger key={s.tab} value={s.tab}>
              {s.label}
            </TabsTrigger>
          ))}
        </TabsList>

        {SECTIONS.map(({ tab, keys }) => (
          <TabsContent key={tab} value={tab} className="space-y-5">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center justify-between gap-3 text-base">
                  <span>{SECTION_TITLES[tab]} 设置</span>
                  {tab === 'oauth' && values.oauth_callback_mode === 'cpa' && (
                    <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2 py-0.5 text-xs font-normal text-emerald-300">当前：CPA 绑定</span>
                  )}
                </CardTitle>
              </CardHeader>
              <CardContent className="grid gap-5 sm:grid-cols-2">
                {keys.map(renderField)}
              </CardContent>
            </Card>

          </TabsContent>
        ))}
      </Tabs>
    </div>
  )
}
