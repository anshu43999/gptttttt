import { apiFetch } from './utils'
import type {
  Account,
  AccountHealthCheckResponse,
  AccountExport,
  BrowserSessionItem,
  AccountExportField,
  AccountTokens,
  Task,
  TaskEvent,
  ProviderInfo,
  ProviderTestRequest,
  ProviderSaveRequest,
  ProviderTestResult,
  RegisterRequest,
  RunStatus,
  ConfigPayload,
  StatsOverview,
  DailyStats,
  ProxyStats,
  ErrorStat,
  HealthStatus,
  EmailOtpResponse,
  ResourceItem,
  ResourceBulkStatusRequest,
  ResourceImportRequest,
  ProxyHealthCheckResult,
  ResourceCapacityResult,
  ResourceCategoryOption,
  ActivationStatus,
  ActivationQueueStats,
  ActivationReleaseResponse,
  PlusActivationBatch,
  PlusActivationBatchItem,
  PlusActivationExport,
  ArchiveBatch,
  ActivationClientKeyIssueRequest,
  ActivationClientKeyIssueResponse,
  PlusVerificationProgress,
  PlusVerificationResultItem,
} from './types'


export interface AccountsListResult {
  items: Account[]
  total: number
  truncated: boolean
}

/* ── Health ── */

export function getHealth() {
  return apiFetch<HealthStatus>('/health')
}

/* ── Accounts ── */
function normalizeAccount(account: Account): Account {
  return {
    ...account,
    key: account.key ?? account.account_key ?? account.account_id ?? '',
    sms_phone: account.sms_phone ?? account.phone_number ?? '',
    proxy_region: account.proxy_region ?? account.proxy?.registration_exit_ip ?? '',
    health_status: account.health_status ?? account.account_health_status,
    health_checked_at: account.health_checked_at ?? account.account_health_checked_at,
    health_check_source: account.health_check_source ?? account.account_health_source,
    health_check_error: account.health_check_error ?? account.account_health_error,
  }
}


export function getAccounts(options: { signal?: AbortSignal; withMeta: true; limit?: number }): Promise<AccountsListResult>
export function getAccounts(options?: { signal?: AbortSignal; withMeta?: false; limit?: number }): Promise<Account[]>
export async function getAccounts(options: { signal?: AbortSignal; withMeta?: boolean; limit?: number } = {}): Promise<Account[] | AccountsListResult> {
  const limit = Math.max(1, options.limit ?? 100000)
  const res = await apiFetch<{ok: boolean; items: Account[]; total?: number; truncated?: boolean}>(`/accounts?limit=${limit}`, { signal: options.signal })
  const items = res.items.map(normalizeAccount)
  if (!options.withMeta) return items
  const total = Number.isFinite(Number(res.total)) ? Number(res.total) : items.length
  return { items, total, truncated: Boolean(res.truncated ?? total > items.length) }
}

export async function getAccount(key: string) {
  const res = await apiFetch<{ok: boolean; account: Account}>(`/accounts/${encodeURIComponent(key)}`)
  return normalizeAccount(res.account)
}

export async function importAtAccounts(text: string) {
  const res = await apiFetch<{ok: boolean; imported: number; items: Account[]; message?: string}>('/accounts/import-at', {
    method: 'POST',
    body: JSON.stringify({ text }),
  })
  return {
    ...res,
    items: (res.items ?? []).map(normalizeAccount),
  }
}

export function archiveAccount(key: string) {
  return archiveAccounts([key])
}

export function archiveAccounts(keys: string[]) {
  return apiFetch<{ok: boolean; archived: number; keys: string[]; missing: string[]}>('/accounts/archive', {
    method: 'POST',
    body: JSON.stringify({ keys }),
  })
}


export async function markPlusAccount(key: string) {
  const res = await apiFetch<{ok: boolean; account: Account}>(`/accounts/${encodeURIComponent(key)}/mark-plus`, { method: 'POST' })
  return normalizeAccount(res.account)
}

export async function verifyPlusAccount(key: string, proxyRegion = 'JP') {
  const res = await apiFetch<{ok: boolean; account: Account; plan_type: string; source: string; paid: boolean}>(`/accounts/${encodeURIComponent(key)}/verify-plus`, {
    method: 'POST',
    body: JSON.stringify({ proxy_region: proxyRegion }),
  })
  return { ...res, account: normalizeAccount(res.account) }
}

