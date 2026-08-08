/* ── Health ── */

export interface HealthStatus {
  status: string
}

/* ── Accounts ── */

export type AccountHealthStatus =
  | 'active'
  | 'active_plus'
  | 'active_free'
  | 'token_expired'
  | 'session_expired'
  | 'login_required'
  | 'email_verification_required'
  | 'phone_verification_required'
  | 'identity_verification_required'
  | 'captcha_required'
  | 'account_suspended'
  | 'account_disabled'
  | 'account_deactivated'
  | 'access_denied'
  | 'api_forbidden'
  | 'proxy_failed'
  | 'rate_limited'
  | 'missing_material'
  | 'unknown'

export type ActivationStatus =
  | 'idle'
  | 'reserved'
  | 'queued'
  | 'submitting'
  | 'submit_unknown'
  | 'submitted'
  | 'processing'
  | 'verifying'
  | 'success'
  | 'verified'
  | 'active'
  | 'failed'
  | 'expired'
  | 'releasable'
  | 'replace_account'
  | 'cancelled'
  | 'released'
  | 'exported'
  | 'archived'
  | 'skipped'

export type PlusBatchStatus = 'queued' | 'running' | 'paused' | 'completed' | 'completed_with_failures' | 'cancelled' | 'archived'

export interface PlusActivationBatch {
  id: number
  batch_key: string
  name: string
  provider: string
  channel: string
  status: PlusBatchStatus | string
  requested_count: number
  accepted_count: number
  skipped_count: number
  total_count: number
  queued_count: number
  submitting_count: number
  submit_unknown_count: number
  submitted_count: number
  processing_count: number
  verifying_count: number
  verified_count: number
  failed_count: number
  releasable_count: number
  released_count: number
  exported_count: number
  archived_count: number
  cdk_consumed_count: number
  progress_percent: number
  success_rate_percent: number
  last_error?: string
  error_summary_json?: string
  created_at: string
  started_at?: string
  finished_at?: string
  updated_at: string
  archived_at?: string
}

export interface PlusActivationBatchItem {
  id: number
  batch_id: number
  batch_key: string
  item_key: string
  account_id_ref: number
  account_key: string
  email: string
  status: ActivationStatus | string
  provider: string
  channel: string
  remote_task_id?: string
  activation_attempt?: number
  retry_count?: number
  activation_error?: string
  activation_display?: string
  can_release?: number
  cdk_consumed?: number
  exported_at?: string
  export_key?: string
  archived_at?: string
  submitted_at?: string
  finished_at?: string
  released_at?: string
  last_polled_at?: string
  created_at: string
  updated_at: string
}

export interface PlusActivationExport {
  id: number
  export_key: string
  batch_key: string
  kind: string
  format: string
  file_path: string
  file_name: string
  count: number
  checksum: string
  created_at: string
}

export interface ArchiveBatch {
  id?: number
  batch_key: string
  name?: string
  reason?: string
  total_count?: number
  product_count?: number
  plus_count?: number
  free_count?: number
  other_count?: number
  restored_count?: number
  active_count?: number
  cutoff_at?: string
  created_at?: string
  updated_at?: string
  notes?: string
}
export interface ActivationQueueConfig {
  enabled: boolean
  has_key: boolean
  key_count: number
  key_prefixes: string[]
  client_keys: string[]
  submit_per_key_per_min: number
  poll_interval_sec: number
  poll_timeout_sec: number
  auto_verify_plus: boolean
}

export interface ActivationQueueStats {
  ok: boolean
  active: number
  counts: Partial<Record<ActivationStatus, number>>
  config: ActivationQueueConfig
  worker_started?: boolean
}

export interface ActivationReleaseResponse {
  ok: boolean
  message: string
  key?: string
  activation_status?: ActivationStatus
  activation_task_id?: string
  account?: Account
}

export interface ActivationClientKeyIssueRequest {
  cdk: string
  note?: string
  rotate?: boolean
}

export interface ActivationClientKeyIssueResponse {
  ok: boolean
  client_key: string
  key_prefix: string
  message?: string
  config?: ActivationQueueConfig
  createdAt?: string
  created_at?: string
  expiresAt?: string
  expires_at?: string
  expiresIn?: number
  expires_in?: number
  id?: string
  keyId?: string
  key_id?: string
  rotate?: boolean
  [field: string]: unknown
}

