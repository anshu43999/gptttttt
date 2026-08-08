import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Search, Download, Eye, Star, Archive, X, Key, Link2, CheckCircle, Monitor, RefreshCw, Save, Power, Mail, Copy, Gauge, KeyRound, Plus, Server, Settings2, Trash2 } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { formatDate } from '@/lib/utils'
import {
  getAccounts,
  importAtAccounts,
  exportAccount,
  exportAccounts,
  exportAtProductsTxt,
  exportPlusProductsTxt,
  getAccountExportFields,
  getAccountTokens,
  markPlusAccount,
  verifyPlusAccount,
  startPlusVerification,
  getPlusVerification,
  cancelPlusVerification,
  checkAccountsHealth,
  archiveAccount,
  archiveAccounts,
  resumeOAuthAccount,
  protocolBindAccount,
  activatePlusAccounts,
  getActivationQueueStats,
  releaseActivation,
  releaseActivations,
  checkResourceCapacity,
  getConfig,
  saveConfig,
  issueActivationClientKey,
  cleanupInvalidAccounts,
  syncCpaToken,
  syncCpaTokens,
  refreshAccountAccessToken,
  refreshAccountAccessTokens,
  bindBillingEmail,
  bindBillingEmails,
  listAccountBrowserSessions,
  openAccountBrowser,
  saveAccountBrowserSession,
  closeAccountBrowserSession,
} from '@/lib/api'
import type { Account, AccountExport, AccountExportField, AccountTokens, ActivationClientKeyIssueResponse, ActivationQueueStats, ActivationStatus, BrowserSessionItem, ConfigPayload, PlusVerificationProgress } from '@/lib/types'
import { rememberPlusVerificationTask } from '@/lib/plusProgressStorage'

const STAGE_LABELS: Record<string, string> = {
  plus: 'ChatGPT Plus',
  manual_plus_confirmed: '已确认 Plus',
  cpa_bound: '已提交 CPA',
  resume_manual: '手动恢复',
  plus_verified_needs_oauth: 'Plus 已验证，待绑定',
  complete: '已完成',
  free: '免费版',
  pending: '待处理',
}

const STATUS_LABELS: Record<string, string> = {
  email_registered: '邮箱已注册，待 Plus',
  manual_plus_required: '等待手动 Plus',
  manual_plus_confirmed: '已确认 Plus',
  cpa_bound: '已提交 CPA',
  resume_manual: '手动恢复',
  plus_verified_needs_oauth: 'Plus 已验证，待绑定',
  complete: '已完成',
  plus: 'Plus',
  free: '免费版',
  pending: '待处理',
}

const PLUS_LABELS: Record<string, string> = {
  unverified: '未校验',
  needs_plus: 'Free',
  manual_confirmed: '手动确认 Plus',
  verified_plus: 'Plus',
  free: 'Free',
  check_failed: '校验失败',
  banned: '封号',
}

const BINDING_LABELS: Record<string, string> = {
  not_ready: '未到绑定阶段',
  pending: '待绑定',
  binding_queued: '排队中',
  binding_started: '绑定中',
  cpa_submitted: 'CPA 已提交',
  bound: '绑定完成',
  failed: '绑定失败',
  archived: '已归档',
}

const EXPORT_LABELS: Record<string, string> = {
  '': '未导出',
  bulk_exported: '已批量导出',
  plus_exported: '已导出plus成品',
  at_exported: '已导出AT成品',
}

type BadgeVariant = 'success' | 'warning' | 'danger' | 'default' | 'secondary'

const ACTIVATION_STATUS_META: Record<ActivationStatus, { label: string; variant: BadgeVariant }> = {
  idle: { label: '未开通', variant: 'secondary' },
  queued: { label: '开通排队', variant: 'warning' },
  reserved: { label: '批次预留', variant: 'warning' },
  submitting: { label: '提交中', variant: 'warning' },
  submit_unknown: { label: '提交待确认', variant: 'warning' },
  submitted: { label: '已提交', variant: 'warning' },
  processing: { label: '开通中', variant: 'warning' },
  verifying: { label: 'Plus 校验中', variant: 'warning' },
  success: { label: '开通成功', variant: 'success' },
  verified: { label: '已验收', variant: 'success' },
  active: { label: '已激活', variant: 'success' },
  failed: { label: '开通失败', variant: 'danger' },
  expired: { label: '已过期', variant: 'danger' },
  releasable: { label: '可释放', variant: 'danger' },
  replace_account: { label: '需换号', variant: 'danger' },
  cancelled: { label: '已取消', variant: 'secondary' },
  released: { label: '已释放', variant: 'secondary' },
  exported: { label: '已导出', variant: 'success' },
  archived: { label: '已归档', variant: 'secondary' },
  skipped: { label: '已跳过', variant: 'secondary' },
}

const ACTIVATION_FILTER_STATUSES: ActivationStatus[] = [
  'queued', 'submit_unknown', 'submitted', 'processing', 'verifying', 'success', 'verified', 'active', 'failed', 'releasable', 'replace_account', 'cancelled', 'released', 'exported', 'archived',
]
const ACTIVE_ACTIVATION_STATUSES: ActivationStatus[] = ['reserved', 'queued', 'submitting', 'submit_unknown', 'submitted', 'processing', 'verifying']

type ActivationEligibilityReason = 'missing_token' | 'registration_unavailable' | 'already_plus' | 'activation_active' | 'activation_complete' | 'replace_account' | 'plus_batch_active' | 'plus_archived'

const ACTIVATION_ELIGIBILITY_META: Record<ActivationEligibilityReason, string> = {
  missing_token: '缺少 access token',
  registration_unavailable: '已归档或注册失败',
  already_plus: '已是 Plus',
  activation_active: '开通任务进行中',
  activation_complete: '已经开通成功',
  replace_account: '远端要求换号',
  plus_batch_active: '已在 Plus 批次中',
  plus_archived: 'Plus 成品已导出归档',
}

function activationMeta(status?: string): { label: string; variant: BadgeVariant } {
  if (!status) return { label: '未开通', variant: 'secondary' }
  return ACTIVATION_STATUS_META[status as ActivationStatus] ?? { label: status, variant: 'secondary' }
}

function shortTaskId(taskId?: string): string {
  const value = String(taskId || '').trim()
  if (!value) return ''
  if (value.length <= 14) return value
  return `${value.slice(0, 6)}…${value.slice(-4)}`
}

function activationDetailText(account: Account): string {
  const status = String(account.activation_status || '')
  // Terminal / settled states: only show the badge. Long error text belongs in detail dialog.
  if (['success', 'verified', 'active', 'failed', 'replace_account', 'cancelled', 'released'].includes(status)) {
    return ''
  }
  if (status === 'submit_unknown') {
    return String(account.activation_display || account.activation_error || '提交结果待确认，系统会自动找回任务号').trim()
  }
  let text = String(account.activation_error || account.activation_display || '').trim()
  // Stale backend message before status=success stopped requiring cdkConsumed==1.
  if (/cdkConsumed\s*!=\s*1|继续轮询/i.test(text) && /status\s*=\s*success/i.test(text)) {
    text = Number(account.activation_cdk_consumed || 0) === 1
      ? '远端已成功，等待下一轮进入 Plus 校验'
      : `远端已成功（cdkConsumed=${Number(account.activation_cdk_consumed || 0)}），等待进入 Plus 校验`
  }
  return text
}
function activationMetaLine(account: Account): string {
  const parts: string[] = []
  if (account.activation_channel) parts.push(String(account.activation_channel).toUpperCase())
  if (account.activation_task_id) parts.push(shortTaskId(account.activation_task_id))
  if (Number(account.activation_cdk_consumed || 0) > 0) parts.push('CDK已核销')
  return parts.join(' · ')
}

function activationEligibilityReason(account: Account): ActivationEligibilityReason | null {
  const tokenSummary = account.tokens ?? {}
  const hasAccessToken = Boolean(tokenSummary.has_access_token || tokenSummary.access_token) || Object.entries(tokenSummary).some(([name, present]) => (
    present && ['accesstoken', 'hasaccesstoken'].includes(name.replace(/[^a-z]/gi, '').toLowerCase())
  ))
  if (!hasAccessToken) return 'missing_token'

  const registrationState = String(account.registration_status || '').toLowerCase()
  const stage = String(account.stage || '').toLowerCase()
  const status = String(account.status || '').toLowerCase()
  if (
    registrationState === 'archived'
    || registrationState === 'failed'
    || ['archived', 'failed', 'error'].includes(stage)
    || ['archived', 'failed', 'error'].includes(status)
  ) return 'registration_unavailable'
  if (account.plus_status === 'verified_plus') return 'already_plus'
  if (account.plus_archived_at || account.plus_export_batch_key) return 'plus_archived'
  if (account.active_plus_batch_key || account.active_plus_batch_id) return 'plus_batch_active'

  const activationStatus = account.activation_status
  if (activationStatus && ACTIVE_ACTIVATION_STATUSES.includes(activationStatus)) return 'activation_active'
  if (activationStatus === 'replace_account') return 'replace_account'
  if (activationStatus && ['active', 'success', 'verified'].includes(activationStatus)) return 'activation_complete'
  return null
}

const TOKEN_FIELDS = [
  'access_token',
  'refresh_token',
  'id_token',
  'chatgpt_access_token_initial',
  'token_expires_at',
] as const

type UpiConfigField =
  | 'upi_activation_enabled'
  | 'upi_base_url'
  | 'upi_device_id'
  | 'upi_default_channel'
  | 'upi_client_key'
  | 'upi_client_keys'
  | 'upi_submit_per_key_per_min'
  | 'upi_poll_interval_sec'
  | 'upi_poll_timeout_sec'
  | 'upi_auto_verify_plus'

interface UpiConfigForm {
  enabled: boolean
  baseUrl: string
  deviceId: string
  defaultChannel: 'upi' | 'pix' | 'ideal'
  primaryKey: string
  additionalKeys: string[]
  submitPerKeyPerMin: string
  pollIntervalSec: string
  pollTimeoutSec: string
  autoVerifyPlus: boolean
}

function configValues(payload: ConfigPayload): Record<string, unknown> {
  return { ...(payload.config ?? {}), ...(payload.file_config ?? {}), ...(payload.db_config ?? {}) }
}

function configKeyList(value: unknown): string[] {
  const raw = Array.isArray(value) ? value : typeof value === 'string' ? value.split(/[\r\n,;]+/) : []
  return raw.map((item) => String(item ?? '').trim()).filter(Boolean)
}

function configBoolean(value: unknown, fallback: boolean): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'string') {
    if (value.toLowerCase() === 'true') return true
    if (value.toLowerCase() === 'false') return false
  }
  return fallback
}

function upiConfigForm(payload: ConfigPayload, stats: ActivationQueueStats): UpiConfigForm {
  const source = configValues(payload)
  const primaryKey = String(source.upi_client_key ?? '').trim()
  const configuredAdditionalKeys = source.upi_client_keys === undefined
    ? stats.config.client_keys.filter((key) => key !== primaryKey)
    : configKeyList(source.upi_client_keys)
  return {
    enabled: configBoolean(source.upi_activation_enabled, stats.config.enabled),
    baseUrl: String(source.upi_base_url ?? ''),
    deviceId: String(source.upi_device_id ?? 'gpt-register'),
    defaultChannel: (['upi', 'pix', 'ideal'].includes(String(source.upi_default_channel ?? 'upi').toLowerCase())
      ? String(source.upi_default_channel ?? 'upi').toLowerCase()
      : 'upi') as UpiConfigForm['defaultChannel'],
    primaryKey,
    additionalKeys: configuredAdditionalKeys,
    submitPerKeyPerMin: String(source.upi_submit_per_key_per_min ?? stats.config.submit_per_key_per_min),
    pollIntervalSec: String(source.upi_poll_interval_sec ?? stats.config.poll_interval_sec),
    pollTimeoutSec: String(source.upi_poll_timeout_sec ?? stats.config.poll_timeout_sec),
    autoVerifyPlus: configBoolean(source.upi_auto_verify_plus, stats.config.auto_verify_plus),
  }
}

const BIND_COUNTRIES = [
  { value: 'US', code: '1', label: '美国 (+1)' },
  { value: 'BR', code: '55', label: '巴西 (+55)' },
  { value: 'JP', code: '81', label: '日本 (+81)' },
] as const

type OAuthCallbackMode = 'local' | 'cpa'
type BindSmsProvider = 'smsbower_api' | 'bind_user_phone_url' | 'user_phone_url'

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

const REGISTRATION_LABELS: Record<string, string> = {
  queued: '注册排队',
  running: '注册中',
  registered: '注册成功',
  failed: '注册失败',
  archived: '已归档',
  unknown: '未知',
}

const HEALTH_STATUS_LABELS: Record<string, string> = {
  active: '正常',
  active_plus: 'Plus 正常',
  active_free: 'Free 正常',
  token_expired: 'Token 过期',
  session_expired: 'Session 过期',
  login_required: '需登录',
  email_verification_required: '需邮箱验证',
  phone_verification_required: '需手机验证',
  identity_verification_required: '需身份验证',
  captcha_required: '需验证码',
  account_suspended: '账号暂停',
  account_disabled: '账号禁用',
  account_deactivated: '账号注销',
  access_denied: '访问拒绝',
  api_forbidden: 'API 403',
  proxy_failed: '代理失败',
  rate_limited: '限流',
  missing_material: '材料缺失',
  unknown: '未知',
}

const HEALTH_STATUS_ORDER = Object.keys(HEALTH_STATUS_LABELS)

function healthStatusLabel(status?: string): string {
  if (!status) return '未检查'
  return HEALTH_STATUS_LABELS[status] ?? status
}

function healthBadgeVariant(status?: string): 'success' | 'warning' | 'danger' | 'default' | 'secondary' {
  if (status === 'active' || status === 'active_plus' || status === 'active_free') return 'success'
  if (status === 'account_suspended' || status === 'account_disabled' || status === 'account_deactivated' || status === 'access_denied' || status === 'proxy_failed') return 'danger'
  if (status === 'unknown' || !status) return 'secondary'
  return 'warning'
}


function formatHealthCounts(items: Array<{ status?: string; health_status?: string; account?: Account }>, counts?: Partial<Record<string, number>>): string {
  const tally = new Map<string, number>()
  if (items.length > 0) {
    items.forEach((item) => {
      const status = item.account?.health_status || item.health_status || item.status || 'unknown'
      tally.set(status, (tally.get(status) || 0) + 1)
    })
  } else if (counts) {
    Object.entries(counts).forEach(([status, count]) => {
      if (count) tally.set(status, count)
    })
  }
  if (tally.size === 0) return '无结果'
  const ordered = [
    ...HEALTH_STATUS_ORDER.filter((status) => tally.has(status)),
    ...Array.from(tally.keys()).filter((status) => !HEALTH_STATUS_ORDER.includes(status)).sort(),
  ]
  return ordered.map((status) => `${healthStatusLabel(status)} ${tally.get(status)}`).join('，')
}

function registrationLabel(account: Account): string {
  const mode = String(account.registration_mode || '').toLowerCase()
  if (mode === 'phone') return '手机号注册'
  if (mode === 'email') return '邮箱注册'
  return account.phone_number || account.sms_phone ? '手机号注册' : account.email ? '邮箱注册' : '未知'
}

function plusBadgeVariant(status?: string): 'success' | 'warning' | 'danger' | 'default' | 'secondary' {
  if (status === 'verified_plus' || status === 'manual_confirmed') return 'success'
  if (status === 'needs_plus' || status === 'unverified') return 'warning'
  if (status === 'check_failed' || status === 'banned') return 'danger'
  return 'secondary'
}

function bindingBadgeVariant(status?: string): 'success' | 'warning' | 'danger' | 'default' | 'secondary' {
  if (status === 'bound' || status === 'cpa_submitted') return 'success'
  if (status === 'pending' || status === 'binding_queued' || status === 'binding_started') return 'warning'
  if (status === 'failed') return 'danger'
  return 'secondary'
}

function exportBadgeVariant(status?: string): 'success' | 'warning' | 'danger' | 'default' | 'secondary' {
  if (status === 'plus_exported' || status === 'at_exported') return 'success'
  if (status === 'bulk_exported') return 'warning'
  return 'secondary'
}



type SortKey = 'registration' | 'phone' | 'password' | 'email' | 'plus' | 'health' | 'binding' | 'export' | 'activation' | 'registration_status' | 'created_at'
type SortDirection = 'asc' | 'desc'

const SORT_LABELS: Record<SortKey, string> = {
  registration: '注册方式',
  phone: '手机号',
  password: '密码',
  email: '邮箱',
  plus: 'Plus 状态',
  health: '账号健康',
  binding: '绑定状态',
  export: '导出状态',
  activation: '开通状态',
  registration_status: '注册状态',
  created_at: '创建时间',
}

function accountEmailDisplay(account: Account): string {
  return account.billing_email || account.codex_email || account.email || ''
}

function accountSortValue(account: Account, key: SortKey): string | number {
  if (key === 'registration') return registrationLabel(account)
  if (key === 'password') return account.has_password || account.password ? '1' : '0'
  if (key === 'phone') return account.phone_number || account.binding_phone_number || account.sms_phone || ''
  if (key === 'email') return accountEmailDisplay(account)
  if (key === 'plus') return PLUS_LABELS[account.plus_status || 'unverified'] ?? account.plus_status ?? ''
  if (key === 'health') return healthStatusLabel(account.health_status)
  if (key === 'binding') return BINDING_LABELS[account.binding_status || 'not_ready'] ?? account.binding_status ?? ''
  if (key === 'export') return EXPORT_LABELS[account.export_status || ''] ?? account.export_status ?? ''
  if (key === 'activation') return activationMeta(account.activation_status).label
  if (key === 'registration_status') return REGISTRATION_LABELS[account.registration_status || 'unknown'] ?? account.registration_status ?? ''
  const time = Date.parse(account.created_at || '')
  return Number.isFinite(time) ? time : 0
}