export async function verifyPlusAccounts(keys: string[], proxyRegion = 'JP') {
  const res = await apiFetch<{ok: boolean; checked: number; paid: number; results: PlusVerificationResultItem[]}>('/accounts-bulk/verify-plus', {
    method: 'POST',
    body: JSON.stringify({ keys, proxy_region: proxyRegion }),
  })
  return {
    ...res,
    results: res.results.map((item) => ({ ...item, account: item.account ? normalizeAccount(item.account) : undefined })),
  }
}

export async function startPlusVerification(keys: string[], proxyRegion = 'JP') {
  const res = await apiFetch<PlusVerificationProgress>('/accounts-bulk/verify-plus', {
    method: 'POST',
    body: JSON.stringify({ keys, proxy_region: proxyRegion, async_mode: true }),
  })
  return normalizePlusVerificationProgress(res)
}

export async function getPlusVerification(taskId: string, options?: { signal?: AbortSignal }) {
  const res = await apiFetch<PlusVerificationProgress>(`/accounts-bulk/verify-plus/${encodeURIComponent(taskId)}`, { signal: options?.signal })
  return normalizePlusVerificationProgress(res)
}

export async function cancelPlusVerification(taskId: string) {
  const res = await apiFetch<PlusVerificationProgress>(`/accounts-bulk/verify-plus/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' })
  return normalizePlusVerificationProgress(res)
}

function normalizePlusVerificationProgress(progress: PlusVerificationProgress): PlusVerificationProgress {
  return {
    ...progress,
    results: (progress.results ?? []).map((item) => ({ ...item, account: item.account ? normalizeAccount(item.account) : undefined })),
  }
}

export async function checkAccountHealth(key: string) {
  const res = await apiFetch<{ok: boolean; account?: Account; status?: string; health_status?: string; message?: string; error?: string}>(`/accounts/${encodeURIComponent(key)}/check-health`, { method: 'POST' })
  return { ...res, account: res.account ? normalizeAccount(res.account) : undefined }
}

export async function checkAccountsHealth(keys: string[]) {
  const res = await apiFetch<AccountHealthCheckResponse>('/accounts-bulk/check-health', {
    method: 'POST',
    body: JSON.stringify({ keys }),
  })
  const rawResults = res.results ?? res.accounts?.map((account) => ({
    key: account.key ?? account.account_key ?? account.account_id ?? '',
    account,
  })) ?? []
  return {
    ...res,
    results: rawResults.map((item) => ({ ...item, account: item.account ? normalizeAccount(item.account) : undefined })),
  }
}

export function cleanupInvalidAccounts() {
  return apiFetch<{ok: boolean; archived: number; keys: string[]}>('/accounts/cleanup-invalid', { method: 'POST' })
}

export async function activatePlusAccounts(keys: string[], options: { channel?: string; provider?: string } = {}) {
  return apiFetch<{
    ok: boolean
    dry_run?: boolean
    batch?: PlusActivationBatch
    batch_key?: string
    requested?: number
    accepted: number
    queued: number
    skipped: number
    failed?: number
    async?: boolean
    skip_counts?: Record<string, number>
    message?: string
    results: Array<{
      key: string
      reason?: string
      ok?: boolean
      skipped?: boolean
      message?: string
      activation_status?: ActivationStatus
      activation_channel?: string
      account?: Partial<Account>
    }>
  }>('/accounts-bulk/activate-plus', {
    method: 'POST',
    body: JSON.stringify({
      keys,
      channel: options.channel || 'upi',
      force: false,
      provider: options.provider || 'upi',
    }),
  })
}

export function createPlusActivationBatch(keys: string[], options: { name?: string; channel?: string; dry_run?: boolean; submit_rate_per_min?: number; max_in_flight?: number } = {}) {
  return apiFetch<{
    ok: boolean
    dry_run?: boolean
    batch?: PlusActivationBatch
    batch_key?: string
    requested: number
    accepted: number
    queued: number
    skipped: number
    skip_counts?: Record<string, number>
    message?: string
    results: Array<{ key: string; reason?: string; message?: string; batch_key?: string }>
  }>('/plus-activation/batches', {
    method: 'POST',
    body: JSON.stringify({
      keys,
      name: options.name || '',
      channel: options.channel || 'upi',
      dry_run: Boolean(options.dry_run),
      submit_rate_per_min: options.submit_rate_per_min ?? 49,
      max_in_flight: options.max_in_flight ?? 16,
    }),
  })
}

export function listPlusActivationBatches(options: { signal?: AbortSignal; status?: string; limit?: number; offset?: number } = {}) {
  const params = new URLSearchParams()
  params.set('status', options.status || 'active')
  params.set('limit', String(Math.max(1, options.limit ?? 50)))
  params.set('offset', String(Math.max(0, options.offset ?? 0)))
  return apiFetch<{ items: PlusActivationBatch[]; total: number; limit: number; offset: number }>(`/plus-activation/batches?${params.toString()}`, { signal: options.signal })
}

export function getPlusActivationBatch(batchKey: string, options: { signal?: AbortSignal } = {}) {
  return apiFetch<{ ok: boolean; batch: PlusActivationBatch; exports: PlusActivationExport[] }>(`/plus-activation/batches/${encodeURIComponent(batchKey)}`, { signal: options.signal })
}

export function listPlusActivationBatchItems(batchKey: string, options: { signal?: AbortSignal; status?: string; search?: string; error?: string; include_exported?: boolean; limit?: number; offset?: number } = {}) {
  const params = new URLSearchParams()
  if (options.status) params.set('status', options.status)
  if (options.search) params.set('search', options.search)
  if (options.error) params.set('error', options.error)
  params.set('include_exported', String(options.include_exported ?? true))
  params.set('limit', String(Math.max(1, options.limit ?? 80)))
  params.set('offset', String(Math.max(0, options.offset ?? 0)))
  return apiFetch<{ ok: boolean; items: PlusActivationBatchItem[]; total: number; limit: number; offset: number }>(`/plus-activation/batches/${encodeURIComponent(batchKey)}/items?${params.toString()}`, { signal: options.signal })
}

export function refreshPlusActivationBatch(batchKey: string) {
  return apiFetch<{ ok: boolean; batch: PlusActivationBatch; remote_refresh?: { checked: number; updated: number } }>(`/plus-activation/batches/${encodeURIComponent(batchKey)}/refresh`, { method: 'POST' })
}

export function retryPlusActivationBatch(batchKey: string, options: { keys?: string[]; statuses?: string[]; channel?: string } = {}) {
  return apiFetch<{ ok: boolean; retried?: number; message?: string; batch?: PlusActivationBatch }>(`/plus-activation/batches/${encodeURIComponent(batchKey)}/retry`, {
    method: 'POST',
    body: JSON.stringify({ keys: options.keys || [], statuses: options.statuses || [], channel: options.channel || 'upi' }),
  })
}

export function releasePlusActivationBatch(batchKey: string, options: { keys?: string[]; statuses?: string[] } = {}) {
  return apiFetch<{ ok: boolean; released?: number; message?: string; batch?: PlusActivationBatch }>(`/plus-activation/batches/${encodeURIComponent(batchKey)}/release`, {
    method: 'POST',
    body: JSON.stringify({ keys: options.keys || [], statuses: options.statuses || [] }),
  })
}

export function showPlusActivationBatchAccounts(batchKey: string, options: { keys?: string[] } = {}) {
  return apiFetch<{ ok: boolean; visible?: number; message?: string; batch?: PlusActivationBatch }>(`/plus-activation/batches/${encodeURIComponent(batchKey)}/show-accounts`, {
    method: 'POST',
    body: JSON.stringify({ keys: options.keys || [] }),
  })
}

export function exportPlusActivationBatch(batchKey: string, options: { format?: 'txt' | 'csv' | 'json'; include_already_exported?: boolean; archive_after_export?: boolean } = {}) {
  return apiFetch<{ ok: boolean; message?: string; export?: PlusActivationExport; count: number; file_name?: string; download_url?: string; text?: string; archived?: number }>(`/plus-activation/batches/${encodeURIComponent(batchKey)}/export-plus`, {
    method: 'POST',
    body: JSON.stringify({
      format: options.format || 'txt',
      include_already_exported: Boolean(options.include_already_exported),
      archive_after_export: options.archive_after_export ?? true,
    }),
  })
}

export function archivePlusActivationBatch(batchKey: string, force = false) {
  return apiFetch<{ ok: boolean; message?: string; batch?: PlusActivationBatch }>(`/plus-activation/batches/${encodeURIComponent(batchKey)}/archive`, {
    method: 'POST',
    body: JSON.stringify({ force }),
  })
}

export function getActivationQueueStats(options: { signal?: AbortSignal } = {}) {
  return apiFetch<ActivationQueueStats>('/activation/queue-stats', { signal: options.signal })
}

export async function releaseActivation(key: string) {
  const res = await apiFetch<ActivationReleaseResponse>(`/accounts/${encodeURIComponent(key)}/activation/release`, { method: 'POST' })
  return { ...res, account: res.account ? normalizeAccount(res.account) : undefined }
}

export async function releaseActivations(keys: string[]) {
  const res = await apiFetch<{
    ok: boolean
    released: number
    failed: number
    message?: string
    results: Array<{
      key: string
      ok?: boolean
      message?: string
      status_code?: number
      activation_status?: ActivationStatus
      account?: Account
    }>
  }>('/accounts-bulk/activation/release', {
    method: 'POST',
    body: JSON.stringify({ keys }),
  })
  return {
    ...res,
    results: (res.results || []).map((item) => ({
      ...item,
      account: item.account ? normalizeAccount(item.account) : undefined,
    })),
  }
}

export async function listActivationTasks(options: { signal?: AbortSignal; status?: string; limit?: number } = {}) {
  const params = new URLSearchParams()
  params.set('limit', String(Math.max(1, options.limit ?? 100000)))
  if (options.status) params.set('status', options.status)
  const res = await apiFetch<{ ok: boolean; items: Account[]; total: number; truncated?: boolean; stats?: ActivationQueueStats }>(`/activation/tasks?${params.toString()}`, { signal: options.signal })
  return { ...res, items: (res.items || []).map(normalizeAccount), truncated: Boolean(res.truncated) }
}

export async function refreshActivationTasks(keys: string[] = []) {
  const res = await apiFetch<{ ok: boolean; checked: number; updated: number; message?: string; items?: Account[]; stats?: ActivationQueueStats }>('/activation/tasks/refresh', {
    method: 'POST',
    body: JSON.stringify({ keys }),
  })
  return { ...res, items: (res.items || []).map(normalizeAccount) }
}

export async function retryActivationTasks(keys: string[] = [], channel = 'upi') {
  return apiFetch<{ ok: boolean; accepted?: number; queued?: number; skipped?: number; failed?: number; message?: string; task_id?: string }>('/activation/tasks/retry', {
    method: 'POST',
    body: JSON.stringify({ keys, channel }),
  })
}

export function issueActivationClientKey(request: ActivationClientKeyIssueRequest) {
  return apiFetch<ActivationClientKeyIssueResponse>('/activation/client-key/issue', {
    method: 'POST',
    body: JSON.stringify(request),
  })
}
export function resumeOAuthAccount(key: string, headed = true, options: { oauth_callback_mode?: string; cpa_base_url?: string; cpa_management_key?: string; bind_sms_provider?: string; bind_sms_phone_url?: string; bind_sms_country?: string; bind_sms_service?: string; bind_country_code?: string } = {}) {
  return apiFetch<{ok: boolean; task: Task}>(`/accounts/${encodeURIComponent(key)}/resume-oauth`, { method: 'POST', body: JSON.stringify({ headed, ...options }) })
}

export function protocolBindAccount(key: string, options: { oauth_callback_mode?: string; cpa_base_url?: string; cpa_management_key?: string; bind_sms_provider?: string; bind_sms_phone_url?: string; bind_sms_country?: string; bind_sms_service?: string; bind_country_code?: string } = {}) {
  return apiFetch<{ok: boolean; task: Task}>(`/accounts/${encodeURIComponent(key)}/protocol-bind`, { method: 'POST', body: JSON.stringify({ oauth_callback_mode: 'cpa', ...options }) })
}

export async function syncCpaToken(key: string) {
  const res = await apiFetch<{ok: boolean; account: Account; file: string; has_refresh_token: boolean}>(`/accounts/${encodeURIComponent(key)}/sync-cpa-token`, { method: 'POST' })
  return { ...res, account: normalizeAccount(res.account) }
}

export async function syncCpaTokens(keys: string[]) {
  const res = await apiFetch<{ok: boolean; synced: number; results: Array<{ok?: boolean; key: string; account?: Account; file?: string; has_refresh_token?: boolean; message?: string}>}>('/accounts-bulk/sync-cpa-token', {
    method: 'POST',
    body: JSON.stringify({ keys }),
  })
  return { ...res, results: res.results.map((item) => item.account ? { ...item, account: normalizeAccount(item.account) } : item) }
}

export async function refreshAccountAccessToken(key: string, options: { use_saved_proxy?: boolean; save_storage?: boolean } = {}) {
  const res = await apiFetch<{ok: boolean; account: Account; has_access_token: boolean; token_length: number; storage_file: string; proxy_enabled: boolean}>(`/accounts/${encodeURIComponent(key)}/refresh-access-token`, {
    method: 'POST',
    body: JSON.stringify({ use_saved_proxy: true, save_storage: true, ...options }),
  })
  return { ...res, account: normalizeAccount(res.account) }
}

export function bindBillingEmail(key: string, options: { headed?: boolean; mailbox_provider?: string; proxy_region?: string } = {}) {
  return apiFetch<{ok: boolean; task: Task}>(`/accounts/${encodeURIComponent(key)}/bind-billing-email`, {
    method: 'POST',
    body: JSON.stringify({ headed: true, mailbox_provider: 'icloud_api', proxy_region: 'JP', ...options }),
  })
}

export function bindBillingEmails(keys: string[], options: { headed?: boolean; mailbox_provider?: string; proxy_region?: string } = {}) {
  return apiFetch<{ok: boolean; started: number; results: Array<{ok?: boolean; key: string; task?: Task; message?: string}>}>('/accounts-bulk/bind-billing-email', {
    method: 'POST',
    body: JSON.stringify({ keys, headed: true, mailbox_provider: 'icloud_api', proxy_region: 'JP', ...options }),
  })
}

export async function refreshAccountAccessTokens(keys: string[]) {
  const res = await apiFetch<{ok: boolean; refreshed: number; results: Array<{ok?: boolean; key: string; account?: Account; token_length?: number; message?: string}>}>('/accounts-bulk/refresh-access-token', {
    method: 'POST',
    body: JSON.stringify({ keys }),
  })
  return { ...res, results: res.results.map((item) => item.account ? { ...item, account: normalizeAccount(item.account) } : item) }
}




export async function getAccountExportFields() {
  const res = await apiFetch<{ok: boolean; fields: AccountExportField[]}>('/accounts/export-fields')
  return res.fields
}

export async function exportAccount(key: string, fields: string[] = []) {
  const res = await apiFetch<{ok: boolean; product: AccountExport}>(`/accounts/${encodeURIComponent(key)}/export`, {
    method: 'POST',
    body: JSON.stringify({ fields }),
  })
  return res.product
}

export async function exportAccounts(keys: string[], fields: string[] = []) {
  return apiFetch<{ok: boolean; count: number; products: AccountExport[]; exported_keys: string[]; missing: string[]}>(`/accounts-bulk/export`, {
    method: 'POST',
    body: JSON.stringify({ keys, fields }),
  })
}

export async function exportPlusProductsTxt(keys: string[] = [], onlyVerified = true, archiveAfterExport = false) {
  return apiFetch<{
    ok: boolean
    count: number
    skipped_count: number
    kind_counts?: Record<string, number>
    text: string
    items?: Array<{ key: string; email: string; kind: string; line: string; plus_status?: string; plan_type?: string }>
    skipped?: Array<{ key: string; email: string; reason: string }>
    exported_keys?: string[]
    archived?: number
    archived_keys?: string[]
    archive_missing?: string[]
    message?: string
  }>('/accounts-bulk/export-plus-txt', {
    method: 'POST',
    body: JSON.stringify({ keys, only_verified: onlyVerified, archive_after_export: archiveAfterExport }),
  })
}

export async function exportAtProductsTxt(keys: string[] = [], archiveAfterExport = false) {
  return apiFetch<{
    ok: boolean
    count: number
    skipped_count: number
    kind_counts?: Record<string, number>
    text: string
    items?: Array<{ key: string; email: string; kind: string; plan_type?: string; has_access_token?: boolean }>
    skipped?: Array<{ key: string; email: string; reason: string }>
    exported_keys?: string[]
    archived?: number
    archived_keys?: string[]
    archive_missing?: string[]
    message?: string
  }>('/accounts-bulk/export-at-txt', {
    method: 'POST',
    body: JSON.stringify({ keys, archive_after_export: archiveAfterExport }),
  })
}

export function listArchiveBatches(options: { signal?: AbortSignal; limit?: number } = {}) {
  const params = new URLSearchParams()
  params.set('limit', String(Math.max(1, options.limit ?? 100)))
  return apiFetch<{ ok: boolean; items: ArchiveBatch[]; total: number }>(`/archive-batches?${params}`, {
    signal: options.signal,
  })
}

export function restoreArchiveBatch(batchKey: string) {
  return apiFetch<{ ok: boolean; restored: number; batch?: ArchiveBatch; message?: string }>(
    `/archive-batches/${encodeURIComponent(batchKey)}/restore`,
    { method: 'POST' },
  )
}

export function archiveAccountsOlderThan(days = 3, options: { name?: string; reason?: string } = {}) {
  return apiFetch<{
    ok: boolean
    archived: number
    product_count?: number
    plus_count?: number
    free_count?: number
    other_count?: number
    cutoff_at?: string
    days?: number
    batch?: ArchiveBatch
    message?: string
  }>('/archive-batches/archive-older-than', {
    method: 'POST',
    body: JSON.stringify({
      days,
      name: options.name || '',
      reason: options.reason || 'older_than_days',
    }),
  })
}


export async function getAccountTokens(key: string) {
  const res = await apiFetch<{ok: boolean; tokens: AccountTokens}>(`/accounts/${encodeURIComponent(key)}/tokens`)
  return res.tokens
}

export function listAccountBrowserSessions() {
  return apiFetch<{ok: boolean; items: BrowserSessionItem[]; max_sessions: number}>('/account-browser-sessions')
}

export function openAccountBrowser(key: string, options: { target_url?: string; use_saved_proxy?: boolean; browser_engine?: string; headed?: boolean; save_on_close?: boolean } = {}) {
  return apiFetch<{ok: boolean; session: BrowserSessionItem}>(`/accounts/${encodeURIComponent(key)}/open-browser`, {
    method: 'POST',
    body: JSON.stringify({ target_url: 'https://chatgpt.com/', use_saved_proxy: true, browser_engine: 'auto', headed: true, save_on_close: false, ...options }),
  })
}

export function saveAccountBrowserSession(sessionId: string) {
  return apiFetch<{ok: boolean; session: BrowserSessionItem}>(`/account-browser-sessions/${encodeURIComponent(sessionId)}/save`, { method: 'POST' })
}

export function closeAccountBrowserSession(sessionId: string, save = false) {
  return apiFetch<{ok: boolean; session: BrowserSessionItem}>(`/account-browser-sessions/${encodeURIComponent(sessionId)}/close`, {
    method: 'POST',
    body: JSON.stringify({ save }),
  })
}


/* ── Registration ── */

export function startRegistration(body: RegisterRequest) {
  return apiFetch<{
    ok: boolean
    run_id: string
    batch_id?: string
    count?: number
    accepted?: number
    created?: number
    creating?: number
    async_create?: boolean
    threads?: number
    message?: string
  }>('/register', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function getRegistrationStatus(runId: string): Promise<RunStatus> {
  const res = await apiFetch<Record<string, unknown>>(
    `/register/${encodeURIComponent(runId)}/status`,
  )
  const rawStatus = String(res.status ?? 'unknown')
  return {
    run_id: String(res.run_id ?? res.id ?? runId),
    status: rawStatus === 'success' ? 'complete' : rawStatus,
    stage: typeof res.stage === 'string' ? res.stage : typeof res.step === 'string' ? res.step : undefined,
    progress: typeof res.progress === 'number' ? res.progress : undefined,
    message: typeof res.message === 'string' ? res.message : undefined,
    error: typeof res.error === 'string' ? res.error : Array.isArray(res.errors) ? res.errors.join('\n') : undefined,
    steps_completed: Array.isArray(res.steps_completed) ? res.steps_completed.map(String) : undefined,
  }
}

export function cancelRegistration(runId: string) {
  return apiFetch<{ok: boolean}>(
    `/register/${encodeURIComponent(runId)}/cancel`,
    { method: 'POST' },
  )
}

/* ── Tasks ── */

export async function getTasks(params?: { status?: string; limit?: number; offset?: number; signal?: AbortSignal }) {
  const query = new URLSearchParams()
  if (params?.status) query.set('status', params.status)
  if (params?.limit) query.set('limit', String(params.limit))
  if (params?.offset) query.set('offset', String(params.offset))
  const res = await apiFetch<{ok: boolean; items: Task[]; counts?: Record<string, number>}>(`/tasks${query.toString() ? `?${query}` : ''}`, { signal: params?.signal })
  return res
}

export async function getTaskBatches(params?: { limit?: number; since?: string; signal?: AbortSignal }) {
  const query = new URLSearchParams()
  if (params?.limit) query.set('limit', String(params.limit))
  if (params?.since) query.set('since', params.since)
  const res = await apiFetch<{
    ok: boolean
    counts?: Record<string, number>
    summary?: {
      total: number
      running: number
      queued: number
      succeeded: number
      failed: number
      active: number
    }
    batches?: Array<{
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
    }>
    limit?: number
  }>(`/tasks/batches${query.toString() ? `?${query}` : ''}`, { signal: params?.signal })
  return res
}

export async function exportTaskBatches(options: {
  batchIds: string[]
  fields?: string[]
  onlySucceeded?: boolean
  archiveAfterExport?: boolean
}) {
  return apiFetch<{
    ok: boolean
    message?: string
    count: number
    products: AccountExport[]
    exported_keys: string[]
    missing: string[]
    batch_ids: string[]
    by_batch?: Record<string, { account_count?: number; task_count?: number; exported?: number }>
    archived?: number
    archived_keys?: string[]
    archive_missing?: string[]
  }>('/tasks/batches/export', {
    method: 'POST',
    body: JSON.stringify({
      batch_ids: options.batchIds,
      fields: options.fields || [],
      only_succeeded: options.onlySucceeded ?? true,
      archive_after_export: options.archiveAfterExport ?? false,
    }),
  })
}

export async function exportTaskBatchesAtTxt(options: {
  batchIds: string[]
  onlySucceeded?: boolean
  archiveAfterExport?: boolean
  chunkSize?: number
  writeDir?: string
  stamp?: string
  onlyUnexported?: boolean
}) {
  return apiFetch<{
    ok: boolean
    message?: string
    count: number
    new_count?: number
    total_ready?: number
    text?: string
    skipped_count?: number
    kind_counts?: Record<string, number>
    exported_keys?: string[]
    batch_ids?: string[]
    files?: Array<{ path: string; name: string; count: number; part?: number }>
    dir?: string
    stamp?: string
    chunk_size?: number
    archived?: number
  }>('/tasks/batches/export-at-txt', {
    method: 'POST',
    body: JSON.stringify({
      batch_ids: options.batchIds,
      only_succeeded: options.onlySucceeded ?? true,
      archive_after_export: options.archiveAfterExport ?? false,
      chunk_size: options.chunkSize ?? 0,
      write_dir: options.writeDir || '',
      stamp: options.stamp || '',
      only_unexported: options.onlyUnexported ?? true,
    }),
  })
}


export async function getTask(id: string) {
  const res = await apiFetch<{ok: boolean; task: Task}>(`/tasks/${encodeURIComponent(id)}`)
  return res.task
}

export async function getTaskEvents(id: string) {
  const res = await apiFetch<{ok: boolean; items: TaskEvent[]}>(`/tasks/${encodeURIComponent(id)}/events`)
  return res.items
}

export async function getTaskLogs(id: string) {
  const text = await fetch(`/api/tasks/${encodeURIComponent(id)}/logs`).then((r) => r.text())
  return { lines: text.split(/\r?\n/) }
}

export function getTaskLogStreamUrl(id: string): string {
  return `/api/tasks/${encodeURIComponent(id)}/logs/stream`
}

export async function stopTask(id: string) {
  const res = await apiFetch<{ok: boolean; stopped: boolean}>(`/tasks/${encodeURIComponent(id)}/stop`, { method: 'POST' })
  return res
}

export async function stopAllTasks() {
  const res = await apiFetch<{ok: boolean; requested: number; stopped: number; failed: number}>('/tasks/stop-all', { method: 'POST' })
  return res
}

export async function retryTask(id: string) {
  const res = await apiFetch<{ok: boolean; task?: Task; message?: string}>(`/tasks/${encodeURIComponent(id)}/retry`, { method: 'POST' })
  if (!res.ok || !res.task) throw new Error(res.message || 'Task not retryable')
  return res.task
}

/* ── Providers ── */

export async function getProviders() {
  const res = await apiFetch<{ok: boolean; items: ProviderInfo[]}>('/providers')
  return res.items
}

export function testProvider(body: ProviderTestRequest) {
  return apiFetch<ProviderTestResult>('/providers/test', {
    method: 'POST',
    body: JSON.stringify({ provider_type: body.provider_type, provider_name: body.provider_name, data: { overrides: body.settings ?? {} } }),
  })
}

export async function saveProvider(body: ProviderSaveRequest) {
  const res = await apiFetch<{ok: boolean; provider: ProviderInfo}>('/providers', {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return res.provider
}

export async function getResourceCategories() {
  const res = await apiFetch<{ok: boolean; items: ResourceCategoryOption[]}>('/resources/categories')
  return res.items
}

export async function getResources(params: {resource_type?: string; provider?: string; status?: string} = {}) {
  const query = new URLSearchParams()
  if (params.resource_type) query.set('resource_type', params.resource_type)
  if (params.provider) query.set('provider', params.provider)
  if (params.status) query.set('status', params.status)
  const res = await apiFetch<{ok: boolean; items: ResourceItem[]}>(`/resources${query.toString() ? `?${query}` : ''}`)
  return res.items
}

export function setResourceStatus(id: number, status: string, cooldown_seconds = 0, error = '') {
  return apiFetch<{ok: boolean}>(`/resources/${id}/status`, {
    method: 'POST',
    body: JSON.stringify({status, cooldown_seconds, error}),
  })
}

export function importResources(body: ResourceImportRequest) {
  return apiFetch<{ok: boolean; count: number}>('/resources/import', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function setResourceStatusBulk(body: ResourceBulkStatusRequest) {
  return apiFetch<{ok: boolean; count: number}>('/resources/status/bulk', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function deleteResourcesBulk(body: Omit<ResourceBulkStatusRequest, 'status'>) {
  return apiFetch<{ok: boolean; count: number}>('/resources/delete/bulk', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function checkProxyHealth(text: string, external = false) {
  return apiFetch<ProxyHealthCheckResult>('/resources/proxy/health-check', {
    method: 'POST',
    body: JSON.stringify({text, external}),
  })
}

export function checkResourceCapacity(body: { need_phone?: number; need_bind_phone?: number; need_proxy?: number; need_email?: number }) {
  return apiFetch<ResourceCapacityResult>('/resources/capacity-check', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function recoverStaleResources(lease_ttl_seconds = 1800) {
  return apiFetch<{ok: boolean; recovered: number}>(`/resources/recover-stale?lease_ttl_seconds=${lease_ttl_seconds}`, {method: 'POST'})
}

/* ── Config ── */

export function getConfig() {
  return apiFetch<ConfigPayload>('/config')
}

export function saveConfig(body: Record<string, unknown>) {
  return apiFetch<ConfigPayload>('/config', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/* ── Stats ── */

export async function getStatsOverview() {
  const res = await apiFetch<StatsOverview>('/stats/overview')
  return res
}

export async function getStatsByDay(days = 7) {
  const res = await apiFetch<{ok: boolean; items: DailyStats[]}>(`/stats/by-day?days=${days}`)
  return res.items
}

export async function getStatsByProxy() {
  const res = await apiFetch<{ok: boolean; items: ProxyStats[]}>('/stats/by-proxy')
  return res.items
}

export async function getStatsErrors() {
  const res = await apiFetch<{ok: boolean; items: ErrorStat[]}>('/stats/errors')
  return res.items
}

/* ── Email ── */

export function receiveEmail() {
  return apiFetch<unknown>('/receive-email', { method: 'POST' })
}

export function getEmailOtp(email: string) {
  return apiFetch<EmailOtpResponse>(
    `/email-otp/${encodeURIComponent(email)}`,
  )
}