export interface Account {
  key: string
  account_key?: string
  account_id?: string
  email?: string
  billing_email?: string
  codex_email?: string
  stage?: string
  status?: string
  password?: string
  has_password?: boolean
  registration_mode?: string
  display_name?: string
  login_identifier?: string
  registration_status?: string
  registration_task_id?: string
  registration_started_at?: string
  registration_completed_at?: string
  registration_error?: string
  plus_status?: string
  plus_verified_at?: string
  plus_check_source?: string
  plus_check_error?: string
  export_status?: string
  export_kind?: string
  exported_at?: string
  activation_provider?: string
  activation_status?: ActivationStatus
  activation_channel?: string
  activation_task_id?: string
  activation_error?: string
  activation_display?: string
  activation_can_release?: number
  activation_cdk_consumed?: number
  activation_submitted_at?: string
  activation_finished_at?: string
  activation_updated_at?: string
  active_plus_batch_id?: number
  active_plus_batch_key?: string
  active_plus_item_id?: number
  plus_batch_status?: string
  plus_reserved_at?: string
  plus_archived_at?: string
  plus_export_batch_key?: string
  plus_export_key?: string
  health_status?: AccountHealthStatus
  health_checked_at?: string
  health_check_source?: string
  health_check_error?: string
  health_message?: string
  account_health_status?: AccountHealthStatus
  account_health_checked_at?: string
  account_health_source?: string
  account_health_error?: string
  account_health_detail_json?: string
  binding_status?: string
  binding_task_id?: string
  binding_provider?: string
  binding_phone_number?: string
  binding_completed_at?: string
  binding_error?: string
  binding_started_at?: string
  oauth_callback_mode?: string
  cpa_base_url?: string
  cpa_submitted_at?: string
  cpa_submit_status?: string
  cpa_submit_error?: string
  cpa_auth_file_name?: string
  cpa_auth_file_json?: string
  cpa_synced_at?: string
  cpa_sync_error?: string
  registration_phone_resource_id?: number
  binding_phone_resource_id?: number
  email_resource_id?: number
  proxy_resource_id?: number
  registration_proxy_exit_ip?: string
  registration_proxy_region?: string
  resume_file?: string
  storage_file?: string
  account_file?: string
  sms_phone?: string
  phone_number?: string
  provider?: string
  proxy_region?: string
  headed?: boolean
  created_at?: string
  updated_at?: string
  plan_type?: string
  paths?: Record<string, string>
  proxy?: Record<string, string>
  tokens?: Record<string, boolean>
}

export interface AccountHealthCheckResult {
  key: string
  ok?: boolean
  status?: AccountHealthStatus
  health_status?: AccountHealthStatus
  account?: Account
  message?: string
  error?: string
}

export interface AccountHealthCheckResponse {
  ok: boolean
  checked: number
  results?: AccountHealthCheckResult[]
  accounts?: Account[]
  counts?: Partial<Record<AccountHealthStatus | string, number>>
}

export interface PlusVerificationResultItem {
  key: string
  ok: boolean
  status_code?: number
  account?: Account
  plan_type?: string
  source?: string
  paid?: boolean
  message?: string
  error_code?: string
}

export interface PlusVerificationProgress {
  ok: boolean
  task_id: string
  total: number
  completed: number
  paid: number
  failed: number
  running: boolean
  cancelled: boolean
  results: PlusVerificationResultItem[]
  pending_keys?: string[]
  in_flight_keys?: string[]
  message?: string
  workers?: number
  backend?: string
}

export interface BrowserSessionItem {
  id: string
  account_key: string
  account_label: string
  storage_file: string
  target_url: string
  proxy_enabled: boolean
  proxy_hint: string
  engine: string
  headed: boolean
  save_on_close: boolean
  status: string
  message: string
  url: string
  title: string
  opened_at: string
  updated_at: string
  closed_at: string
  saved_at: string
  saved_path: string
  backup_path: string
  error: string
}


export interface AccountExportField {
  key: string
  label: string
  description: string
}

export interface AccountExport {
  _field_descriptions?: Record<string, { label: string; description: string }>
  [field: string]: unknown
}

export interface AccountTokens {
  access_token?: string | null
  refresh_token?: string | null
  id_token?: string | null
  chatgpt_access_token_initial?: string | null
  token_expires_at?: string | null
  [tokenName: string]: string | null | undefined
}

/* ── Tasks ── */

export interface Task {
  id: string
  type?: string
  task_type?: string
  status: string
  error?: string
  log_file?: string
  created_at?: string
  finished_at?: string
  started_at?: string
  updated_at?: string
  params?: Record<string, unknown>
  result?: Record<string, unknown>
}

export interface TaskEvent {
  id?: number
  timestamp?: string
  level?: string
  event_type?: string
  message?: string
}

export interface TaskLogs {
  lines: string[]
  total?: number
}

/* ── Providers ── */

export interface ProviderFieldDefinition {
  key: string
  label: string
  help?: string
  placeholder?: string
  secret?: boolean
  multiline?: boolean
  required?: boolean
}