function compareAccounts(a: Account, b: Account, key: SortKey, direction: SortDirection): number {
  const av = accountSortValue(a, key)
  const bv = accountSortValue(b, key)
  const base = typeof av === 'number' && typeof bv === 'number'
    ? av - bv
    : String(av).localeCompare(String(bv), 'zh-Hans-CN', { numeric: true, sensitivity: 'base' })
  return direction === 'asc' ? base : -base
}


export default function Accounts() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loading, setLoading] = useState(true)
  const [accountsTotal, setAccountsTotal] = useState(0)
  const [accountsTruncated, setAccountsTruncated] = useState(false)
  const [accountsLoadWarning, setAccountsLoadWarning] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('registered')
  const [filterPlus, setFilterPlus] = useState('')
  const [filterBinding, setFilterBinding] = useState('')
  const [filterExport, setFilterExport] = useState('')
  const [filterActivation, setFilterActivation] = useState('')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [tokensOpen, setTokensOpen] = useState(false)
  const [accountTokens, setAccountTokens] = useState<AccountTokens | null>(null)
  const [tokensLoading, setTokensLoading] = useState(false)
  const [tokensError, setTokensError] = useState<string | null>(null)
  const [copiedToken, setCopiedToken] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const [actionWarning, setActionWarning] = useState<string | null>(null)
  const [exportOpen, setExportOpen] = useState(false)
  const [actionFailures, setActionFailures] = useState<Array<{ key: string; message: string }>>([])
  const [exportKey, setExportKey] = useState<string | null>(null)
  const [exportFields, setExportFields] = useState<AccountExportField[]>([])
  const [selectedExportFields, setSelectedExportFields] = useState<string[]>([])
  const [exportLoading, setExportLoading] = useState(false)
  const [plusExportOpen, setPlusExportOpen] = useState(false)
  const [plusExportArchive, setPlusExportArchive] = useState(false)
  const [plusExportLoading, setPlusExportLoading] = useState(false)
  const [atExportOpen, setAtExportOpen] = useState(false)
  const [atExportArchive, setAtExportArchive] = useState(false)
  const [atExportLoading, setAtExportLoading] = useState(false)
  const [atImportOpen, setAtImportOpen] = useState(false)
  const [atImportText, setAtImportText] = useState('')
  const [atImportLoading, setAtImportLoading] = useState(false)
  const [bulkBindOpen, setBulkBindOpen] = useState(false)
  const [bulkActivateOpen, setBulkActivateOpen] = useState(false)
  const [activateChannel, setActivateChannel] = useState<'upi' | 'pix' | 'ideal'>('upi')
  const [activationStats, setActivationStats] = useState<ActivationQueueStats | null>(null)
  const [activationStatsLoading, setActivationStatsLoading] = useState(false)
  const [activationDialogError, setActivationDialogError] = useState<string | null>(null)
  const [activationFailures, setActivationFailures] = useState<Array<{ key: string; message: string }>>([])
  const [releaseLoading, setReleaseLoading] = useState<string | null>(null)
  const [bulkReleaseLoading, setBulkReleaseLoading] = useState(false)
  const [activateLoading, setActivateLoading] = useState(false)
  const [upiConfigOpen, setUpiConfigOpen] = useState(false)
  const [upiConfigLoading, setUpiConfigLoading] = useState(false)
  const [upiConfigSaving, setUpiConfigSaving] = useState(false)
  const [upiConfigError, setUpiConfigError] = useState<string | null>(null)
  const [upiConfigSuccess, setUpiConfigSuccess] = useState<string | null>(null)
  const [upiConfigDraft, setUpiConfigDraft] = useState<UpiConfigForm>(() => ({
    enabled: true,
    baseUrl: '',
    deviceId: 'gpt-register',
    defaultChannel: 'upi',
    primaryKey: '',
    additionalKeys: [],
    submitPerKeyPerMin: '50',
    pollIntervalSec: '5',
    pollTimeoutSec: '1800',
    autoVerifyPlus: true,
  }))
  const [upiDirtyKeys, setUpiDirtyKeys] = useState<Set<UpiConfigField>>(() => new Set())
  const [upiCdk, setUpiCdk] = useState('')
  const [upiKeyNote, setUpiKeyNote] = useState('gpt-register')
  const [upiRotateKey, setUpiRotateKey] = useState(false)
  const [upiIssuingKey, setUpiIssuingKey] = useState(false)
  const [upiIssuedKey, setUpiIssuedKey] = useState<ActivationClientKeyIssueResponse | null>(null)
  const [copiedUpiValue, setCopiedUpiValue] = useState<string | null>(null)
  const [oauthMode, setOauthMode] = useState<OAuthCallbackMode>('cpa')
  const [cpaBaseUrl, setCpaBaseUrl] = useState('')
  const [cpaManagementKey, setCpaManagementKey] = useState('')
  const [bindSmsProvider, setBindSmsProvider] = useState<BindSmsProvider>('bind_user_phone_url')
  const [bindSmsPhoneUrl, setBindSmsPhoneUrl] = useState('')
  const [bindSmsCountry, setBindSmsCountry] = useState('US')
  const [bindSmsService, setBindSmsService] = useState('dr')
  const [bindCountryCode, setBindCountryCode] = useState('1')
  const [bindThreads, setBindThreads] = useState(1)
  const [maxParallelTasks, setMaxParallelTasks] = useState(1)
  const [billingEmailProvider, setBillingEmailProvider] = useState('icloud_api')
  const [plusVerifyTask, setPlusVerifyTask] = useState<PlusVerificationProgress | null>(null)
  const [rowVerifyLoading, setRowVerifyLoading] = useState<string | null>(null)
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [bulkLoading, setBulkLoading] = useState(false)
  const [plusVerifyLoading, setPlusVerifyLoading] = useState(false)
  const [hiddenActivationKeys, setHiddenActivationKeys] = useState<Set<string>>(() => new Set())

  const [plusVerifyRegion, setPlusVerifyRegion] = useState<'JP' | 'VN'>('JP')
  const [healthCheckLoading, setHealthCheckLoading] = useState(false)
  const [browserSessions, setBrowserSessions] = useState<BrowserSessionItem[]>([])
  const [browserSessionsLoading, setBrowserSessionsLoading] = useState(false)
  const [browserActionLoading, setBrowserActionLoading] = useState<string | null>(null)
  const [browserUseProxy, setBrowserUseProxy] = useState(true)
  const [browserSaveOnClose, setBrowserSaveOnClose] = useState(false)
  const [browserTargetUrl, setBrowserTargetUrl] = useState('https://chatgpt.com/')
  const [sortKey, setSortKey] = useState<SortKey>('created_at')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)

  const browserSessionsLoadingRef = useRef(false)
  const selectAllRef = useRef<HTMLInputElement>(null)


  useEffect(() => {
    const controller = new AbortController()
    getAccounts({ signal: controller.signal, withMeta: true, limit: 100000 })
      .then((result) => {
        if (controller.signal.aborted) return
        setAccounts(result.items)
        setAccountsTotal(result.total)
        setAccountsTruncated(result.truncated)
      })
      .catch((err) => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : '账号加载失败') })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })

    Promise.allSettled([getConfig(), getActivationQueueStats({ signal: controller.signal })])
      .then(([configResult, statsResult]) => {
        if (controller.signal.aborted) return
        const warnings: string[] = []
        if (statsResult.status === 'fulfilled') setActivationStats(statsResult.value)
        else warnings.push('UPI 队列状态读取失败')

        if (configResult.status === 'fulfilled') {
          const cfg = configResult.value
          const merged = configValues(cfg)
          const nextOauthMode: OAuthCallbackMode = String(merged.oauth_callback_mode ?? 'cpa') === 'local' ? 'local' : 'cpa'
          const loadedProvider = String(merged.bind_sms_provider ?? merged.sms_provider ?? 'bind_user_phone_url')
          const nextProvider: BindSmsProvider = loadedProvider === 'smsbower' || loadedProvider === 'smsbower_api'
            ? 'smsbower_api'
            : loadedProvider === 'user_phone_url' ? 'user_phone_url' : 'bind_user_phone_url'
          const loadedCountry = String(merged.bind_sms_country ?? (nextProvider === 'smsbower_api' ? 'BR' : 'US'))
          const loadedOauthTasks = Math.max(1, Math.min(100, Number(merged.max_oauth_tasks ?? 1) || 1))
          const loadedParallelTasks = Math.max(1, Math.min(100, Number(merged.max_parallel_tasks ?? loadedOauthTasks) || loadedOauthTasks))
          setOauthMode(nextOauthMode)
          setCpaBaseUrl(String(merged.cpa_base_url ?? ''))
          setCpaManagementKey(String(merged.cpa_management_key ?? ''))
          setBindSmsProvider(nextProvider)
          setBindSmsPhoneUrl(String(merged.bind_sms_phone_url ?? ''))
          setBindSmsCountry(loadedCountry)
          setBindSmsService(String(merged.bind_sms_service ?? merged.sms_service ?? 'dr'))
          setBindCountryCode(String(merged.bind_country_code ?? (loadedCountry === 'BR' ? '55' : '1')))
          setBindThreads(loadedOauthTasks)
          setMaxParallelTasks(Math.max(loadedParallelTasks, loadedOauthTasks))
          if (statsResult.status === 'fulfilled') {
            const nextUpiConfig = upiConfigForm(cfg, statsResult.value)
            setUpiConfigDraft(nextUpiConfig)
            setUpiDirtyKeys(new Set())
            setActivateChannel(nextUpiConfig.defaultChannel)
          }
        } else {
          warnings.push('服务配置读取失败')
        }
        setAccountsLoadWarning(warnings.length > 0 ? `账号列表已加载；${warnings.join('，')}，可在对应操作中重试。` : null)
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!plusVerifyTask?.task_id || !plusVerifyTask.running) return
    const controller = new AbortController()
    let timer: number | undefined

    const poll = async () => {
      try {
        const next = await getPlusVerification(plusVerifyTask.task_id, { signal: controller.signal })
        if (controller.signal.aborted) return
        setPlusVerifyTask(next)
        updateAccountsFromResults(next.results)
        if (next.running) {
          timer = window.setTimeout(poll, 2000)
          return
        }
        setPlusVerifyLoading(false)
        const failed = next.results.length - next.results.filter((item) => item.ok).length
        setActionMessage(`${next.cancelled ? 'Plus 校验已取消' : 'Plus 校验完成'}：已处理 ${next.completed}/${next.total}，Plus/Team ${next.paid} 个，失败 ${failed} 个。`)
      } catch (err) {
        if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return
        setPlusVerifyLoading(false)
        setActionError(err instanceof Error ? err.message : '读取 Plus 校验进度失败')
      }
    }

    timer = window.setTimeout(poll, 800)
    return () => {
      controller.abort()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [plusVerifyTask?.task_id, plusVerifyTask?.running])

  const refreshBrowserSessions = useCallback(async () => {
    if (browserSessionsLoadingRef.current) return
    browserSessionsLoadingRef.current = true
    setBrowserSessionsLoading(true)
    try {
      const res = await listAccountBrowserSessions()
      setBrowserSessions(res.items)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '加载浏览器会话失败')
    } finally {
      browserSessionsLoadingRef.current = false
      setBrowserSessionsLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshBrowserSessions()
    const timer = window.setInterval(refreshBrowserSessions, 30000)
    return () => window.clearInterval(timer)
  }, [refreshBrowserSessions])

  const upsertBrowserSession = (session: BrowserSessionItem) => {
    setBrowserSessions((prev) => {
      const exists = prev.some((item) => item.id === session.id)
      if (exists) return prev.map((item) => item.id === session.id ? session : item)
      return [session, ...prev]
    })
  }

  const handleOpenBrowser = async (account: Account) => {
    setBrowserActionLoading(account.key)
    setActionError(null)
    setActionMessage(null)
    try {
      const res = await openAccountBrowser(account.key, {
        target_url: browserTargetUrl,
        use_saved_proxy: browserUseProxy,
        browser_engine: 'camoufox',
        headed: true,
        save_on_close: browserSaveOnClose,
      })
      upsertBrowserSession(res.session)
      setActionMessage(`已打开浏览器会话：${res.session.account_label}`)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '打开浏览器失败')
    } finally {
      setBrowserActionLoading(null)
    }
  }

  const handleSaveBrowserSession = async (sessionId: string) => {
    setBrowserActionLoading(sessionId)
    setActionError(null)
    try {
      const res = await saveAccountBrowserSession(sessionId)
      upsertBrowserSession(res.session)
      setActionMessage(`已保存浏览器状态：${res.session.saved_path || res.session.storage_file}`)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '保存浏览器状态失败')
    } finally {
      setBrowserActionLoading(null)
    }
  }

  const handleCloseBrowserSession = async (sessionId: string, save: boolean) => {
    setBrowserActionLoading(sessionId)
    setActionError(null)
    try {
      const res = await closeAccountBrowserSession(sessionId, save)
      upsertBrowserSession(res.session)
      setActionMessage(save ? '浏览器状态已保存并关闭。' : '浏览器会话已关闭。')
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '关闭浏览器失败')
    } finally {
      setBrowserActionLoading(null)
    }
  }
  const { filtered, sortedAccounts, totalPages, safePage, pageStart, pageAccounts, selected, selectedAccounts, selectableKeys, allFilteredSelected, somePageSelected } = useMemo(() => {
    const filtered = accounts.filter((a) => {
      const isArchived = a.registration_status === 'archived' || a.stage === 'archived' || a.status === 'archived'
      // Default list hides archived; "注册成功" etc. must not keep archived rows just because registration_status lagged.
      if (!filterStatus && isArchived) return false
      if (hiddenActivationKeys.has(a.key) && !filterActivation) return false
      const occupiedByPlusBatch = Boolean(a.active_plus_batch_key || a.active_plus_batch_id)
      if (occupiedByPlusBatch) return false
      if (search) {
        const q = search.toLowerCase()
        const matchEmail = a.email?.toLowerCase().includes(q)
        const matchPhone = a.sms_phone?.toLowerCase().includes(q)
        const matchKey = a.key?.toLowerCase().includes(q)
        const matchLogin = a.login_identifier?.toLowerCase().includes(q)
        if (!matchEmail && !matchPhone && !matchKey && !matchLogin) return false
      }
      if (filterStatus) {
        if (filterStatus === 'archived') {
          if (!isArchived) return false
        } else {
          if (isArchived) return false
          if ((a.registration_status || a.stage) !== filterStatus) return false
        }
      }
      if (filterPlus && (a.plus_status || 'unverified') !== filterPlus) return false
      if (filterBinding && (a.binding_status || 'not_ready') !== filterBinding) return false
      if (filterExport) {
        const exportStatus = a.export_status || ''
        if (filterExport === 'none') {
          if (exportStatus) return false
        } else if (exportStatus !== filterExport) {
          return false
        }
      }
      if (filterActivation) {
        const activationStatus = a.activation_status || ''
        if (filterActivation === 'none') {
          if (activationStatus && activationStatus !== 'idle') return false
        } else if (activationStatus !== filterActivation) {
          return false
        }
      }
      return true
    })
    const sortedAccounts = [...filtered].sort((a, b) => compareAccounts(a, b, sortKey, sortDirection))
    const totalPages = Math.max(1, Math.ceil(sortedAccounts.length / pageSize))
    const safePage = Math.min(page, totalPages)
    const pageStart = (safePage - 1) * pageSize
    const pageAccounts = sortedAccounts.slice(pageStart, pageStart + pageSize)
    const selected = accounts.find((a) => a.key === selectedKey)
    const selectedKeySet = new Set(selectedKeys)
    const selectedAccounts = accounts.filter((a) => selectedKeySet.has(a.key))
    const selectableKeys = pageAccounts.map((a) => a.key)
    const allFilteredSelected = selectableKeys.length > 0 && selectableKeys.every((key) => selectedKeySet.has(key))
    const somePageSelected = selectableKeys.some((key) => selectedKeySet.has(key))
    return { filtered, sortedAccounts, totalPages, safePage, pageStart, pageAccounts, selected, selectedAccounts, selectableKeys, allFilteredSelected, somePageSelected }
  }, [accounts, filterStatus, search, filterPlus, filterBinding, filterExport, filterActivation, hiddenActivationKeys, sortKey, sortDirection, pageSize, page, selectedKey, selectedKeys])

  const protocolBindableSelectedAccounts = useMemo(
    () => selectedAccounts.filter((account) => isBindable(account) && registrationMode(account) === 'email'),
    [selectedAccounts],
  )

  const activationSelection = useMemo(() => {
    const eligible: Account[] = []
    const excluded: Record<ActivationEligibilityReason, number> = {
      missing_token: 0,
      registration_unavailable: 0,
      already_plus: 0,
      activation_active: 0,
      activation_complete: 0,
      replace_account: 0,
      plus_batch_active: 0,
      plus_archived: 0,
    }
    for (const account of selectedAccounts) {
      const reason = activationEligibilityReason(account)
      if (reason) excluded[reason] += 1
      else eligible.push(account)
    }
    return { eligible, excluded }
  }, [selectedAccounts])

  const activatableSelectedAccounts = activationSelection.eligible
  const activationReady = Boolean(activationStats?.config.enabled && activationStats.config.has_key)
  const activationSuccessCount = ['success', 'verified', 'active'].reduce((total, status) => total + Number(activationStats?.counts[status as ActivationStatus] || 0), 0)
  const activationFailureCount = Number(activationStats?.counts.failed || 0)
  const activationBlockedMessage = !activationStats
    ? '正在读取 UPI 服务配置，暂不可提交。'
    : !activationStats.config.enabled
      ? 'UPI 开通已停用。打开 UPI 配置启用后即可继续。'
      : !activationStats.config.has_key
        ? '尚未配置可用的 UPI Client Key。打开 UPI 配置填写 Key 或使用 CDK 签发。'
        : ''

  const hasActiveActivation = useMemo(() => {
    const localActive = accounts.some((account) => (
      account.activation_status ? ACTIVE_ACTIVATION_STATUSES.includes(account.activation_status) : false
    ))
    const awaitingVerification = Boolean(
      activationStats?.config.auto_verify_plus
      && accounts.some((account) => account.activation_status === 'success'),
    )
    return localActive || awaitingVerification || Number(activationStats?.active || 0) > 0
  }, [accounts, activationStats])

  const refreshActivationState = useCallback(async (showFeedback = false) => {
    if (showFeedback) {
      setActivationStatsLoading(true)
      setActionError(null)
      setActionWarning(null)
    }
    const [accountsResult, statsResult] = await Promise.allSettled([
      getAccounts({ withMeta: true, limit: 100000 }),
      getActivationQueueStats(),
    ])
    if (accountsResult.status === 'fulfilled') {
      setAccounts(accountsResult.value.items)
      setAccountsTotal(accountsResult.value.total)
      setAccountsTruncated(accountsResult.value.truncated)
    }
    if (statsResult.status === 'fulfilled') setActivationStats(statsResult.value)
    if (showFeedback) {
      if (accountsResult.status === 'rejected' && statsResult.status === 'rejected') {
        setActionError('账号与 UPI 队列状态刷新失败。')
      } else if (accountsResult.status === 'rejected' || statsResult.status === 'rejected') {
        setActionWarning(accountsResult.status === 'rejected' ? 'UPI 队列已刷新，但账号列表刷新失败。' : '账号列表已刷新，但 UPI 队列状态刷新失败。')
      } else {
        setActionMessage('UPI 开通状态已刷新。')
      }
      setActivationStatsLoading(false)
    }
  }, [])

  const reloadUpiConfiguration = async () => {
    const [cfg, stats] = await Promise.all([getConfig(), getActivationQueueStats()])
    const next = upiConfigForm(cfg, stats)
    setUpiConfigDraft(next)
    setUpiDirtyKeys(new Set())
    setActivationStats(stats)
    setActivateChannel(next.defaultChannel)
  }

  const openUpiConfigDialog = async () => {
    setUpiConfigOpen(true)
    setUpiConfigLoading(true)
    setUpiConfigError(null)
    setUpiConfigSuccess(null)
    setUpiIssuedKey(null)
    try {
      await reloadUpiConfiguration()
    } catch (err) {
      setUpiConfigError(err instanceof Error ? err.message : 'UPI 配置加载失败')
    } finally {
      setUpiConfigLoading(false)
    }
  }

  const updateUpiConfig = (patch: Partial<UpiConfigForm>, dirtyKey: UpiConfigField) => {
    setUpiConfigDraft((current) => ({ ...current, ...patch }))
    setUpiDirtyKeys((current) => new Set(current).add(dirtyKey))
    setUpiConfigSuccess(null)
  }

  const updateAdditionalUpiKey = (index: number, value: string) => {
    setUpiConfigDraft((current) => ({
      ...current,
      additionalKeys: current.additionalKeys.map((key, keyIndex) => keyIndex === index ? value : key),
    }))
    setUpiDirtyKeys((current) => new Set(current).add('upi_client_keys'))
    setUpiConfigSuccess(null)
  }

  const copyUpiValue = async (copyId: string, value: string) => {
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      setCopiedUpiValue(copyId)
      window.setTimeout(() => setCopiedUpiValue((current) => current === copyId ? null : current), 1800)
    } catch (err) {
      setUpiConfigError(err instanceof Error ? err.message : '复制失败')
    }
  }

  const handleSaveUpiConfig = async () => {
    if (upiDirtyKeys.size === 0) return
    const numberFields: Array<{ key: UpiConfigField; label: string; value: string; max?: number }> = [
      { key: 'upi_submit_per_key_per_min', label: '单 Key 每分钟提交数', value: upiConfigDraft.submitPerKeyPerMin },
      { key: 'upi_poll_interval_sec', label: '轮询间隔', value: upiConfigDraft.pollIntervalSec, max: 300 },
      { key: 'upi_poll_timeout_sec', label: '任务超时', value: upiConfigDraft.pollTimeoutSec },
    ]
    for (const field of numberFields) {
      if (!upiDirtyKeys.has(field.key)) continue
      const parsed = Number(field.value)
      if (!Number.isFinite(parsed) || parsed < 1 || (field.max !== undefined && parsed > field.max)) {
        setUpiConfigError(`${field.label}必须是 1${field.max ? `–${field.max}` : ' 以上'}的数字。`)
        return
      }
    }

    const additionalKeys = upiConfigDraft.additionalKeys.map((key) => key.trim()).filter(Boolean)
    if (upiDirtyKeys.has('upi_client_key') || upiDirtyKeys.has('upi_client_keys')) {
      const allKeys = [upiConfigDraft.primaryKey.trim(), ...additionalKeys].filter(Boolean)
      const invalidKey = allKeys.find((key) => !key.startsWith('actk_'))
      if (invalidKey) {
        setUpiConfigError('Client Key 必须以 actk_ 开头。')
        return
      }
      if (new Set(allKeys).size !== allKeys.length) {
        setUpiConfigError('Client Key 列表存在重复项。')
        return
      }
    }

    const body: Record<string, unknown> = {}
    if (upiDirtyKeys.has('upi_activation_enabled')) body.upi_activation_enabled = upiConfigDraft.enabled
    if (upiDirtyKeys.has('upi_base_url')) body.upi_base_url = upiConfigDraft.baseUrl.trim()
    if (upiDirtyKeys.has('upi_device_id')) body.upi_device_id = upiConfigDraft.deviceId.trim()
    if (upiDirtyKeys.has('upi_default_channel')) body.upi_default_channel = upiConfigDraft.defaultChannel
    if (upiDirtyKeys.has('upi_client_key')) body.upi_client_key = upiConfigDraft.primaryKey.trim()
    if (upiDirtyKeys.has('upi_client_keys')) body.upi_client_keys = additionalKeys
    if (upiDirtyKeys.has('upi_submit_per_key_per_min')) body.upi_submit_per_key_per_min = Number(upiConfigDraft.submitPerKeyPerMin)
    if (upiDirtyKeys.has('upi_poll_interval_sec')) body.upi_poll_interval_sec = Number(upiConfigDraft.pollIntervalSec)
    if (upiDirtyKeys.has('upi_poll_timeout_sec')) body.upi_poll_timeout_sec = Number(upiConfigDraft.pollTimeoutSec)
    if (upiDirtyKeys.has('upi_auto_verify_plus')) body.upi_auto_verify_plus = upiConfigDraft.autoVerifyPlus

    setUpiConfigSaving(true)
    setUpiConfigError(null)
    setUpiConfigSuccess(null)
    try {
      await saveConfig(body)
      setUpiDirtyKeys(new Set())
      setUpiConfigSuccess(`已保存 ${Object.keys(body).length} 项。正在回读配置与队列状态…`)
      try {
        await reloadUpiConfiguration()
        setUpiConfigSuccess(`已保存 ${Object.keys(body).length} 项，并回读配置与队列状态。`)
      } catch (readbackError) {
        setUpiConfigError(`配置已保存，但回读失败：${readbackError instanceof Error ? readbackError.message : '请关闭后重新打开 UPI 配置重试'}`)
      }
    } catch (err) {
      setUpiConfigError(err instanceof Error ? err.message : 'UPI 配置保存失败')
    } finally {
      setUpiConfigSaving(false)
    }
  }

  const handleIssueUpiKey = async () => {
    if (upiDirtyKeys.size > 0) {
      setUpiConfigError('请先保存当前配置更改，再使用 CDK 签发或轮换 Key。')
      return
    }
    const cdk = upiCdk.trim()
    if (!cdk) {
      setUpiConfigError('请输入 CDK。')
      return
    }
    setUpiIssuingKey(true)
    setUpiConfigError(null)
    setUpiConfigSuccess(null)
    setUpiIssuedKey(null)
    try {
      const result = await issueActivationClientKey({ cdk, note: upiKeyNote.trim() || 'gpt-register', rotate: upiRotateKey })
      setUpiIssuedKey(result)
      setUpiCdk('')
      setUpiConfigSuccess('Client Key 已签发并保存。正在回读配置与队列状态…')
      try {
        await reloadUpiConfiguration()
        setUpiConfigSuccess('Client Key 已签发并保存，配置与队列状态已回读。')
      } catch (readbackError) {
        setUpiConfigError(`Client Key 已签发并保存，但回读失败：${readbackError instanceof Error ? readbackError.message : '请关闭后重新打开 UPI 配置重试'}`)
      }
    } catch (err) {
      setUpiConfigError(err instanceof Error ? err.message : 'CDK 签发失败')
    } finally {
      setUpiIssuingKey(false)
    }
  }

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = somePageSelected && !allFilteredSelected
  }, [allFilteredSelected, somePageSelected])

  useEffect(() => {
    if (!hasActiveActivation) return
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      if (stopped) return
      const [accountsResult, statsResult] = await Promise.allSettled([getAccounts({ withMeta: true, limit: 100000 }), getActivationQueueStats()])
      if (!stopped) {
        if (accountsResult.status === 'fulfilled') {
          setAccounts(accountsResult.value.items)
          setAccountsTotal(accountsResult.value.total)
          setAccountsTruncated(accountsResult.value.truncated)
        }
        if (statsResult.status === 'fulfilled') setActivationStats(statsResult.value)
      }
      const intervalMs = Math.max(2000, Math.min(10000, Number(activationStats?.config.poll_interval_sec || 3) * 1000))
      if (!stopped) timer = window.setTimeout(poll, intervalMs)
    }
    timer = window.setTimeout(poll, 1200)
    return () => {
      stopped = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [activationStats?.config.poll_interval_sec, hasActiveActivation])

  const openBulkActivateDialog = () => {
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActivationDialogError(null)
    setActivationFailures([])
    if (activationStats && !activationReady) {
      void openUpiConfigDialog()
      return
    }
    setBulkActivateOpen(true)
  }

  const handleBulkActivate = () => {
    if (!activationReady) {
      setActivationDialogError(activationBlockedMessage)
      return
    }
    // Prefer all selected accounts with tokens; backend skips active/terminal safely.
    // Do not block the operator on eligibility chips — enqueue is async + rate-limited server-side.
    const candidates = selectedAccounts.filter((account) => {
      const reason = activationEligibilityReason(account)
      return reason === null || reason === 'activation_active'
    })
    if (candidates.length === 0) {
      setActivationDialogError('选中的账号中没有可提交项（缺 token / 已是 Plus / 终态）。')
      return
    }
    const keys = candidates.map((account) => account.key)
    const channel = activateChannel
    setActivateLoading(true)
    setActionError(null)
    setActionWarning(null)
    // Do NOT claim success before the request returns — previous optimistic
    // toast caused "已提交后台排队" + red "Failed to fetch" when the HTTP call
    // died mid-flight while the batch was never fully enqueued.
    setActionMessage(`正在提交 ${keys.length} 个 UPI 开通到后台…`)
    setActivationDialogError(null)
    setActivationFailures([])
    // Close immediately — never hold the dialog hostage during network/enqueue.
    setBulkActivateOpen(false)
    void (async () => {
      try {
        const res = await activatePlusAccounts(keys, {
          channel,
          provider: 'upi',
        })
        const resultByKey = new Map((res.results || []).map((result) => [result.key, result]))
        if (resultByKey.size > 0) {
          setAccounts((current) => current.map((account) => {
            const result = resultByKey.get(account.key)
            if (!result) return account
            return {
              ...account,
              ...(result.account ?? {}),
              activation_status: result.activation_status ?? result.account?.activation_status ?? account.activation_status,
              activation_channel: result.activation_channel ?? result.account?.activation_channel ?? account.activation_channel,
            }
          }))
        }
        const submittedKeys = new Set(keys)
        if (submittedKeys.size > 0 && (res.ok || (res.accepted || 0) > 0)) {
          setSelectedKeys((current) => current.filter((key) => !submittedKeys.has(key)))
          setHiddenActivationKeys((current) => new Set([...current, ...submittedKeys]))
        }
        const summary = res.message
          || (res.async
            ? `已接受 ${res.accepted ?? keys.length} 个，后台入队中`
            : `UPI 开通：排队 ${res.queued}，跳过 ${res.skipped}，失败 ${res.failed}`)
        if ((res.failed || 0) > 0 && !res.async) {
          if ((res.queued || 0) > 0) setActionWarning(summary)
          else setActionError(summary)
        } else {
          setActionMessage(summary)
        }
        void refreshActivationState(false)
        // Async enqueue fills the queue over the next few seconds — refresh again.
        if (res.async) {
          window.setTimeout(() => { void refreshActivationState(false) }, 2000)
          window.setTimeout(() => { void refreshActivationState(false) }, 8000)
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : 'UPI 开通提交失败'
        setActionMessage(null)
        setActionError(message)
      } finally {
        setActivateLoading(false)
      }
    })()
  }

  const handleReleaseActivation = async (account: Account) => {
    const cancelLocal = !account.activation_task_id && ['queued', 'submit_unknown'].includes(String(account.activation_status || ''))
    if (!cancelLocal && !account.activation_can_release) return
    const actionLabel = cancelLocal
      ? (account.activation_status === 'submit_unknown' ? '取消待确认开通' : '取消本地排队')
      : '释放远端任务'
    if (!window.confirm(`确定${actionLabel}“${accountEmailDisplay(account) || account.key}”吗？`)) return

    setReleaseLoading(account.key)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    try {
      const result = await releaseActivation(account.key)
      const releasedAccount = result.account
      setAccounts((current) => current.map((item) => item.key === account.key
        ? (releasedAccount ?? {
            ...item,
            activation_status: result.activation_status ?? (cancelLocal ? 'cancelled' : 'released'),
            activation_task_id: result.activation_task_id ?? item.activation_task_id,
            activation_can_release: 0,
            activation_error: '',
            activation_display: cancelLocal ? '已取消本地开通' : item.activation_display,
          })
        : item))
      setActionMessage(result.message || `${actionLabel}成功。`)
      await refreshActivationState(false)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : `${actionLabel}失败`)
    } finally {
      setReleaseLoading(null)
    }
  }

  const handleBulkReleaseActivation = () => {
    const selected = accounts.filter((item) => selectedKeys.includes(item.key))
    const releasable = selected.filter((account) => {
      const status = String(account.activation_status || '')
      const isTerminal = ['succeeded', 'failed', 'cancelled', 'released', 'excluded'].includes(status)
      const canCancelLocal = !account.activation_task_id && ['queued', 'submit_unknown'].includes(status)
      const canReleaseRemote = Boolean(account.activation_can_release) && !isTerminal
      return canCancelLocal || canReleaseRemote
    })
    if (releasable.length === 0) {
      setActionError('选中账号里没有可取消排队/可释放的开通任务（只有「开通排队」或 canRelease=true 的可释放）')
      return
    }
    if (!window.confirm(
      `确定对 ${releasable.length} 个账号执行取消排队/释放远端任务？\n这将释放 UPI API Key 占用。\n（选中 ${selected.length}，可释放 ${releasable.length}）`,
    )) {
      return
    }

    const keys = releasable.map((item) => item.key)
    setBulkReleaseLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    void (async () => {
      try {
        const res = await releaseActivations(keys)
        const byKey = new Map(res.results.map((item) => [item.key, item]))
        setAccounts((current) => current.map((item) => {
          const hit = byKey.get(item.key)
          if (!hit) return item
          if (hit.account) return hit.account
          if (!hit.ok) return item
          return {
            ...item,
            activation_status: hit.activation_status ?? item.activation_status,
            activation_can_release: 0,
            activation_error: '',
            activation_display: hit.message || item.activation_display,
          }
        }))
        const failures = res.results
          .filter((item) => !item.ok)
          .map((item) => ({ key: item.key, message: item.message || '释放失败' }))
        setActionFailures(failures)
        if ((res.failed || 0) > 0) {
          if ((res.released || 0) > 0) setActionWarning(res.message || `已释放 ${res.released}，失败 ${res.failed}`)
          else setActionError(res.message || '批量释放失败')
        } else {
          setActionMessage(res.message || `已释放/取消 ${res.released} 个开通任务`)
        }
        setSelectedKeys((prev) => prev.filter((key) => !byKey.get(key)?.ok))
        void refreshActivationState(false)
      } catch (err) {
        setActionError(err instanceof Error ? err.message : '批量释放失败')
      } finally {
        setBulkReleaseLoading(false)
      }
    })()
  }


  useEffect(() => {
    setPage(1)
  }, [search, filterStatus, filterPlus, filterBinding, filterExport, filterActivation, sortKey, sortDirection, pageSize])

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDirection((prev) => prev === 'asc' ? 'desc' : 'asc')
      return
    }
    setSortKey(key)
    setSortDirection(key === 'created_at' ? 'desc' : 'asc')
  }

  const sortLabel = (key: SortKey) => sortKey === key ? (sortDirection === 'asc' ? ' ↑' : ' ↓') : ''

  const openDetail = (key: string) => {
    setSelectedKey(key)
    setDetailOpen(true)
  }



  const toggleSelected = (key: string) => {
    setSelectedKeys((prev) => prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key])
  }

  const toggleAllFiltered = () => {
    setSelectedKeys((prev) => {
      if (allFilteredSelected) return prev.filter((key) => !selectableKeys.includes(key))
      return Array.from(new Set([...prev, ...selectableKeys]))
    })
  }

  const sanitizedBindOptions = () => Object.fromEntries(
    Object.entries({
      oauth_callback_mode: oauthMode,
      cpa_base_url: cpaBaseUrl,
      cpa_management_key: cpaManagementKey,
      bind_sms_provider: bindSmsProvider,
      bind_sms_phone_url: ['bind_user_phone_url', 'user_phone_url'].includes(bindSmsProvider) ? bindSmsPhoneUrl : '',
      bind_sms_country: bindSmsCountry,
      bind_sms_service: bindSmsService,
      bind_country_code: bindCountryCode,
    }).filter(([, value]) => String(value ?? '') !== '***' && String(value ?? '').trim() !== ''),
  ) as Record<string, string>

  const openBulkBindDialog = () => {
    if (selectedAccounts.length === 0) {
      setActionError('请先选择账号。')
      return
    }
    setActionError(null)
    setActionMessage(null)
    setBulkBindOpen(true)
  }

  const handleBindCountryChange = (country: string) => {
    setBindSmsCountry(country)
    const matched = BIND_COUNTRIES.find((item) => item.value === country)
    if (matched) setBindCountryCode(matched.code)
  }

  const handleBindProviderChange = (provider: string) => {
    const nextProvider = provider as BindSmsProvider
    setBindSmsProvider(nextProvider)
    if (nextProvider === 'smsbower_api') {
      setBindSmsCountry('BR')
      setBindCountryCode('55')
      setBindSmsService('dr')
    }
  }

  const handleBulkBind = async () => {
    const candidates = protocolBindableSelectedAccounts
    if (candidates.length === 0) {
      setActionError('选中的账号中没有可执行协议绑定的邮箱注册账号。')
      return
    }
    setBulkLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    try {
      if (bindSmsProvider === 'bind_user_phone_url') {
        const capacity = await checkResourceCapacity({ need_bind_phone: candidates.length })
        const phone = capacity.resources.find((item) => item.resource_type === 'phone' && item.provider === 'bind_user_phone_url')
        if (phone && !phone.enough) {
          throw new Error(`绑定手机号不足：需要 ${phone.required}，可用 ${phone.available}`)
        }
      }

      const taskOptions = sanitizedBindOptions()
      await saveConfig({
        ...taskOptions,
        binding_method: 'protocol',
        oauth_callback_mode: oauthMode,
        bind_sms_provider: bindSmsProvider,
        bind_sms_country: bindSmsCountry,
        bind_sms_service: bindSmsService,
        max_oauth_tasks: bindThreads,
        max_parallel_tasks: Math.max(maxParallelTasks, bindThreads),
      })

      const candidateKeySet = new Set(candidates.map((account) => account.key))
      const startedKeys: string[] = []
      const failures = selectedAccounts
        .filter((account) => !candidateKeySet.has(account.key))
        .map((account) => ({ key: account.key, message: '不符合协议绑定条件（仅支持可绑定的邮箱注册账号）' }))
      for (const account of candidates) {
        try {
          await protocolBindAccount(account.key, taskOptions)
          startedKeys.push(account.key)
        } catch (err) {
          failures.push({ key: account.key, message: err instanceof Error ? err.message : '协议绑定任务启动失败' })
        }
      }

      const startedKeySet = new Set(startedKeys)
      setSelectedKeys((prev) => prev.filter((key) => !startedKeySet.has(key)))
      setBulkBindOpen(false)
      const destination = oauthMode === 'local' ? '本地 refresh_token' : '直接 CPA'
      const summary = `协议批量绑定（${destination}）：已启动 ${startedKeys.length} 个，失败/跳过 ${failures.length} 个。并发 ${bindThreads}，全局最大并发 ${Math.max(maxParallelTasks, bindThreads)}。`
      if (failures.length > 0) {
        setActionFailures(failures)
        setActionWarning(summary)
      } else {
        setActionMessage(summary)
      }
    } catch (err) {
      setBulkBindOpen(false)
      setActionError(err instanceof Error ? err.message : '批量协议绑定失败')
    } finally {
      setBulkLoading(false)
    }
  }

  const handleBulkBillingEmailBind = async () => {
    if (selectedAccounts.length === 0) {
      setActionError('请先选择账号。')
      return
    }
    setBulkLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    try {
      const requestedKeys = selectedAccounts.map((account) => account.key)
      const res = await bindBillingEmails(requestedKeys, { headed: true, mailbox_provider: billingEmailProvider, proxy_region: 'JP' })
      const successKeys = new Set(res.results.filter((item) => item.ok).map((item) => item.key))
      const resultByKey = new Map(res.results.map((item) => [item.key, item]))
      const failures = requestedKeys
        .filter((key) => !successKeys.has(key))
        .map((key) => ({ key, message: resultByKey.get(key)?.message || '未返回任务启动结果' }))
      setSelectedKeys((current) => current.filter((key) => !successKeys.has(key)))
      const summary = `账单邮箱绑定：已启动 ${successKeys.size} 个，失败 ${failures.length} 个，provider=${billingEmailProvider}。`
      if (failures.length > 0) {
        setActionFailures(failures)
        setActionWarning(summary)
      } else {
        setActionMessage(summary)
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '批量账单邮箱绑定失败')
    } finally {
      setBulkLoading(false)
    }
  }

  const handleBulkArchive = async () => {
    if (selectedKeys.length === 0) {
      setActionError('请先选择账号。')
      return
    }
    if (!window.confirm(`确定删除选中的 ${selectedKeys.length} 个账号吗？账号会从默认列表隐藏，可通过归档状态筛选查看。`)) return
    setBulkLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    try {
      const res = await archiveAccounts(selectedKeys)
      const archivedKeys = new Set(res.keys)
      setAccounts((prev) => prev.filter((account) => !archivedKeys.has(account.key)))
      setAccountsTotal((current) => Math.max(0, current - res.archived))
      setSelectedKeys((current) => current.filter((key) => !archivedKeys.has(key)))
      if (res.missing.length > 0) {
        setActionFailures(res.missing.map((key) => ({ key, message: '未找到账号' })))
        setActionWarning(`已删除 ${res.archived} 个账号，${res.missing.length} 个未找到并保留选择。`)
      } else {
        setActionMessage(`已删除 ${res.archived} 个账号。`)
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '批量删除失败')
    } finally {
      setBulkLoading(false)
    }
  }


  const updateAccountsFromResults = (items: Array<{account?: Account}>) => {
    const updates = new Map(items.filter((item) => item.account).map((item) => [item.account!.key, item.account!]))
    if (updates.size > 0) {
      setAccounts((prev) => prev.map((account) => updates.get(account.key) ?? account))
    }
  }

  const handleBulkVerifyPlus = async () => {
    if (selectedAccounts.length === 0) {
      setActionError('请先选择账号。')
      return
    }
    setPlusVerifyLoading(true)
    setActionError(null)
    setActionMessage(null)
    try {
      const task = await startPlusVerification(selectedAccounts.map((account) => account.key), plusVerifyRegion)
      setPlusVerifyTask(task)
      rememberPlusVerificationTask(task, `账号页批量校验 ${selectedAccounts.length} 个 · ${plusVerifyRegion}`)
      const workers = typeof task.workers === 'number' && task.workers > 0 ? task.workers : 32
      setActionMessage(`Plus 校验已启动：0/${task.total}，Go ${workers} worker，分批推进进度；可到「Plus 进度」页面持续跟踪或重试失败项。`)
    } catch (err) {
      setPlusVerifyLoading(false)
      setActionError(err instanceof Error ? err.message : '批量 Plus 校验失败')
    }
  }

  const handleCancelBulkVerifyPlus = async () => {
    if (!plusVerifyTask?.task_id) return
    try {
      const task = await cancelPlusVerification(plusVerifyTask.task_id)
      setPlusVerifyTask(task)
      setActionMessage('已请求取消 Plus 校验；正在运行的账号会在当前代理请求结束后停止。')
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '取消 Plus 校验失败')
    }
  }

  const handleBulkCheckHealth = async () => {
    if (selectedAccounts.length === 0) {
      setActionError('请先选择账号。')
      return
    }
    setHealthCheckLoading(true)
    setActionError(null)
    setActionMessage(null)
    try {
      const res = await checkAccountsHealth(selectedAccounts.map((account) => account.key))
      updateAccountsFromResults(res.results)
      const checked = res.checked ?? res.results.length
      setActionMessage(`已检查 ${checked} 个账号健康：${formatHealthCounts(res.results, res.counts)}。`)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '批量健康检查失败')
    } finally {
      setHealthCheckLoading(false)
    }
  }


  const handleBulkSyncCpa = async () => {
    if (selectedAccounts.length === 0) {
      setActionError('请先选择账号。')
      return
    }
    setBulkLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    try {
      const requestedKeys = selectedAccounts.map((account) => account.key)
      const res = await syncCpaTokens(requestedKeys)
      updateAccountsFromResults(res.results)
      const successKeys = new Set(res.results.filter((item) => item.ok).map((item) => item.key))
      const resultByKey = new Map(res.results.map((item) => [item.key, item]))
      const failures = requestedKeys
        .filter((key) => !successKeys.has(key))
        .map((key) => ({ key, message: resultByKey.get(key)?.message || '未返回同步结果' }))
      const withRefresh = res.results.filter((item) => item.ok && item.has_refresh_token).length
      setSelectedKeys((current) => current.filter((key) => !successKeys.has(key)))
      const summary = `CPA 同步：成功 ${successKeys.size} 个，refresh_token ${withRefresh} 个，失败 ${failures.length} 个。`
      if (failures.length > 0) {
        setActionFailures(failures)
        setActionWarning(summary)
      } else {
        setActionMessage(summary)
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '批量同步 CPA 失败')
    } finally {
      setBulkLoading(false)
    }
  }


  const handleBulkRefreshAccessToken = async () => {
    if (selectedAccounts.length === 0) {
      setActionError('请先选择账号。')
      return
    }
    setBulkLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    try {
      const requestedKeys = selectedAccounts.map((account) => account.key)
      const res = await refreshAccountAccessTokens(requestedKeys)
      updateAccountsFromResults(res.results)
      const successKeys = new Set(res.results.filter((item) => item.ok).map((item) => item.key))
      const resultByKey = new Map(res.results.map((item) => [item.key, item]))
      const failures = requestedKeys
        .filter((key) => !successKeys.has(key))
        .map((key) => ({ key, message: resultByKey.get(key)?.message || '未返回刷新结果' }))
      setSelectedKeys((current) => current.filter((key) => !successKeys.has(key)))
      const summary = `Access Token 刷新：成功 ${successKeys.size} 个，失败 ${failures.length} 个。`
      if (failures.length > 0) {
        setActionFailures(failures)
        setActionWarning(summary)
      } else {
        setActionMessage(summary)
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '批量刷新 Access Token 失败')
    } finally {
      setBulkLoading(false)
    }
  }

  const handleAction = async (key: string, action: string) => {
    setActionError(null)
    setActionMessage(null)
    if (action === 'verify-plus') setRowVerifyLoading(key)
    try {
      if (action === 'mark-plus') {
        const account = await markPlusAccount(key)
        setAccounts((prev) => prev.map((a) => a.key === key ? account : a))
        setActionMessage('已手动标记为 Plus 已确认；自动校验请使用对勾按钮。')
      } else if (action === 'verify-plus') {
        const res = await verifyPlusAccount(key, plusVerifyRegion)
        setAccounts((prev) => prev.map((a) => a.key === key ? res.account : a))
        setActionMessage(`Plus 自动校验完成：${res.plan_type} (${res.source})，出口 ${plusVerifyRegion}。`)
      } else if (action === 'resume-oauth') {
        const res = await resumeOAuthAccount(key, true, sanitizedBindOptions())
        setActionMessage(`${oauthMode === 'cpa' ? 'CPA 绑定' : 'OAuth 绑定'}任务已启动: ${res.task.id}`)
      } else if (action === 'sync-cpa-token') {
        const res = await syncCpaToken(key)
        setAccounts((prev) => prev.map((a) => a.key === key ? res.account : a))
        setActionMessage(`CPA Token 已同步：${res.file}，refresh_token=${res.has_refresh_token ? '已入库' : '未找到'}`)
      } else if (action === 'refresh-access-token') {
        const res = await refreshAccountAccessToken(key)
        setAccounts((prev) => prev.map((a) => a.key === key ? res.account : a))
        setActionMessage(`Access Token 已刷新：长度 ${res.token_length}，storage=${res.storage_file}${res.proxy_enabled ? '，已复用代理' : ''}`)
      } else if (action === 'bind-billing-email') {
        const res = await bindBillingEmail(key, { headed: true, mailbox_provider: billingEmailProvider, proxy_region: 'JP' })
        setActionMessage(`账单邮箱绑定任务已启动: ${res.task.id}，provider=${billingEmailProvider}`)
      } else if (action === 'archive') {
        const res = await archiveAccount(key)
        if (res.keys.includes(key)) {
          setAccounts((prev) => prev.filter((account) => account.key !== key))
          setAccountsTotal((current) => Math.max(0, current - 1))
          setActionMessage('账号已归档。')
        } else {
          setActionWarning('账号未找到，未从当前列表移除。')
        }
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '操作失败')
    } finally {
      if (action === 'verify-plus') setRowVerifyLoading((current) => current === key ? null : current)
    }
  }

  const handleCleanupInvalid = async () => {
    setActionError(null)
    setActionMessage(null)
    try {
      const res = await cleanupInvalidAccounts()
      const result = await getAccounts({ withMeta: true, limit: 100000 })
      setAccounts(result.items)
      setAccountsTotal(result.total)
      setAccountsTruncated(result.truncated)
      setSelectedKeys([])
      setActionMessage(`已归档 ${res.archived} 条无效账号记录。`)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '清理无效账号失败')
    }
  }

  const handleImportAtAccounts = async () => {
    const text = atImportText.trim()
    if (!text) {
      setActionError('请先粘贴 access token 或 AT 文本。')
      return
    }
    setAtImportLoading(true)
    setActionError(null)
    setActionMessage(null)
    setActionWarning(null)
    try {
      const res = await importAtAccounts(text)
      const result = await getAccounts({ withMeta: true, limit: 100000 })
      setAccounts(result.items)
      setAccountsTotal(result.total)
      setAccountsTruncated(result.truncated)
      setFilterStatus('registered')
      setSelectedKeys([])
      setAtImportOpen(false)
      setAtImportText('')
      setActionMessage(`已导入 ${res.imported} 个 AT 账号。`)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'AT 导入失败')
    } finally {
      setAtImportLoading(false)
    }
  }

  const openExportDialog = async (key: string) => {
    setActionError(null)
    setExportKey(key)
    setExportOpen(true)
    try {
      const fields = exportFields.length > 0 ? exportFields : await getAccountExportFields()
      setExportFields(fields)
      setSelectedExportFields((prev) => prev.length > 0 ? prev : fields.map((field) => field.key))
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '导出字段加载失败')
    }
  }

  const openBulkExportDialog = async () => {
    if (selectedAccounts.length === 0) {
      setActionError('请先选择账号。')
      return
    }
    setActionError(null)
    setExportKey(null)
    setExportOpen(true)
    try {
      const fields = exportFields.length > 0 ? exportFields : await getAccountExportFields()
      setExportFields(fields)
      setSelectedExportFields((prev) => prev.length > 0 ? prev : fields.map((field) => field.key))
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '导出字段加载失败')
    }
  }

  const toggleExportField = (fieldKey: string) => {
    setSelectedExportFields((prev) => (
      prev.includes(fieldKey)
        ? prev.filter((item) => item !== fieldKey)
        : [...prev, fieldKey]
    ))
  }

  const handleExport = async () => {
    const keys = exportKey ? [exportKey] : selectedAccounts.map((account) => account.key)
    if (keys.length === 0) return
    if (selectedExportFields.length === 0) {
      setActionError('请至少选择一个导出字段。')
      return
    }
    setExportLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    try {
      let data: AccountExport | AccountExport[]
      let exportedCount = 1
      let missing: string[] = []
      let exportedKeys: string[] = []
      let metadataWarning = ''
      if (exportKey) {
        data = await exportAccount(exportKey, selectedExportFields)
      } else {
        const result = await exportAccounts(keys, selectedExportFields)
        data = result.products
        exportedCount = result.count
        missing = result.missing
        exportedKeys = result.exported_keys
        if (result.exported_keys.length !== result.count) metadataWarning = `后端报告 count=${result.count}，exported_keys=${result.exported_keys.length}。`
      }
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      const safeName = exportKey ? exportKey : `bulk-${exportedCount}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}`
      anchor.download = `account-${safeName}.json`
      anchor.click()
      URL.revokeObjectURL(url)
      setExportOpen(false)
      if (!exportKey && exportedKeys.length > 0) {
        const exportedKeySet = new Set(exportedKeys)
        setSelectedKeys((current) => current.filter((key) => !exportedKeySet.has(key)))
      }
      try {
        const result = await getAccounts({ withMeta: true, limit: 100000 })
        setAccounts(result.items)
        setAccountsTotal(result.total)
        setAccountsTruncated(result.truncated)
      } catch {
        // Keep download success even if refresh fails.
      }
      if (missing.length > 0) {
        setActionFailures(missing.map((key) => ({ key, message: '未找到账号，未导出' })))
        setActionWarning(`已导出 ${exportedCount} 个账号 JSON，${missing.length} 个未找到。${metadataWarning}`)
      } else if (metadataWarning) {
        setActionWarning(`已导出 ${exportedCount} 个账号 JSON。${metadataWarning}`)
      } else {
        setActionMessage(exportKey ? '账号 JSON 已导出。' : `已导出 ${exportedCount} 个账号 JSON。`)
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '导出失败')
    } finally {
      setExportLoading(false)
    }
  }

  const openPlusExportDialog = () => {
    setPlusExportOpen(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
  }

  const handleExportPlusProductsTxt = async () => {
    const keys = selectedAccounts.map((account) => account.key)
    setPlusExportLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    try {
      const result = await exportPlusProductsTxt(keys, true, plusExportArchive)
      if (!result.count || !result.text) {
        throw new Error(result.message || '没有可导出的 Plus 成品号')
      }
      const blob = new Blob([result.text], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
      a.download = keys.length > 0
        ? `plus-products-selected-${result.count}-${stamp}.txt`
        : `plus-products-all-${result.count}-${stamp}.txt`
      a.click()
      URL.revokeObjectURL(url)
      try {
        const refreshed = await getAccounts({ withMeta: true, limit: 100000 })
        setAccounts(refreshed.items)
        setAccountsTotal(refreshed.total)
        setAccountsTruncated(refreshed.truncated)
      } catch {
        // Keep download success even if refresh fails.
      }
      const kinds = Object.entries(result.kind_counts || {})
        .map(([kind, count]) => `${kind}:${count}`)
        .join(', ')
      const scope = keys.length > 0 ? `选中 ${keys.length} 个` : '全部账号'
      const archivedCount = Number(result.archived || 0)
      const archiveMissing = result.archive_missing || []
      const archivePart = plusExportArchive
        ? `；已归档 ${archivedCount}${archiveMissing.length ? `，归档失败 ${archiveMissing.length}` : ''}`
        : ''
      if (archiveMissing.length > 0) {
        setActionFailures(archiveMissing.map((key) => ({ key, message: '导出成功但归档失败' })))
        setActionWarning(
          `已导出 ${result.count} 条 Plus 成品号 TXT（${scope}${kinds ? `；${kinds}` : ''}；跳过 ${result.skipped_count || 0}${archivePart}）。`,
        )
      } else {
        setActionMessage(
          `已导出 ${result.count} 条 Plus 成品号 TXT（${scope}${kinds ? `；${kinds}` : ''}；跳过 ${result.skipped_count || 0}${archivePart}）。`,
        )
      }
      setPlusExportOpen(false)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '导出 Plus 成品号失败')
    } finally {
      setPlusExportLoading(false)
    }
  }

  const openAtExportDialog = () => {
    setAtExportOpen(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
  }

  const handleExportAtProductsTxt = async () => {
    const keys = selectedAccounts.map((account) => account.key)
    setAtExportLoading(true)
    setActionError(null)
    setActionWarning(null)
    setActionMessage(null)
    setActionFailures([])
    try {
      const result = await exportAtProductsTxt(keys, atExportArchive)
      if (!result.count || !result.text) {
        throw new Error(result.message || '没有可导出的 AT 账号')
      }
      const blob = new Blob([result.text], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
      a.download = keys.length > 0
        ? `at-products-selected-${result.count}-${stamp}.txt`
        : `at-products-all-${result.count}-${stamp}.txt`
      a.click()
      URL.revokeObjectURL(url)
      try {
        const refreshed = await getAccounts({ withMeta: true, limit: 100000 })
        setAccounts(refreshed.items)
        setAccountsTotal(refreshed.total)
        setAccountsTruncated(refreshed.truncated)
      } catch {
        // Keep download success even if refresh fails.
      }
      const kinds = Object.entries(result.kind_counts || {})
        .map(([kind, count]) => `${kind}:${count}`)
        .join(', ')
      const scope = keys.length > 0 ? `选中 ${keys.length} 个` : '全部账号'
      const archivedCount = Number(result.archived || 0)
      const archiveMissing = result.archive_missing || []
      const archivePart = atExportArchive
        ? `；已归档 ${archivedCount}${archiveMissing.length ? `，归档失败 ${archiveMissing.length}` : ''}`
        : ''
      if (archiveMissing.length > 0) {
        setActionFailures(archiveMissing.map((key) => ({ key, message: '导出成功但归档失败' })))
        setActionWarning(
          `已导出 ${result.count} 条 AT 成品号 TXT（${scope}${kinds ? `；${kinds}` : ''}；跳过 ${result.skipped_count || 0}${archivePart}）。`,
        )
      } else {
        setActionMessage(
          `已导出 ${result.count} 条 AT 成品号 TXT（${scope}${kinds ? `；${kinds}` : ''}；跳过 ${result.skipped_count || 0}${archivePart}）。`,
        )
      }
      setAtExportOpen(false)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '导出 AT 成品号失败')
    } finally {
      setAtExportLoading(false)
    }
  }



  const handleViewTokens = async (key: string) => {
    setTokensOpen(true)
    setTokensLoading(true)
    setAccountTokens(null)
    setTokensError(null)
    setCopiedToken(null)
    try {
      setAccountTokens(await getAccountTokens(key))
    } catch (err) {
      setTokensError(err instanceof Error ? err.message : 'Token 加载失败')
    } finally {
      setTokensLoading(false)
    }
  }

  const visibleAccountTokens = accountTokens
    ? TOKEN_FIELDS.map((key) => ({ key, value: String(accountTokens[key] ?? '') }))
    : []

  const copyTokenValue = async (key: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopiedToken(key)
      window.setTimeout(() => setCopiedToken((current) => current === key ? null : current), 1800)
    } catch (err) {
      setTokensError(err instanceof Error ? err.message : '复制失败')
    }
  }

  const copyAllTokens = async () => {
    const text = visibleAccountTokens.map(({ key, value }) => `${key}: ${value}`).join('\n')
    if (!text) return
    await copyTokenValue('all', text)
  }

  const stageColor = (stage?: string): 'success' | 'warning' | 'default' => {
    if (stage === 'plus') return 'success'
    if (stage === 'pending') return 'warning'
    return 'default'
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="text-zinc-500 animate-pulse">正在加载账号…</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center p-8 gap-4">

        <p className="text-red-400">{error}</p>
        <Button variant="outline" onClick={() => window.location.reload()}>重试</Button>
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-xl font-semibold text-zinc-100">账号</h2>
        <p className="mt-1 text-sm text-zinc-400">
          查看和管理已注册账号，共 {accounts.length}{accountsTotal > accounts.length ? ` / ${accountsTotal}` : ''} 个。批量绑定固定使用项目内置 Mailat 协议运行时；在弹窗中选择本地保存 refresh_token 或直接提交 CPA。
        </p>
      </div>

      {accountsLoadWarning && (
        <div role="status" className="rounded-lg border border-amber-400/20 bg-amber-950/20 px-4 py-3 text-sm text-amber-200">{accountsLoadWarning}</div>
      )}
      {accountsTruncated && (
        <div role="alert" className="rounded-lg border border-amber-400/30 bg-amber-950/25 px-4 py-3 text-sm text-amber-100">
          当前仅加载 {accounts.length} / {accountsTotal} 个账号；筛选、选择和批量操作只作用于已加载范围。
        </div>
      )}

      {hiddenActivationKeys.size > 0 && (
        <div role="status" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-cyan-400/20 bg-cyan-950/20 px-4 py-3 text-sm text-cyan-100">
          <span>已临时隐藏 {hiddenActivationKeys.size} 个刚提交 UPI 开通的账号，避免重复选择；设置开通筛选或恢复显示可查看。</span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" asChild><Link to="/plus-progress">查看 Plus 进度</Link></Button>
            <Button variant="ghost" size="sm" onClick={() => setHiddenActivationKeys(new Set())}>恢复显示</Button>
          </div>
        </div>
      )}
      {actionMessage && (
        <div role="status" aria-live="polite" className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-400 flex items-center justify-between">
          <span>{actionMessage}</span>
          <button aria-label="关闭成功通知" onClick={() => setActionMessage(null)} className="text-emerald-400 hover:text-emerald-300">
            <X size={16} />
          </button>
        </div>
      )}

      {actionError && (
        <div role="alert" aria-live="assertive" className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400 flex items-center justify-between">
          <span>{actionError}</span>
          <button aria-label="关闭错误通知" onClick={() => setActionError(null)} className="text-red-400 hover:text-red-300">
            <X size={16} />
          </button>
        </div>
      )}

      {actionWarning && (
        <div role="status" aria-live="polite" className="flex items-center justify-between rounded-lg border border-amber-400/20 bg-amber-950/20 px-4 py-3 text-sm text-amber-300">
          <span>{actionWarning}</span>
          <button aria-label="关闭警告通知" onClick={() => { setActionWarning(null); setActionFailures([]) }} className="text-amber-300 hover:text-amber-200">
            <X size={16} />
          </button>
        </div>
      )}

      {actionFailures.length > 0 && (
        <div className="rounded-lg border border-amber-400/20 bg-black/25 px-4 py-3">
          <div className="text-xs font-semibold uppercase tracking-[0.16em] text-amber-300">失败项已保留选择</div>
          <div className="mt-2 max-h-40 divide-y divide-white/5 overflow-y-auto">
            {actionFailures.map((failure) => (
              <div key={`${failure.key}:${failure.message}`} className="grid gap-2 py-2 text-xs sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
                <span className="truncate font-mono text-zinc-400" title={failure.key}>{failure.key}</span>
                <span className="break-words text-amber-100/80">{failure.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <section aria-label="UPI 开通控制台" className="overflow-hidden rounded-lg border border-white/10 border-l-2 border-l-emerald-400/70 bg-zinc-950 shadow-lg shadow-black/20">
        <div className="flex flex-col xl:flex-row xl:items-stretch">
          <div className="flex min-w-[250px] items-center gap-3 border-b border-white/10 px-4 py-3 xl:border-b-0 xl:border-r">
            <div className={`h-2.5 w-2.5 rounded-full ${activationReady ? 'bg-emerald-400 shadow-[0_0_12px_rgba(52,211,153,0.8)]' : activationStats ? 'bg-amber-400' : 'bg-zinc-600'}`} />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold tracking-wide text-zinc-100">UPI 控制台</span>
                <Badge variant={activationReady ? 'success' : activationStats ? 'danger' : 'secondary'}>
                  {!activationStats ? '读取中' : !activationStats.config.enabled ? '已停用' : activationStats.config.has_key ? '可提交' : '缺少 Key'}
                </Badge>
              </div>
              <div className="mt-1 flex items-center gap-1.5 text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                <Server size={11} /> Worker · {activationStats?.worker_started ? '在线' : '待机'}
              </div>
              {activationBlockedMessage && <p className="mt-1 truncate text-xs text-amber-300/75" title={activationBlockedMessage}>{activationBlockedMessage}</p>}
            </div>
          </div>

          <div className="grid flex-1 grid-cols-2 divide-x divide-y divide-white/10 sm:grid-cols-4 sm:divide-y-0">
            {[
              { label: '活动', value: activationStats?.active ?? '—', tone: 'text-cyan-300' },
              { label: 'Key', value: activationStats?.config.key_count ?? '—', tone: 'text-zinc-100' },
              { label: '成功', value: activationStats ? activationSuccessCount : '—', tone: 'text-emerald-300' },
              { label: '失败', value: activationStats ? activationFailureCount : '—', tone: 'text-red-300' },
            ].map((metric) => (
              <div key={metric.label} className="flex min-h-16 flex-col justify-center bg-black/15 px-4 py-2">
                <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-zinc-600">{metric.label}</span>
                <span className={`mt-0.5 font-mono text-lg font-semibold tabular-nums ${metric.tone}`}>{metric.value}</span>
              </div>
            ))}
          </div>

          <div className="flex items-center justify-end gap-2 border-t border-white/10 px-3 py-3 xl:border-l xl:border-t-0">
            <Button size="sm" onClick={() => void openUpiConfigDialog()}>
              <Settings2 size={14} /> UPI 配置
            </Button>
            <Button variant="outline" size="sm" onClick={() => void refreshActivationState(true)} disabled={activationStatsLoading}>
              <RefreshCw size={14} className={activationStatsLoading ? 'animate-spin' : ''} />
              {activationStatsLoading ? '刷新中…' : '刷新'}
            </Button>
          </div>
        </div>
      </section>

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="按邮箱、手机号或账号键搜索…"
            className="pl-9"
          />
        </div>
        <Button variant="outline" onClick={() => setAtImportOpen(true)} disabled={atImportLoading}>
          <KeyRound size={16} /> 导入 AT
        </Button>
        <select
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
          className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-600"
        >
          <option value="">全部注册状态（含失败日志）</option>
          {Object.entries(REGISTRATION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <select
          value={filterPlus}
          onChange={(e) => setFilterPlus(e.target.value)}
          className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-600"
        >
          <option value="">全部 Plus</option>
          {Object.entries(PLUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <select
          value={filterBinding}
          onChange={(e) => setFilterBinding(e.target.value)}
          className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-600"
        >
          <option value="">全部绑定</option>
          {Object.entries(BINDING_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <select
          value={filterExport}
          onChange={(e) => setFilterExport(e.target.value)}
          className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-600"
          title="按导出状态筛选"
        >
          <option value="">全部导出</option>
          <option value="none">未导出</option>
          <option value="bulk_exported">已批量导出</option>
          <option value="plus_exported">已导出plus成品</option>
          <option value="at_exported">已导出AT成品</option>
        </select>
        <select
          value={filterActivation}
          onChange={(e) => setFilterActivation(e.target.value)}
          className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-600"
          title="按开通状态筛选"
        >
          <option value="">全部开通</option>
          <option value="none">未开通</option>
          {ACTIVATION_FILTER_STATUSES.map((status) => (
            <option key={status} value={status}>{ACTIVATION_STATUS_META[status].label}</option>
          ))}
        </select>
        <select
          value={billingEmailProvider}
          onChange={(e) => setBillingEmailProvider(e.target.value)}
          className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-600"
          title="账单邮箱 provider"
        >
          <option value="icloud_api">iCloud API 邮箱</option>

          <option value="icloud_privacy">iCloud 转发邮箱（旧）</option>
          <option value="forwarded_domain">转发域名</option>
        </select>
        <Button variant="outline" onClick={openBulkExportDialog} disabled={bulkLoading || selectedKeys.length === 0}>
          {`批量导出 ${selectedKeys.length}`}
        </Button>
        <Button
          variant="outline"
          onClick={openPlusExportDialog}
          disabled={plusExportLoading || bulkLoading}
          title={selectedKeys.length > 0 ? '导出选中账号里校验成功的 Plus 成品号为 TXT' : '导出全部校验成功的 Plus 成品号为 TXT'}
        >
          {plusExportLoading
            ? '导出 Plus TXT…'
            : (selectedKeys.length > 0 ? `导出 Plus 成品号 ${selectedKeys.length}` : '导出全部 Plus 成品号')}
        </Button>
        <Button
          variant="outline"
          onClick={openAtExportDialog}
          disabled={atExportLoading || bulkLoading}
          title={selectedKeys.length > 0 ? '导出选中账号：邮箱四段----access_token（不校验 Plus）' : '导出全部有 AT 的账号：邮箱四段----access_token（不校验 Plus）'}
        >
          {atExportLoading
            ? '导出 AT TXT…'
            : (selectedKeys.length > 0 ? `导出 AT 成品号 ${selectedKeys.length}` : '导出全部 AT 成品号')}
        </Button>
        <select
          value={plusVerifyRegion}
          onChange={(e) => setPlusVerifyRegion(e.target.value as 'JP' | 'VN')}
          className="h-9 rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-600"
          title="Plus 校验代理出口"
        >
          <option value="JP">校验出口：日本 JP</option>
          <option value="VN">校验出口：越南 VN</option>
        </select>
        <Button onClick={handleBulkVerifyPlus} disabled={plusVerifyLoading || selectedKeys.length === 0}>
          {plusVerifyLoading ? '正在校验…' : `批量校验 Plus ${selectedKeys.length}`}
        </Button>
        <Button variant="outline" onClick={handleBulkCheckHealth} disabled={healthCheckLoading || selectedKeys.length === 0}>
          {healthCheckLoading ? '检查中…' : `批量健康检查 ${selectedKeys.length}`}
        </Button>
        <Button onClick={openBulkBindDialog} disabled={bulkLoading || selectedKeys.length === 0}>
          {bulkLoading ? '正在启动…' : `批量绑定选中 ${selectedKeys.length}`}
        </Button>
        <Button variant={activationReady ? 'default' : 'outline'} onClick={openBulkActivateDialog} disabled={activateLoading || selectedKeys.length === 0}>
          {activateLoading
            ? '提交开通…'
            : !activationStats
              ? '读取 UPI 配置…'
              : !activationStats.config.enabled
                ? 'UPI 已停用'
                : !activationStats.config.has_key
                  ? 'UPI 缺少 Key'
                  : `批量开通 Plus ${selectedKeys.length}`}
        </Button>
        <Button
          variant="outline"
          onClick={handleBulkReleaseActivation}
          disabled={bulkReleaseLoading || activateLoading || selectedKeys.length === 0}
          title="取消本地开通排队 / 释放远端 UPI 任务，释放 API Key 占用"
        >
          {bulkReleaseLoading ? '释放中…' : `批量释放开通 ${selectedKeys.length}`}
        </Button>
        <Button variant="outline" onClick={handleBulkBillingEmailBind} disabled={bulkLoading || selectedKeys.length === 0}>
          {bulkLoading ? '启动中…' : `批量账单邮箱 ${selectedKeys.length}`}
        </Button>
        <Button variant="outline" onClick={handleBulkArchive} disabled={bulkLoading || selectedKeys.length === 0}>
          {bulkLoading ? '处理中…' : `批量删除 ${selectedKeys.length}`}
        </Button>
        <Button variant="outline" onClick={handleBulkSyncCpa} disabled={bulkLoading || selectedKeys.length === 0}>
          {bulkLoading ? '同步中…' : `同步 CPA 到本地 ${selectedKeys.length}`}
        </Button>
        <Button variant="outline" onClick={handleBulkRefreshAccessToken} disabled={bulkLoading || selectedKeys.length === 0}>
          {bulkLoading ? '刷新中…' : `刷新 Access Token ${selectedKeys.length}`}
        </Button>
        <Button variant="outline" onClick={handleCleanupInvalid}>清理无效账号</Button>
        {selectedKeys.length > 0 && (
          <Button variant="ghost" onClick={() => setSelectedKeys([])}>清空选择</Button>
        )}
      </div>

      {plusVerifyTask && (
        <div className="rounded-xl border border-cyan-400/20 bg-cyan-950/20 p-4 text-sm text-cyan-100 shadow-lg shadow-cyan-950/10">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="font-semibold text-cyan-200">Plus 校验进度</div>
              <div className="text-xs text-cyan-100/70">
                {plusVerifyTask.completed}/{plusVerifyTask.total} 已完成，Plus/Team {plusVerifyTask.paid}，失败 {plusVerifyTask.failed}
                {typeof plusVerifyTask.workers === 'number' && plusVerifyTask.workers > 0 ? `，${plusVerifyTask.workers} 并发` : ''}
                {plusVerifyTask.running
                  ? (plusVerifyTask.in_flight_keys?.length
                      ? `，运行中 ${plusVerifyTask.in_flight_keys.length}`
                      : '，排队启动中…')
                  : (plusVerifyTask.cancelled ? '，已取消' : '')}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" asChild>
                <Link to={`/plus-progress?task=${encodeURIComponent(plusVerifyTask.task_id)}`}>打开进度页</Link>
              </Button>
              {plusVerifyTask.running && !plusVerifyTask.cancelled && (
                <Button variant="outline" onClick={handleCancelBulkVerifyPlus}>取消校验</Button>
              )}
              {plusVerifyTask.running && plusVerifyTask.cancelled && (
                <span className="text-xs text-amber-300">取消中…</span>
              )}
            </div>
          </div>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-cyan-950/60">
            <div
              className="h-full rounded-full bg-cyan-300 transition-all"
              style={{ width: `${plusVerifyTask.total ? Math.round((plusVerifyTask.completed / plusVerifyTask.total) * 100) : 0}%` }}
            />
          </div>
          {plusVerifyTask.results.length > 0 && (
            <div className="mt-3 max-h-28 overflow-auto text-xs text-cyan-100/75">
              {plusVerifyTask.results.slice(-5).map((item) => (
                <div key={item.key} className="flex justify-between gap-3 border-t border-cyan-400/10 py-1 first:border-t-0">
                  <span className="truncate">{item.key}</span>
                  <span className={item.ok ? 'text-emerald-300' : 'text-amber-300'}>{item.ok ? (item.plan_type || 'ok') : (item.message || '失败')}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <Card className="border-cyan-400/10 bg-gradient-to-br from-zinc-950 via-zinc-950 to-cyan-950/20">
        <CardContent className="space-y-4 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-medium text-cyan-100">
                <Monitor size={16} /> 账号浏览器会话
              </div>
              <p className="mt-1 text-xs text-zinc-500">用账号保存的 storage_state 打开 ChatGPT；保存会先生成 .bak 备份，不会把 cookie/token 显示到前端。</p>
            </div>
            <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-400">
              <label className="flex items-center gap-2 rounded-md border border-white/10 bg-black/20 px-3 py-2">
                <input type="checkbox" checked={browserUseProxy} onChange={(e) => setBrowserUseProxy(e.target.checked)} className="h-4 w-4 rounded border-white/20 bg-zinc-900 text-cyan-500" />
                优先复用账号代理
              </label>
              <label className="flex items-center gap-2 rounded-md border border-white/10 bg-black/20 px-3 py-2">
                <input type="checkbox" checked={browserSaveOnClose} onChange={(e) => setBrowserSaveOnClose(e.target.checked)} className="h-4 w-4 rounded border-white/20 bg-zinc-900 text-cyan-500" />
                关闭时保存 storage
              </label>
              <Input value={browserTargetUrl} onChange={(e) => setBrowserTargetUrl(e.target.value)} className="h-8 w-56" />
              <Button variant="outline" size="sm" onClick={refreshBrowserSessions} disabled={browserSessionsLoading}>
                <RefreshCw size={14} /> 刷新会话
              </Button>
            </div>
          </div>
          {browserSessions.length === 0 ? (
            <div className="rounded-lg border border-dashed border-white/10 bg-black/20 px-4 py-3 text-xs text-zinc-500">暂无打开的账号浏览器。点击账号行里的显示器图标启动。</div>
          ) : (
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {browserSessions.map((session) => {
                const active = session.status === 'active' || session.status === 'launching'
                return (
                  <div key={session.id} className="rounded-xl border border-white/10 bg-black/30 p-3 shadow-inner shadow-cyan-950/20">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-zinc-100" title={session.account_label}>{session.account_label}</p>
                        <p className="mt-1 truncate font-mono text-[11px] text-zinc-500" title={session.storage_file}>{session.storage_file}</p>
                      </div>
                      <Badge variant={session.status === 'active' ? 'success' : session.status === 'failed' ? 'danger' : session.status === 'closed' ? 'secondary' : 'warning'}>{session.status}</Badge>
                    </div>
                    <div className="mt-2 space-y-1 text-xs text-zinc-400">
                      <p className="truncate" title={session.message}>{session.message || '—'}</p>
                      <p>引擎：{session.engine} · 代理：{session.proxy_enabled ? session.proxy_hint || '已启用' : '未启用'}</p>
                      {session.saved_at && <p className="truncate text-emerald-400" title={session.saved_path}>已保存：{session.saved_path}</p>}
                      {session.error && <p className="truncate text-red-400" title={session.error}>{session.error}</p>}
                    </div>
                    <div className="mt-3 flex justify-end gap-2">
                      <Button variant="ghost" size="sm" disabled={!active || browserActionLoading === session.id} onClick={() => handleSaveBrowserSession(session.id)}>
                        <Save size={14} /> 保存
                      </Button>
                      <Button variant="outline" size="sm" disabled={!active || browserActionLoading === session.id} onClick={() => handleCloseBrowserSession(session.id, false)}>
                        <Power size={14} /> 关闭
                      </Button>
                      <Button variant="destructive" size="sm" disabled={!active || browserActionLoading === session.id} onClick={() => handleCloseBrowserSession(session.id, true)}>
                        保存并关闭
                      </Button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {filtered.length === 0 ? (
            <p className="text-sm text-zinc-500 py-12 text-center">
              {search || filterStatus ? '没有匹配的账号。' : '暂无账号。'}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1500px] table-fixed text-sm">
                <thead>
                  <tr className="border-b border-white/5 text-left text-xs text-zinc-500">
                    <th className="w-12 whitespace-nowrap py-3 px-3 font-medium">
                      <input
                        ref={selectAllRef}
                        aria-label="选择或取消选择当前页全部账号"
                        type="checkbox"
                        checked={allFilteredSelected}
                        onChange={toggleAllFiltered}
                        className="h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
                      />
                    </th>
                    {([
                      { key: 'registration', className: 'w-[76px]' },
                      { key: 'phone', className: 'w-[104px]' },
                      { key: 'password', className: 'w-[120px]' },
                      { key: 'email', className: 'w-[200px]' },
                      { key: 'plus', className: 'w-[76px]' },
                      { key: 'health', className: 'w-[76px]' },
                      { key: 'binding', className: 'w-[100px]' },
                      { key: 'export', className: 'w-[108px]' },
                      { key: 'activation', className: 'w-[100px]' },
                      { key: 'registration_status', className: 'w-[80px]' },
                      { key: 'created_at', className: 'w-[108px]' },
                    ] as Array<{ key: SortKey; className: string }>).map(({ key, className }) => (
                      <th key={key} className={`whitespace-nowrap py-3 px-2 font-medium ${className}`}>
                        <button type="button" onClick={() => toggleSort(key)} className="inline-flex items-center gap-1 whitespace-nowrap text-left transition-colors hover:text-zinc-200" title={`${SORT_LABELS[key]}排序`}>
                          <span className="whitespace-nowrap">{SORT_LABELS[key]}</span>
                          <span className="w-3 shrink-0 text-cyan-400">{sortLabel(key)}</span>
                        </button>
                      </th>
                    ))}
                    <th className="w-[280px] whitespace-nowrap py-3 px-2 font-medium text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {pageAccounts.map((a) => (
                    <tr
                      key={a.key}
                      className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]"
                    >
                      <td className="py-2.5 px-3">
                        <input
                          type="checkbox"
                          aria-label={`选择账号 ${accountEmailDisplay(a) || a.key}`}
                          checked={selectedKeys.includes(a.key)}
                          onChange={() => toggleSelected(a.key)}
                          className="h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
                        />
                      </td>
                      <td className="whitespace-nowrap py-2.5 px-2 text-zinc-400">{registrationLabel(a)}</td>
                      <td className="truncate whitespace-nowrap py-2.5 px-2 text-zinc-300" title={a.phone_number || a.binding_phone_number || a.sms_phone || ''}>{a.phone_number || a.binding_phone_number || a.sms_phone || '—'}</td>
                      <td className="truncate py-2.5 px-2 font-mono text-xs text-zinc-300" title={a.has_password || a.password ? '已设置密码' : ''}>{a.has_password || a.password ? '••••••••' : '—'}</td>
                      <td className="truncate py-2.5 px-2 text-zinc-300" title={accountEmailDisplay(a)}>
                        {accountEmailDisplay(a) || '—'}
                      </td>
                      <td className="whitespace-nowrap py-2.5 px-2">
                        <Badge className="whitespace-nowrap" variant={plusBadgeVariant(a.plus_status)}>
                          {PLUS_LABELS[a.plus_status || 'unverified'] ?? a.plus_status ?? '未校验'}
                        </Badge>
                      </td>
                      <td className="whitespace-nowrap py-2.5 px-2">
                        <Badge className="whitespace-nowrap" variant={healthBadgeVariant(a.health_status)}>
                          {healthStatusLabel(a.health_status)}
                        </Badge>
                      </td>
                      <td className="whitespace-nowrap py-2.5 px-2">
                        <Badge className="whitespace-nowrap" variant={bindingBadgeVariant(a.binding_status)}>
                          {BINDING_LABELS[a.binding_status || 'not_ready'] ?? a.binding_status ?? '未到绑定阶段'}
                        </Badge>
                      </td>
                      <td className="whitespace-nowrap py-2.5 px-2">
                        <Badge
                          className="whitespace-nowrap"
                          variant={exportBadgeVariant(a.export_status)}
                          title={a.exported_at ? `导出时间 ${a.exported_at}` : undefined}
                        >
                          {EXPORT_LABELS[a.export_status || ''] ?? a.export_status ?? '未导出'}
                        </Badge>
                      </td>
                      <td className="py-2.5 px-2 align-middle">
                        {(() => {
                          const meta = activationMeta(a.activation_status)
                          const status = String(a.activation_status || '')
                          const isTerminal = ['success', 'verified', 'active', 'failed', 'replace_account', 'cancelled', 'released'].includes(status)
                          const metaLine = isTerminal ? '' : activationMetaLine(a)
                          const detail = activationDetailText(a)
                          const fullTask = String(a.activation_task_id || '').trim()
                          const canCancelLocal = !a.activation_task_id && ['queued', 'submit_unknown'].includes(status)
                          // Only show release for non-terminal canRelease states (processing etc.).
                          const canReleaseRemote = Boolean(a.activation_can_release) && !isTerminal
                          const showAction = canCancelLocal || canReleaseRemote
                          const actionLabel = releaseLoading === a.key
                            ? '处理中…'
                            : canCancelLocal && status === 'submit_unknown'
                              ? '取消待确认'
                              : canCancelLocal
                                ? '取消排队'
                                : '释放'
                          return (
                            <div className="flex min-w-0 flex-col gap-1.5">
                              <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                                <Badge className="shrink-0 whitespace-nowrap" variant={meta.variant}>
                                  {meta.label}
                                </Badge>
                                {metaLine ? (
                                  <span
                                    className="min-w-0 truncate font-mono text-[11px] leading-4 text-zinc-500"
                                    title={fullTask || metaLine}
                                  >
                                    {metaLine}
                                  </span>
                                ) : null}
                              </div>
                              {detail ? (
                                <p
                                  className={`line-clamp-2 break-words text-[11px] leading-4 ${
                                    a.activation_status === 'submit_unknown'
                                      ? 'text-amber-300/90'
                                      : a.activation_error
                                        ? 'text-red-400/90'
                                        : 'text-zinc-500'
                                  }`}
                                  title={detail}
                                >
                                  {detail}
                                </p>
                              ) : null}
                              {showAction ? (
                                <div>
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    className="h-7 px-2 text-[11px]"
                                    onClick={() => handleReleaseActivation(a)}
                                    disabled={releaseLoading === a.key}
                                  >
                                    {actionLabel}
                                  </Button>
                                </div>
                              ) : null}
                            </div>
                          )
                        })()}
                      </td>
                      <td className="whitespace-nowrap py-2.5 px-2 text-xs text-zinc-400">
                        {REGISTRATION_LABELS[a.registration_status || 'unknown'] ?? a.registration_status ?? '未知'}
                      </td>
                      <td className="whitespace-nowrap py-2.5 px-2 text-xs text-zinc-500">
                        {formatDate(a.created_at)}
                      </td>
                      <td className="whitespace-nowrap py-2.5 px-2">
                        <div className="flex flex-nowrap items-center justify-end gap-0.5">
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleOpenBrowser(a)}
                            disabled={browserActionLoading === a.key}
                            title="用指纹浏览器打开该账号"
                          >
                            <Monitor size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => openDetail(a.key)}
                            title="查看详情"
                          >
                            <Eye size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleViewTokens(a.key)}
                            disabled={tokensLoading}
                            title="查看 Token"
                            aria-label="查看 Token"
                          >
                            <Key size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => openExportDialog(a.key)}
                            title="导出"
                          >
                            <Download size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleAction(a.key, 'verify-plus')}
                            disabled={rowVerifyLoading === a.key || plusVerifyLoading}
                            title="自动校验 Plus"
                          >
                            {rowVerifyLoading === a.key ? <RefreshCw size={15} className="animate-spin" /> : <CheckCircle size={15} />}
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleAction(a.key, 'mark-plus')}
                            title="标记 Plus"
                          >
                            <Star size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleAction(a.key, 'resume-oauth')}
                            title="继续 OAuth 绑定"
                          >
                            <Link2 size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleAction(a.key, 'refresh-access-token')}
                            title="用缓存浏览器 session 获取最新 Access Token"
                          >
                            <RefreshCw size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleAction(a.key, 'bind-billing-email')}
                            title={`绑定账单邮箱 (${billingEmailProvider})`}
                          >
                            <Mail size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleAction(a.key, 'sync-cpa-token')}
                            title="同步 CPA Token 到本地"
                          >
                            <Key size={15} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleAction(a.key, 'archive')}
                            title="归档"
                          >
                            <Archive size={15} />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/5 px-4 py-3 text-xs text-zinc-400">
                <div>
                  共 {sortedAccounts.length} 个账号，当前 {sortedAccounts.length === 0 ? 0 : pageStart + 1}-{Math.min(pageStart + pageSize, sortedAccounts.length)}，第 {safePage}/{totalPages} 页
                </div>
                <div className="flex items-center gap-2">
                  <span>每页</span>
                  <select
                    value={pageSize}
                    onChange={(e) => setPageSize(Number(e.target.value))}
                    className="h-8 rounded-md border border-white/10 bg-zinc-900 px-2 text-xs text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-600"
                  >
                    {[10, 25, 50, 100, 200, 500, 1000].map((size) => <option key={size} value={size}>{size}</option>)}
                  </select>
                  <Button variant="outline" size="sm" onClick={() => setPage(1)} disabled={safePage <= 1}>首页</Button>
                  <Button variant="outline" size="sm" onClick={() => setPage((prev) => Math.max(1, prev - 1))} disabled={safePage <= 1}>上一页</Button>
                  <Button variant="outline" size="sm" onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))} disabled={safePage >= totalPages}>下一页</Button>
                  <Button variant="outline" size="sm" onClick={() => setPage(totalPages)} disabled={safePage >= totalPages}>末页</Button>
                </div>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Protocol Bulk Bind Dialog */}
      <Dialog open={bulkBindOpen} onOpenChange={(open) => { if (!bulkLoading) setBulkBindOpen(open) }}>
        <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto border-cyan-400/20 bg-zinc-950 shadow-2xl shadow-cyan-950/30">
          <DialogHeader className="border-b border-white/5 pb-4">
            <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-cyan-400">
              <Link2 size={14} /> Project-local Mailat protocol runner
            </div>
            <DialogTitle>批量协议绑定</DialogTitle>
            <p className="text-sm leading-6 text-zinc-400">全程使用项目内置 Mailat 协议任务，不读取外部协议目录，也不打开或恢复浏览器。先保存并发与手机号配置，再逐个派发可绑定账号。</p>
          </DialogHeader>

          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-lg border border-white/10 bg-black/25 px-4 py-3">
                <div className="text-xs uppercase tracking-wider text-zinc-500">已选择</div>
                <div className="mt-1 text-2xl font-semibold text-zinc-100">{selectedAccounts.length}</div>
              </div>
              <div className="rounded-lg border border-cyan-400/20 bg-cyan-950/20 px-4 py-3">
                <div className="text-xs uppercase tracking-wider text-cyan-500">可协议绑定</div>
                <div className="mt-1 text-2xl font-semibold text-cyan-200">{protocolBindableSelectedAccounts.length}</div>
              </div>
            </div>

            {protocolBindableSelectedAccounts.length < selectedAccounts.length && (
              <div className="rounded-lg border border-amber-400/20 bg-amber-950/20 px-4 py-3 text-xs leading-5 text-amber-200/80">
                协议 add-phone 仅派发未完成绑定的邮箱注册账号；其他选中项会保留，不会启动任务。
              </div>
            )}

            <div className="space-y-2">
              <label className="text-sm font-medium text-zinc-300">授权结果去向</label>
              <div className="grid gap-3 md:grid-cols-2">
                <button
                  type="button"
                  aria-pressed={oauthMode === 'local'}
                  onClick={() => setOauthMode('local')}
                  className={`rounded-xl border p-4 text-left transition-colors ${oauthMode === 'local' ? 'border-cyan-400/50 bg-cyan-950/30 ring-1 ring-cyan-400/20' : 'border-white/10 bg-black/20 hover:border-white/20'}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-semibold text-zinc-100">本地换取 refresh_token</span>
                    {oauthMode === 'local' && <CheckCircle size={17} className="text-cyan-400" />}
                  </div>
                  <p className="mt-2 text-xs leading-5 text-zinc-400">协议完成 PKCE 授权码交换，将 access_token、id_token 与 refresh_token 保存到本地账号；不提交 CPA。</p>
                </button>
                <button
                  type="button"
                  aria-pressed={oauthMode === 'cpa'}
                  onClick={() => setOauthMode('cpa')}
                  className={`rounded-xl border p-4 text-left transition-colors ${oauthMode === 'cpa' ? 'border-amber-400/50 bg-amber-950/25 ring-1 ring-amber-400/20' : 'border-white/10 bg-black/20 hover:border-white/20'}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-semibold text-zinc-100">直接提交 CPA</span>
                    {oauthMode === 'cpa' && <CheckCircle size={17} className="text-amber-400" />}
                  </div>
                  <p className="mt-2 text-xs leading-5 text-zinc-400">协议授权完成后直接提交 CPA，本地不执行授权码交换，也不保存新的 refresh_token。</p>
                </button>
              </div>
            </div>

            <div className="grid gap-4 border-t border-white/5 pt-5 md:grid-cols-2">
              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">绑定手机号池</label>
                <Select value={bindSmsProvider} onValueChange={handleBindProviderChange}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="smsbower_api">SMSBower API</SelectItem>
                    <SelectItem value="bind_user_phone_url">绑定手机号 API</SelectItem>
                    <SelectItem value="user_phone_url">注册手机号池（兼容）</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {['bind_user_phone_url', 'user_phone_url'].includes(bindSmsProvider) ? (
                <div className="space-y-1.5">
                  <label className="text-sm text-zinc-400">临时绑定手机号 API</label>
                  <Input value={bindSmsPhoneUrl} onChange={(event) => setBindSmsPhoneUrl(event.target.value)} placeholder="15555550101|https://sms.example.invalid/messages/placeholder" />
                </div>
              ) : (
                <div className="rounded-lg border border-white/5 bg-black/20 px-4 py-3 text-xs leading-5 text-zinc-500">
                  SMSBower 凭据沿用“服务商”页配置；此处只选择国家与服务代码。
                </div>
              )}
              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">绑定国家</label>
                <Select value={bindSmsCountry} onValueChange={handleBindCountryChange}>
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
              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">绑定服务代码</label>
                <Input value={bindSmsService} onChange={(event) => setBindSmsService(event.target.value)} placeholder="dr" />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm text-zinc-400">绑定并发线程</label>
                <Input
                  type="number"
                  min={1}
                  max={100}
                  value={bindThreads}
                  onChange={(event) => {
                    const next = Math.max(1, Math.min(100, Number(event.target.value) || 1))
                    setBindThreads(next)
                    setMaxParallelTasks((current) => Math.max(current, next))
                  }}
                />
                <p className="text-xs text-zinc-500">1–100，保存为 max_oauth_tasks。</p>
              </div>
              <div className="space-y-1.5 md:col-span-2">
                <label className="text-sm text-zinc-400">全局最大并发任务</label>
                <Input
                  type="number"
                  min={bindThreads}
                  max={100}
                  value={maxParallelTasks}
                  onChange={(event) => setMaxParallelTasks(Math.max(bindThreads, Math.min(100, Number(event.target.value) || bindThreads)))}
                />
                <p className="text-xs text-zinc-500">保存为 max_parallel_tasks，提交时保证不小于绑定并发。Go 协议可到 100。</p>
              </div>
            </div>
          </div>

          <DialogFooter className="border-t border-white/5 pt-4">
            <Button variant="ghost" onClick={() => setBulkBindOpen(false)} disabled={bulkLoading}>取消</Button>
            <Button onClick={handleBulkBind} disabled={bulkLoading || protocolBindableSelectedAccounts.length === 0}>
              {bulkLoading ? '正在保存并启动…' : `启动 ${protocolBindableSelectedAccounts.length} 个协议任务`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Dedicated UPI Configuration Dialog */}
      <Dialog open={upiConfigOpen} onOpenChange={(open) => { if (!upiConfigSaving && !upiIssuingKey) setUpiConfigOpen(open) }}>
        <DialogContent className="max-h-[94vh] max-w-6xl overflow-y-auto border-emerald-400/20 bg-zinc-950 p-0 shadow-2xl shadow-emerald-950/30">
          <DialogHeader className="border-b border-white/10 bg-[linear-gradient(110deg,rgba(16,185,129,0.10),transparent_45%)] px-6 py-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-emerald-400">
                  <Settings2 size={14} /> UPI 专用配置
                </div>
                <DialogTitle className="text-xl">UPI 配置</DialogTitle>
                <p className="mt-1 max-w-3xl text-sm leading-6 text-zinc-400">连接、Client Key、执行策略与 CDK 签发集中在账号页。所有后端返回值按原值显示；保存仅提交已修改字段。</p>
              </div>
              <div className="flex items-center gap-2">
                <Badge variant={activationReady ? 'success' : activationStats ? 'danger' : 'secondary'}>
                  {activationReady ? '就绪' : activationStats?.config.enabled ? '缺少 Key' : '已停用'}
                </Badge>
                {upiDirtyKeys.size > 0 && <Badge variant="warning">{upiDirtyKeys.size} 项未保存</Badge>}
              </div>
            </div>
          </DialogHeader>

          {upiConfigLoading ? (
            <div className="flex min-h-80 items-center justify-center gap-2 text-sm text-zinc-500">
              <RefreshCw size={16} className="animate-spin" /> 正在读取真实配置与队列状态…
            </div>
          ) : (
            <div className="space-y-5 px-6 py-5">
              {upiConfigError && <div role="alert" className="rounded-md border border-red-400/25 bg-red-950/25 px-4 py-3 text-sm text-red-200">{upiConfigError}</div>}
              {upiConfigSuccess && <div role="status" className="rounded-md border border-emerald-400/25 bg-emerald-950/25 px-4 py-3 text-sm text-emerald-200">{upiConfigSuccess}</div>}
              <fieldset disabled={upiConfigSaving || upiIssuingKey} className="contents">

              <section className="overflow-hidden rounded-lg border border-white/10 bg-black/20">
                <div className="flex items-center gap-2 border-b border-white/10 bg-white/[0.025] px-4 py-3">
                  <Server size={15} className="text-cyan-400" />
                  <div>
                    <h3 className="text-sm font-semibold text-zinc-100">基础连接</h3>
                    <p className="text-xs text-zinc-500">开关、服务端点、设备标识与默认支付通道</p>
                  </div>
                </div>
                <div className="grid gap-4 p-4 lg:grid-cols-12">
                  <label className="flex items-start gap-3 rounded-md border border-white/10 bg-zinc-950/80 px-4 py-3 lg:col-span-3">
                    <input
                      type="checkbox"
                      checked={upiConfigDraft.enabled}
                      onChange={(event) => updateUpiConfig({ enabled: event.target.checked }, 'upi_activation_enabled')}
                      className="mt-0.5 h-4 w-4 rounded border-white/20 bg-zinc-900 text-emerald-500"
                    />
                    <span>
                      <span className="block text-sm font-medium text-zinc-200">启用 UPI 开通</span>
                      <span className="mt-1 block text-xs text-zinc-500">关闭时 worker 保留状态但不接受新提交。</span>
                    </span>
                  </label>
                  <div className="space-y-1.5 lg:col-span-5">
                    <label htmlFor="upi-base-url" className="text-xs font-medium tracking-wider text-zinc-500">服务地址</label>
                    <Input id="upi-base-url" value={upiConfigDraft.baseUrl} onChange={(event) => updateUpiConfig({ baseUrl: event.target.value }, 'upi_base_url')} placeholder="https://upi.example.com" />
                  </div>
                  <div className="space-y-1.5 lg:col-span-2">
                    <label htmlFor="upi-device-id" className="text-xs font-medium tracking-wider text-zinc-500">设备标识</label>
                    <Input id="upi-device-id" value={upiConfigDraft.deviceId} onChange={(event) => updateUpiConfig({ deviceId: event.target.value }, 'upi_device_id')} placeholder="gpt-register" />
                  </div>
                  <div className="space-y-1.5 lg:col-span-2">
                    <label className="text-xs font-medium tracking-wider text-zinc-500">默认通道</label>
                    <Select
                      value={upiConfigDraft.defaultChannel}
                      onValueChange={(value) => updateUpiConfig({
                        defaultChannel: (value === 'pix' || value === 'ideal' || value === 'upi') ? value : 'upi',
                      }, 'upi_default_channel')}
                    >
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="upi">UPI</SelectItem>
                        <SelectItem value="pix">PIX</SelectItem>
                        <SelectItem value="ideal">iDEAL</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              </section>

              <section className="overflow-hidden rounded-lg border border-white/10 bg-black/20">
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 bg-white/[0.025] px-4 py-3">
                  <div className="flex items-center gap-2">
                    <KeyRound size={15} className="text-emerald-400" />
                    <div>
                      <h3 className="text-sm font-semibold text-zinc-100">Key 管理</h3>
                      <p className="text-xs text-zinc-500">主 Key 与附加 Key 均明文显示；当前有效队列 Key {activationStats?.config.key_count ?? '—'} 个</p>
                    </div>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => updateUpiConfig({ additionalKeys: [...upiConfigDraft.additionalKeys, ''] }, 'upi_client_keys')}>
                    <Plus size={14} /> 添加附加 Key
                  </Button>
                </div>
                <div className="space-y-4 p-4">
                  <div className="space-y-1.5">
                    <label htmlFor="upi-primary-key" className="text-xs font-medium tracking-wider text-zinc-500">主 Client Key</label>
                    <div className="flex gap-2">
                      <Input id="upi-primary-key" className="font-mono text-xs" autoComplete="off" value={upiConfigDraft.primaryKey} onChange={(event) => updateUpiConfig({ primaryKey: event.target.value }, 'upi_client_key')} placeholder="actk_…" />
                      <Button variant="outline" size="icon" disabled={!upiConfigDraft.primaryKey} aria-label="复制主 Client Key" onClick={() => void copyUpiValue('primary-key', upiConfigDraft.primaryKey)}>
                        {copiedUpiValue === 'primary-key' ? <CheckCircle size={15} /> : <Copy size={15} />}
                      </Button>
                    </div>
                  </div>

                  <div className="space-y-2">
                    <div className="text-xs font-medium tracking-wider text-zinc-500">附加 Client Keys</div>
                    {upiConfigDraft.additionalKeys.length === 0 ? (
                      <div className="rounded-md border border-dashed border-white/10 px-4 py-5 text-center text-xs text-zinc-600">暂无附加 Key。使用右上角按钮逐行添加。</div>
                    ) : upiConfigDraft.additionalKeys.map((keyValue, index) => (
                      <div key={`additional-key-${index}`} className="grid grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-2">
                        <span className="w-7 text-right font-mono text-xs text-zinc-600">{String(index + 1).padStart(2, '0')}</span>
                        <Input className="font-mono text-xs" autoComplete="off" value={keyValue} onChange={(event) => updateAdditionalUpiKey(index, event.target.value)} placeholder="actk_…" aria-label={`附加 Client Key ${index + 1}`} />
                        <Button variant="outline" size="icon" disabled={!keyValue} aria-label={`复制附加 Client Key ${index + 1}`} onClick={() => void copyUpiValue(`additional-key-${index}`, keyValue)}>
                          {copiedUpiValue === `additional-key-${index}` ? <CheckCircle size={15} /> : <Copy size={15} />}
                        </Button>
                        <Button variant="ghost" size="icon" aria-label={`删除附加 Client Key ${index + 1}`} onClick={() => updateUpiConfig({ additionalKeys: upiConfigDraft.additionalKeys.filter((_, keyIndex) => keyIndex !== index) }, 'upi_client_keys')}>
                          <Trash2 size={15} className="text-red-300" />
                        </Button>
                      </div>
                    ))}
                  </div>
                </div>
              </section>

              <section className="overflow-hidden rounded-lg border border-white/10 bg-black/20">
                <div className="flex items-center gap-2 border-b border-white/10 bg-white/[0.025] px-4 py-3">
                  <Gauge size={15} className="text-amber-400" />
                  <div>
                    <h3 className="text-sm font-semibold text-zinc-100">执行策略</h3>
                    <p className="text-xs text-zinc-500">限速、轮询、任务超时与成功后的自动验收</p>
                  </div>
                </div>
                <div className="grid gap-4 p-4 md:grid-cols-2 xl:grid-cols-4">
                  <div className="space-y-1.5">
                    <label htmlFor="upi-submit-rate" className="text-xs font-medium tracking-wider text-zinc-500">每 Key 提交 / 分钟</label>
                    <Input id="upi-submit-rate" type="number" min={1} value={upiConfigDraft.submitPerKeyPerMin} onChange={(event) => updateUpiConfig({ submitPerKeyPerMin: event.target.value }, 'upi_submit_per_key_per_min')} />
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor="upi-poll-interval" className="text-xs font-medium tracking-wider text-zinc-500">轮询间隔 / 秒</label>
                    <Input id="upi-poll-interval" type="number" min={1} max={300} value={upiConfigDraft.pollIntervalSec} onChange={(event) => updateUpiConfig({ pollIntervalSec: event.target.value }, 'upi_poll_interval_sec')} />
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor="upi-poll-timeout" className="text-xs font-medium tracking-wider text-zinc-500">任务超时 / 秒</label>
                    <Input id="upi-poll-timeout" type="number" min={1} value={upiConfigDraft.pollTimeoutSec} onChange={(event) => updateUpiConfig({ pollTimeoutSec: event.target.value }, 'upi_poll_timeout_sec')} />
                  </div>
                  <label className="flex items-start gap-3 rounded-md border border-white/10 bg-zinc-950/80 px-4 py-3">
                    <input type="checkbox" checked={upiConfigDraft.autoVerifyPlus} onChange={(event) => updateUpiConfig({ autoVerifyPlus: event.target.checked }, 'upi_auto_verify_plus')} className="mt-0.5 h-4 w-4 rounded border-white/20 bg-zinc-900 text-emerald-500" />
                    <span>
                      <span className="block text-sm font-medium text-zinc-200">自动校验 Plus</span>
                      <span className="mt-1 block text-xs text-zinc-500">远端开通成功后进入校验中 / 已验收。</span>
                    </span>
                  </label>
                </div>
              </section>

              <section className="overflow-hidden rounded-lg border border-emerald-400/15 bg-[linear-gradient(120deg,rgba(6,78,59,0.22),rgba(0,0,0,0.15)_48%)]">
                <div className="flex items-center gap-2 border-b border-emerald-400/10 px-4 py-3">
                  <Power size={15} className="text-emerald-400" />
                  <div>
                    <h3 className="text-sm font-semibold text-emerald-100">CDK 签发 / 轮换</h3>
                    <p className="text-xs text-zinc-500">CDK 仅随本次请求发送；返回的完整 Client Key 与元数据会在下方原样显示。</p>
                  </div>
                </div>
                <div className="grid gap-4 p-4 lg:grid-cols-12">
                  <div className="space-y-1.5 lg:col-span-5">
                    <label htmlFor="upi-cdk" className="text-xs font-medium uppercase tracking-wider text-zinc-500">CDK</label>
                    <Input id="upi-cdk" type="text" autoComplete="off" value={upiCdk} onChange={(event) => setUpiCdk(event.target.value)} placeholder="输入 CDK 原值" />
                  </div>
                  <div className="space-y-1.5 lg:col-span-4">
                    <label htmlFor="upi-key-note" className="text-xs font-medium tracking-wider text-zinc-500">备注</label>
                    <Input id="upi-key-note" value={upiKeyNote} onChange={(event) => setUpiKeyNote(event.target.value)} placeholder="gpt-register" />
                  </div>
                  <label className="flex items-center gap-3 rounded-md border border-amber-400/15 bg-amber-950/10 px-4 py-2 lg:col-span-3">
                    <input type="checkbox" checked={upiRotateKey} onChange={(event) => setUpiRotateKey(event.target.checked)} className="h-4 w-4 rounded border-white/20 bg-zinc-900 text-amber-500" />
                    <span className="text-sm text-amber-100">轮换当前主 Key</span>
                  </label>
                  <div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/10 pt-4 lg:col-span-12">
                    <p className="text-xs text-zinc-500">{upiDirtyKeys.size > 0 ? '当前有未保存字段；先保存后才能签发。' : '签发成功后会立即回读真实配置与队列状态。'}</p>
                    <Button onClick={() => void handleIssueUpiKey()} disabled={upiIssuingKey || !upiCdk.trim() || upiDirtyKeys.size > 0}>
                      {upiIssuingKey ? <RefreshCw size={14} className="animate-spin" /> : <KeyRound size={14} />}
                      {upiIssuingKey ? '正在签发…' : upiRotateKey ? '轮换并保存 Key' : '签发并保存 Key'}
                    </Button>
                  </div>

                  {upiIssuedKey && (
                    <div role="status" className="space-y-3 rounded-md border border-emerald-400/25 bg-black/30 p-4 lg:col-span-12">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div>
                          <div className="text-xs font-semibold tracking-[0.16em] text-emerald-400">已签发 Client Key · 完整原值</div>
                          <div className="mt-2 break-all font-mono text-sm text-emerald-100">{upiIssuedKey.client_key}</div>
                        </div>
                        <Button variant="outline" size="sm" onClick={() => void copyUpiValue('issued-key', upiIssuedKey.client_key)}>
                          {copiedUpiValue === 'issued-key' ? <CheckCircle size={14} /> : <Copy size={14} />} {copiedUpiValue === 'issued-key' ? '已复制' : '复制完整 Key'}
                        </Button>
                      </div>
                      <div className="grid gap-px overflow-hidden rounded border border-white/10 bg-white/10 sm:grid-cols-2 xl:grid-cols-3">
                        {Object.entries(upiIssuedKey)
                          .filter(([field, value]) => !['ok', 'client_key', 'config'].includes(field) && value !== null && ['string', 'number', 'boolean'].includes(typeof value))
                          .map(([field, value]) => (
                            <div key={field} className="bg-zinc-950 px-3 py-2">
                              <div className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">{field}</div>
                              <div className="mt-1 break-all font-mono text-xs text-zinc-300">{String(value)}</div>
                            </div>
                          ))}
                      </div>
                    </div>
                  )}
                </div>
              </section>
              </fieldset>
            </div>
          )}

          <DialogFooter className="border-t border-white/10 bg-black/20 px-6 py-4">
            <Button variant="ghost" onClick={() => setUpiConfigOpen(false)} disabled={upiConfigSaving || upiIssuingKey}>关闭</Button>
            <Button onClick={() => void handleSaveUpiConfig()} disabled={upiConfigLoading || upiConfigSaving || upiIssuingKey || upiDirtyKeys.size === 0}>
              {upiConfigSaving ? <RefreshCw size={14} className="animate-spin" /> : <Save size={14} />}
              {upiConfigSaving ? '保存并回读…' : upiDirtyKeys.size > 0 ? `保存 ${upiDirtyKeys.size} 项` : '已保存'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* UPI Bulk Activate Dialog */}
      <Dialog open={bulkActivateOpen} onOpenChange={(open) => { setBulkActivateOpen(open) }}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto border-emerald-400/20 bg-zinc-950 shadow-2xl shadow-emerald-950/30">
          <DialogHeader className="border-b border-white/5 pb-4">
            <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-400">
                  <Star size={14} /> UPI 开通 · 本系统内开通
            </div>
            <DialogTitle>批量开通 Plus</DialogTitle>
            <p className="text-sm leading-6 text-zinc-400">
              只提交具备 access token 且未处于终态/活动态的账号。请求固定 force=false，避免覆盖或孤儿化已有远端任务。
            </p>
          </DialogHeader>

          <div className="space-y-5">
            {!activationReady && (
              <div role="alert" className="rounded-lg border border-amber-400/25 bg-amber-950/25 px-4 py-3 text-sm text-amber-200">
                <div className="font-medium">当前不可提交</div>
                <p className="mt-1 text-xs leading-5 text-amber-100/70">{activationBlockedMessage}</p>
                <Button className="mt-3" variant="outline" size="sm" onClick={() => { setBulkActivateOpen(false); void openUpiConfigDialog() }}>打开 UPI 配置</Button>
              </div>
            )}

            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-lg border border-white/10 bg-black/25 px-4 py-3">
                <div className="text-xs uppercase tracking-wider text-zinc-500">已选择</div>
                <div className="mt-1 text-2xl font-semibold text-zinc-100">{selectedAccounts.length}</div>
              </div>
              <div className="rounded-lg border border-emerald-400/20 bg-emerald-950/20 px-4 py-3">
                <div className="text-xs uppercase tracking-wider text-emerald-500">可开通</div>
                <div className="mt-1 text-2xl font-semibold text-emerald-200">{activatableSelectedAccounts.length}</div>
              </div>
              <div className="rounded-lg border border-amber-400/20 bg-amber-950/20 px-4 py-3">
                <div className="text-xs uppercase tracking-wider text-amber-500">已在跑/不可新开</div>
                <div className="mt-1 text-2xl font-semibold text-amber-200">{selectedAccounts.length - activatableSelectedAccounts.length}</div>
              </div>
            </div>

            {selectedAccounts.length > activatableSelectedAccounts.length && (
              <div className="rounded-lg border border-white/10 bg-black/20 px-4 py-3">
                <div className="text-xs font-medium text-zinc-300">说明（不阻塞提交）</div>
                <p className="mt-1 text-[11px] leading-5 text-zinc-500">
                  “已在跑/不可新开”只是提示：进行中的任务后台会跳过，不会重复提交；缺 token / 已是 Plus / 终态不会入队。点排队后立即关闭，提交走异步，限速由服务端按每 Key 每分钟上限消化。
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {(Object.entries(activationSelection.excluded) as Array<[ActivationEligibilityReason, number]>)
                    .filter(([, count]) => count > 0)
                    .map(([reason, count]) => (
                      <span key={reason} className="rounded-full border border-amber-400/15 bg-amber-950/20 px-2.5 py-1 text-[11px] text-amber-200/80">
                        {ACTIVATION_ELIGIBILITY_META[reason]} {count}
                      </span>
                    ))}
                </div>
              </div>
            )}

            {activationDialogError && (
              <div role="alert" className="rounded-lg border border-red-400/25 bg-red-950/25 px-4 py-3 text-sm text-red-200">
                {activationDialogError}
              </div>
            )}

            {activationFailures.length > 0 && (
              <div className="rounded-lg border border-red-400/20 bg-black/20 px-4 py-3">
                <div className="text-xs font-medium text-red-300">未成功排队（已保留选择）</div>
                <div className="mt-2 max-h-36 space-y-1 overflow-y-auto">
                  {activationFailures.map((failure) => (
                    <div key={failure.key} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)] gap-3 border-t border-white/5 py-1.5 text-xs first:border-t-0">
                      <span className="truncate font-mono text-zinc-400" title={failure.key}>{failure.key}</span>
                      <span className="break-words text-red-300/80">{failure.message}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="space-y-2">
              <label className="text-sm font-medium text-zinc-300">支付通道</label>
              <div className="grid gap-3 md:grid-cols-3">
                <button
                  type="button"
                  aria-pressed={activateChannel === 'upi'}
                  onClick={() => setActivateChannel('upi')}
                  className={`rounded-xl border p-4 text-left transition-colors ${activateChannel === 'upi' ? 'border-emerald-400/50 bg-emerald-950/30 ring-1 ring-emerald-400/20' : 'border-white/10 bg-black/20 hover:border-white/20'}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-semibold text-zinc-100">UPI</span>
                    {activateChannel === 'upi' && <CheckCircle size={17} className="text-emerald-400" />}
                  </div>
                  <p className="mt-2 text-xs leading-5 text-zinc-400">最新官方自助提交通道（channel=upi）。</p>
                </button>
                <button
                  type="button"
                  aria-pressed={activateChannel === 'pix'}
                  onClick={() => setActivateChannel('pix')}
                  className={`rounded-xl border p-4 text-left transition-colors ${activateChannel === 'pix' ? 'border-emerald-400/50 bg-emerald-950/30 ring-1 ring-emerald-400/20' : 'border-white/10 bg-black/20 hover:border-white/20'}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-semibold text-zinc-100">PIX</span>
                    {activateChannel === 'pix' && <CheckCircle size={17} className="text-emerald-400" />}
                  </div>
                  <p className="mt-2 text-xs leading-5 text-zinc-400">巴西 PIX 值守付款流程。</p>
                </button>
                <button
                  type="button"
                  aria-pressed={activateChannel === 'ideal'}
                  onClick={() => setActivateChannel('ideal')}
                  className={`rounded-xl border p-4 text-left transition-colors ${activateChannel === 'ideal' ? 'border-cyan-400/50 bg-cyan-950/30 ring-1 ring-cyan-400/20' : 'border-white/10 bg-black/20 hover:border-white/20'}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-semibold text-zinc-100">iDEAL</span>
                    {activateChannel === 'ideal' && <CheckCircle size={17} className="text-cyan-400" />}
                  </div>
                  <p className="mt-2 text-xs leading-5 text-zinc-400">荷兰 iDEAL 值守付款流程。</p>
                </button>
              </div>
            </div>

            <div className="rounded-lg border border-white/10 bg-black/20 px-4 py-3 text-xs leading-5 text-zinc-500">
              {activationStats ? (
                <>
                  实际配置：{activationStats.config.key_count} 个 Key，单 Key {activationStats.config.submit_per_key_per_min}/分钟，
                  总提交能力 {activationStats.config.key_count * activationStats.config.submit_per_key_per_min}/分钟；轮询 {activationStats.config.poll_interval_sec} 秒，超时 {activationStats.config.poll_timeout_sec} 秒；
                  自动验收{activationStats.config.auto_verify_plus ? '开启，远端成功后进入校验中' : '关闭，远端成功即终止'}。
                  {activationStats.config.key_count > 0 && activationStats.config.submit_per_key_per_min > 0
                    ? ` 当前 ${activatableSelectedAccounts.length} 个账号估算约 ${Math.max(1, Math.ceil(activatableSelectedAccounts.length / (activationStats.config.key_count * activationStats.config.submit_per_key_per_min)))} 分钟完成提交（不含值守付款）。`
                    : ' 当前没有可用提交容量。'}
                </>
              ) : '正在读取实际 Key 数量、限速与验收配置。'}
            </div>
          </div>

          <DialogFooter className="border-t border-white/5 pt-4">
            <Button variant="ghost" onClick={() => setBulkActivateOpen(false)}>关闭</Button>
            {!activationReady && <Button variant="outline" onClick={() => { setBulkActivateOpen(false); void openUpiConfigDialog() }}>打开 UPI 配置</Button>}
            <Button onClick={handleBulkActivate} disabled={activateLoading || !activationReady || selectedAccounts.length === 0}>
              {activateLoading ? '后台排队中…' : `异步排队 ${selectedAccounts.length} 个（${activateChannel.toUpperCase()}）`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Detail Dialog */}
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>账号详情</DialogTitle>
          </DialogHeader>
          {selected && (
            <div className="space-y-3 mt-2">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <p className="text-xs text-zinc-500">账号键</p>
                  <p className="text-sm text-zinc-200 font-mono break-all">{selected.key}</p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">邮箱</p>
                  <p className="text-sm text-zinc-200">{selected.email || '—'}</p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">手机号</p>
                  <p className="text-sm text-zinc-200">{selected.phone_number || selected.binding_phone_number || selected.sms_phone || '—'}</p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">阶段</p>
                  <Badge variant={stageColor(selected.stage)}>
                    {STATUS_LABELS[selected.stage ?? ''] ?? STAGE_LABELS[selected.stage ?? ''] ?? selected.stage ?? '—'}
                  </Badge>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">账号健康</p>
                  <Badge variant={healthBadgeVariant(selected.health_status)}>
                    {healthStatusLabel(selected.health_status)}
                  </Badge>
                  {selected.health_checked_at && <p className="mt-1 text-xs text-zinc-500">{formatDate(selected.health_checked_at)}</p>}
                  {(selected.health_check_error || selected.health_message) && (
                    <p className="mt-1 text-xs text-zinc-500">{selected.health_check_error || selected.health_message}</p>
                  )}
                </div>
                <div>
                  <p className="text-xs text-zinc-500">服务商</p>
                  <p className="text-sm text-zinc-200">{selected.provider || '—'}</p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">代理地区</p>
                  <p className="text-sm text-zinc-200">{selected.proxy_region || '—'}</p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">显示浏览器</p>
                  <p className="text-sm text-zinc-200">{selected.headed ? '是' : '否'}</p>
                </div>
                <div>
                  <p className="text-xs text-zinc-500">创建时间</p>
                  <p className="text-sm text-zinc-200">{formatDate(selected.created_at)}</p>
                </div>
              </div>
              <div className="rounded-xl border border-emerald-400/15 bg-emerald-950/10 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-xs text-zinc-500">UPI 开通诊断</p>
                    <Badge className="mt-1" variant={activationMeta(selected.activation_status).variant}>
                      {activationMeta(selected.activation_status).label}
                    </Badge>
                  </div>
                  {((!selected.activation_task_id && ['queued', 'submit_unknown'].includes(String(selected.activation_status || ''))) || Boolean(selected.activation_can_release)) && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => handleReleaseActivation(selected)}
                      disabled={releaseLoading === selected.key}
                    >
                      {releaseLoading === selected.key
                        ? '处理中…'
                        : !selected.activation_task_id && selected.activation_status === 'submit_unknown'
                          ? '取消待确认'
                          : !selected.activation_task_id && selected.activation_status === 'queued'
                            ? '取消排队'
                            : '释放任务'}
                    </Button>
                  )}
                </div>
                <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                  <p className="text-zinc-500">服务商：<span className="text-zinc-300">{selected.activation_provider || '—'}</span></p>
                  <p className="text-zinc-500">通道：<span className="text-zinc-300">{selected.activation_channel || '—'}</span></p>
                  <p className="break-all text-zinc-500">任务：<span className="font-mono text-zinc-300">{selected.activation_task_id || '—'}</span></p>
                  <p className="text-zinc-500">CDK：<span className="text-zinc-300">{selected.activation_cdk_consumed ? '已核销' : '未核销/未知'}</span></p>
                  <p className="text-zinc-500">可释放：<span className="text-zinc-300">{selected.activation_can_release ? '是' : '否'}</span></p>
                  <p className="text-zinc-500">更新时间：<span className="text-zinc-300">{formatDate(selected.activation_updated_at)}</span></p>
                </div>
                {(selected.activation_display || selected.activation_error) && (
                  <div className={`mt-3 rounded-lg border px-3 py-2 text-xs leading-5 ${selected.activation_error ? 'border-red-400/20 bg-red-950/20 text-red-300' : 'border-white/10 bg-black/20 text-zinc-400'}`}>
                    {selected.activation_error || selected.activation_display}
                  </div>
                )}
              </div>
              {selected.tokens && Object.keys(selected.tokens).length > 0 && (
                <div>
                  <p className="text-xs text-zinc-500 mb-1">Token 摘要（不含明文）</p>
                  <div className="rounded-lg bg-black/40 p-3 font-mono text-xs text-zinc-300 max-h-32 overflow-auto">
                    {Object.entries(selected.tokens).map(([k, v]) => (
                      <div key={k} className="truncate">
                        <span className="text-zinc-500">{k}:</span> {v ? '已保存' : '无'}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Export Dialog */}
      <Dialog open={exportOpen} onOpenChange={setExportOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{exportKey ? '选择导出字段' : `批量导出 ${selectedAccounts.length} 个账号`}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-zinc-400">选择要写入 JSON 的账号字段，导出内容沿用单账号导出结构；下载文件名前缀固定为 account。</p>
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
            <Button variant="ghost" onClick={() => setExportOpen(false)}>取消</Button>
            <Button onClick={handleExport} disabled={exportLoading || selectedExportFields.length === 0}>
              {exportLoading ? '正在导出…' : '下载 JSON'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* AT Import Dialog */}
      <Dialog open={atImportOpen} onOpenChange={(open) => { if (!atImportLoading) setAtImportOpen(open) }}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>导入 AT 账号</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm leading-6 text-zinc-400">
              支持每行一个 access token、email----password----access_token，或包含 accessToken / access_token 的 JSON。
            </p>
            <textarea
              value={atImportText}
              onChange={(event) => setAtImportText(event.target.value)}
              className="min-h-72 w-full resize-y rounded-md border border-white/10 bg-zinc-950 px-3 py-2 font-mono text-xs leading-5 text-zinc-100 outline-none focus:border-blue-500"
              placeholder="eyJhbGciOiJSUzI1NiIs..."
              spellCheck={false}
            />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setAtImportOpen(false)} disabled={atImportLoading}>取消</Button>
            <Button onClick={() => void handleImportAtAccounts()} disabled={atImportLoading || !atImportText.trim()}>
              {atImportLoading ? '导入中…' : '导入账号'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Plus Product Export Dialog */}
      <Dialog open={plusExportOpen} onOpenChange={(open) => { if (!plusExportLoading) setPlusExportOpen(open) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {selectedAccounts.length > 0
                ? `导出 Plus 成品号 ${selectedAccounts.length}`
                : '导出全部 Plus 成品号'}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm leading-6 text-zinc-400">
              将导出校验成功的 Plus 成品号为 TXT。
              {selectedAccounts.length > 0 ? ' 当前范围：已选中账号。' : ' 当前范围：全部账号。'}
            </p>
            <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-white/10 bg-black/20 p-4 hover:border-white/20">
              <input
                type="checkbox"
                checked={plusExportArchive}
                onChange={(event) => setPlusExportArchive(event.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
              />
              <span>
                <span className="block text-sm font-medium text-zinc-200">导出后自动归档</span>
                <span className="mt-1 block text-xs leading-5 text-zinc-500">
                  仅归档本次成功导出的账号；失败或跳过的账号不会归档。
                </span>
              </span>
            </label>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPlusExportOpen(false)} disabled={plusExportLoading}>取消</Button>
            <Button onClick={() => void handleExportPlusProductsTxt()} disabled={plusExportLoading}>
              {plusExportLoading
                ? '正在导出…'
                : plusExportArchive
                  ? '导出并归档'
                  : '导出 TXT'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* AT Product Export Dialog */}
      <Dialog open={atExportOpen} onOpenChange={(open) => { if (!atExportLoading) setAtExportOpen(open) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {selectedAccounts.length > 0
                ? `导出 AT 成品号 ${selectedAccounts.length}`
                : '导出全部 AT 成品号'}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm leading-6 text-zinc-400">
              导出格式：邮箱四段----access_token（与 Plus 四段相同，末尾再追加 ChatGPT access_token）。
              不校验是否 Plus；只要有 access_token 和邮箱令牌即可。
              {selectedAccounts.length > 0 ? ' 当前范围：已选中账号。' : ' 当前范围：全部账号。'}
            </p>
            <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-white/10 bg-black/20 p-4 hover:border-white/20">
              <input
                type="checkbox"
                checked={atExportArchive}
                onChange={(event) => setAtExportArchive(event.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-white/20 bg-zinc-900 text-blue-600"
              />
              <span>
                <span className="block text-sm font-medium text-zinc-200">导出后自动归档</span>
                <span className="mt-1 block text-xs leading-5 text-zinc-500">
                  仅归档本次成功导出的账号；失败或跳过的账号不会归档。
                </span>
              </span>
            </label>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setAtExportOpen(false)} disabled={atExportLoading}>取消</Button>
            <Button onClick={() => void handleExportAtProductsTxt()} disabled={atExportLoading}>
              {atExportLoading
                ? '正在导出…'
                : atExportArchive
                  ? '导出并归档'
                  : '导出 TXT'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Tokens Dialog */}
      <Dialog open={tokensOpen} onOpenChange={setTokensOpen}>
        <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>账号 Token</DialogTitle>
            <p className="text-xs leading-5 text-zinc-500">仅按需从后端加载指定账号的实际值；账号列表仍只返回 boolean 摘要。复制后请注意剪贴板安全。</p>
          </DialogHeader>

          {tokensLoading ? (
            <div className="flex items-center justify-center gap-2 py-12 text-sm text-zinc-500" role="status">
              <RefreshCw size={16} className="animate-spin" /> 正在加载 Token…
            </div>
          ) : tokensError ? (
            <div role="alert" className="rounded-lg border border-red-400/20 bg-red-950/20 px-4 py-3 text-sm text-red-300">{tokensError}</div>
          ) : visibleAccountTokens.length > 0 ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-3">
                <span className="text-xs text-zinc-500">固定显示 5 个后端原始字段</span>
                <Button variant="outline" size="sm" onClick={copyAllTokens}>
                  <Copy size={14} /> {copiedToken === 'all' ? '已复制全部' : '复制全部'}
                </Button>
              </div>
              <div className="space-y-3">
                {visibleAccountTokens.map(({ key, value }) => (
                  <div key={key} className="rounded-lg border border-white/10 bg-black/30 p-3">
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <span className="font-mono text-xs text-zinc-400">{key}</span>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`复制 ${key}`}
                        disabled={!value}
                        onClick={() => copyTokenValue(key, value)}
                      >
                        <Copy size={14} /> {copiedToken === key ? '已复制' : '复制'}
                      </Button>
                    </div>
                    <div className={`max-h-28 overflow-auto break-all rounded-md bg-black/40 px-3 py-2 font-mono text-xs leading-5 ${value ? 'text-zinc-200' : 'text-zinc-600'}`}>{value || '（空值）'}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="py-8 text-center text-sm text-zinc-500">未找到可显示的 Token。</p>
          )}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setTokensOpen(false)}>关闭</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