export interface ProviderDefinition {
  provider_type: string
  provider_name: string
  label: string
  help?: string
  fields: ProviderFieldDefinition[]
}

export interface ProviderInfo {
  provider_type: string
  provider_name: string
  enabled: boolean
  settings?: Record<string, unknown>
  definition?: ProviderDefinition
}

export interface ProviderTestRequest {
  provider_type: string
  provider_name: string
  settings?: Record<string, unknown>
}

export interface ProviderSaveRequest {
  provider_type: string
  provider_name: string
  enabled: boolean
  settings: Record<string, unknown>
}

export interface ResourceItem {
  id: number
  resource_type: string
  provider: string
  resource_key: string
  payload: Record<string, unknown>
  status: string
  lease_id: string
  leased_at: string
  cooldown_until: string
  success_count: number
  fail_count: number
  last_error: string
  updated_at: string
}

export type ResourceImportType = 'phone' | 'bind_phone' | 'proxy' | 'email' | 'icloud_email'

export interface ResourceCategoryOption {
  key: string
  resource_type: string
  provider: string
  label: string
  group: string
  importable: boolean
  total: number
  available: number
}

export interface ResourceImportRequest {
  resource_type: ResourceImportType
  provider: string
  text?: string
  /** Server-local absolute path for huge batches (avoids paste). */
  file_path?: string
  metadata?: Record<string, unknown>
}

export interface ResourceBulkStatusRequest {
  status: string
  resource_ids?: number[]
  resource_type?: string
  provider?: string
  current_status?: string
  cooldown_seconds?: number
  error?: string
}

export interface ProxyHealthCheckResult {
  ok: boolean
  checked: number
  valid: number
  external: boolean
  items: { proxy: string; ok: boolean; message: string }[]
}

export interface ResourceCapacityItem {
  resource_type: string
  provider: string
  required: number
  available: number
  leased: number
  used: number
  cooldown: number
  disabled: number
  enough: boolean
}

export interface ResourceCapacityResult {
  ok: boolean
  resources: ResourceCapacityItem[]
}


export interface ProviderTestResult {
  ok: boolean
  provider_type?: string
  message?: string
  details?: unknown
}

/* ── Registration ── */

export interface RegisterRequest {
  mode?: string
  registration_engine?: string
  sms_provider?: string
  sms_country?: string
  mailbox_provider?: string
  auto_bind_billing_email?: boolean
  billing_email_provider?: string
  proxy_mode?: string
  proxy_region?: string
  headed?: boolean
  lajiao_proxy_credential_protocol?: string
  skip_precheck?: boolean
  force_signup?: boolean
  register_count?: number
  register_threads?: number
  browser_engine?: string
  browser_channel?: string
  browser_profile_mode?: string
  browser_no_viewport?: boolean
  email_register_flow?: string
  locale?: string
  timezone_id?: string
  accept_language?: string
  email_otp_timeout?: number
  email_otp_poll_interval?: number
  mailat_protocol_use_local_bridge?: boolean
  mailat_protocol_timeout_seconds?: number
  mailat_protocol_proxy_precheck_enabled?: boolean
  mailat_protocol_proxy_attempts?: number
  mailat_protocol_proxy_preflight_timeout_seconds?: number
  email_protocol_backend?: string
  go_email_protocol_url?: string
  go_email_protocol_timeout_seconds?: number
}

export interface RegistrationRun {
  run_id: string
  status: string
  mode?: string
  sms_provider?: string
  proxy_region?: string
  started_at?: string
  finished_at?: string
  error?: string
}

export interface RunStatus {
  run_id: string
  status: string
  stage?: string
  progress?: number
  message?: string
  error?: string
  steps_completed?: string[]
}

/* ── Config ── */

export interface ConfigPayload {
  ok: boolean
  file_config?: Record<string, unknown>
  db_config?: Record<string, unknown>
  config?: Record<string, unknown>
}

/* ── Stats ── */

export interface StatsOverview {
  ok: boolean
  total_accounts: number
  active_plus: number
  today_success: number
  today_fail: number
}

export interface StatsByDay {
  ok: boolean
  items: DailyStats[]
}

export interface DailyStats {
  date: string
  success: number
  fail: number
  total: number
}

export interface StatsByProxy {
  ok: boolean
  items: ProxyStats[]
}

export interface ProxyStats {
  exit_ip: string
  region: string
  success_count: number
  fail_count: number
  is_active: number
}

export interface StatsError {
  ok: boolean
  items: ErrorStat[]
}

export interface ErrorStat {
  errors: string
  cnt: number
}

/* ── Email OTP ── */

export interface EmailOtpResponse {
  ok: boolean
  code?: string
  message?: string
}

/* ── Common ── */

export interface ApiError {
  detail?: string
  message?: string
}
