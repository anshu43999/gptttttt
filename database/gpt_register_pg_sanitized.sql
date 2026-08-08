-- GPT Register sanitized PostgreSQL database
-- Schema-only, portable (IDENTITY columns; no production sequences/data).
-- Contains NO rows: accounts, tokens, phones, emails, proxies, OTPs, tasks, provider settings.
-- Import:
--   createdb gpt_register
--   psql "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register" -f database/gpt_register_pg_sanitized.sql
-- App start also runs infrastructure/db.py migrations.

BEGIN;

DROP TABLE IF EXISTS "tasks" CASCADE;
DROP TABLE IF EXISTS "task_events" CASCADE;
DROP TABLE IF EXISTS "sms_activations" CASCADE;
DROP TABLE IF EXISTS "resource_pool" CASCADE;
DROP TABLE IF EXISTS "registration_runs" CASCADE;
DROP TABLE IF EXISTS "proxies" CASCADE;
DROP TABLE IF EXISTS "provider_settings" CASCADE;
DROP TABLE IF EXISTS "plus_activation_exports" CASCADE;
DROP TABLE IF EXISTS "plus_activation_batches" CASCADE;
DROP TABLE IF EXISTS "plus_activation_batch_items" CASCADE;
DROP TABLE IF EXISTS "email_otp_events" CASCADE;
DROP TABLE IF EXISTS "archive_batches" CASCADE;
DROP TABLE IF EXISTS "app_config" CASCADE;
DROP TABLE IF EXISTS "accounts" CASCADE;
DROP TABLE IF EXISTS "account_proxy" CASCADE;
DROP TABLE IF EXISTS "account_events" CASCADE;
DROP TABLE IF EXISTS "account_credentials" CASCADE;
DROP TABLE IF EXISTS "account_artifacts" CASCADE;

CREATE TABLE "account_artifacts" (
  "id" bigint NOT NULL,
  "account_id_ref" bigint NOT NULL,
  "artifact_type" text NOT NULL,
  "path" text NOT NULL,
  "created_at" text NOT NULL,
  "updated_at" text DEFAULT ''::text
);

CREATE TABLE "account_credentials" (
  "id" bigint NOT NULL,
  "account_id_ref" bigint NOT NULL,
  "access_token" text DEFAULT ''::text,
  "refresh_token" text DEFAULT ''::text,
  "id_token" text DEFAULT ''::text,
  "chatgpt_access_token_initial" text DEFAULT ''::text,
  "token_expires_at" text DEFAULT ''::text,
  "created_at" text NOT NULL,
  "updated_at" text NOT NULL
);

CREATE TABLE "account_events" (
  "id" bigint NOT NULL,
  "account_key" text NOT NULL,
  "task_id" text DEFAULT ''::text,
  "event_type" text NOT NULL,
  "status" text DEFAULT ''::text,
  "message" text DEFAULT ''::text,
  "payload_json" text DEFAULT '{}'::text,
  "created_at" text NOT NULL
);

CREATE TABLE "account_proxy" (
  "id" bigint NOT NULL,
  "account_id_ref" bigint NOT NULL,
  "registration_proxy" text DEFAULT ''::text,
  "registration_exit_ip" text DEFAULT ''::text,
  "registration_country" text DEFAULT ''::text,
  "subscription_check_proxy" text DEFAULT ''::text,
  "subscription_check_source" text DEFAULT ''::text,
  "created_at" text NOT NULL,
  "updated_at" text DEFAULT ''::text
);

CREATE TABLE "accounts" (
  "id" bigint NOT NULL,
  "account_key" text NOT NULL,
  "account_id" text,
  "platform" text DEFAULT 'chatgpt'::text NOT NULL,
  "phone_number" text,
  "email" text,
  "password" text,
  "plan_type" text,
  "status" text,
  "stage" text,
  "created_at" text NOT NULL,
  "updated_at" text NOT NULL,
  "last_error" text DEFAULT ''::text,
  "registration_mode" text DEFAULT ''::text,
  "display_name" text DEFAULT ''::text,
  "plus_status" text DEFAULT ''::text,
  "plus_verified_at" text DEFAULT ''::text,
  "plus_check_source" text DEFAULT ''::text,
  "plus_check_error" text DEFAULT ''::text,
  "binding_status" text DEFAULT ''::text,
  "binding_task_id" text DEFAULT ''::text,
  "binding_provider" text DEFAULT ''::text,
  "binding_phone_number" text DEFAULT ''::text,
  "binding_completed_at" text DEFAULT ''::text,
  "binding_error" text DEFAULT ''::text,
  "login_identifier" text DEFAULT ''::text,
  "registration_status" text DEFAULT ''::text,
  "registration_task_id" text DEFAULT ''::text,
  "registration_started_at" text DEFAULT ''::text,
  "registration_completed_at" text DEFAULT ''::text,
  "registration_error" text DEFAULT ''::text,
  "binding_started_at" text DEFAULT ''::text,
  "oauth_callback_mode" text DEFAULT ''::text,
  "cpa_base_url" text DEFAULT ''::text,
  "cpa_submitted_at" text DEFAULT ''::text,
  "cpa_submit_status" text DEFAULT ''::text,
  "cpa_submit_error" text DEFAULT ''::text,
  "registration_phone_resource_id" bigint DEFAULT 0,
  "binding_phone_resource_id" bigint DEFAULT 0,
  "email_resource_id" bigint DEFAULT 0,
  "proxy_resource_id" bigint DEFAULT 0,
  "registration_proxy_exit_ip" text DEFAULT ''::text,
  "registration_proxy_region" text DEFAULT ''::text,
  "resume_file" text DEFAULT ''::text,
  "storage_file" text DEFAULT ''::text,
  "account_file" text DEFAULT ''::text,
  "cpa_auth_file_name" text DEFAULT ''::text,
  "cpa_auth_file_json" text DEFAULT ''::text,
  "cpa_synced_at" text DEFAULT ''::text,
  "cpa_sync_error" text DEFAULT ''::text,
  "billing_email" text DEFAULT ''::text,
  "codex_email" text DEFAULT ''::text,
  "account_health_status" text DEFAULT ''::text,
  "account_health_checked_at" text DEFAULT ''::text,
  "account_health_source" text DEFAULT ''::text,
  "account_health_error" text DEFAULT ''::text,
  "account_health_detail_json" text DEFAULT ''::text,
  "outlook_email" text DEFAULT ''::text,
  "outlook_password" text DEFAULT ''::text,
  "outlook_client_id" text DEFAULT ''::text,
  "outlook_refresh_token" text DEFAULT ''::text,
  "export_status" text DEFAULT ''::text,
  "export_kind" text DEFAULT ''::text,
  "exported_at" text DEFAULT ''::text,
  "activation_provider" text DEFAULT ''::text,
  "activation_status" text DEFAULT ''::text,
  "activation_channel" text DEFAULT ''::text,
  "activation_task_id" text DEFAULT ''::text,
  "activation_idempotency_key" text DEFAULT ''::text,
  "activation_attempt" bigint DEFAULT 0,
  "activation_error" text DEFAULT ''::text,
  "activation_display" text DEFAULT ''::text,
  "activation_can_release" bigint DEFAULT 0,
  "activation_cdk_consumed" bigint DEFAULT 0,
  "activation_submitted_at" text DEFAULT ''::text,
  "activation_finished_at" text DEFAULT ''::text,
  "activation_updated_at" text DEFAULT ''::text,
  "activation_client_key_hash" text DEFAULT ''::text,
  "activation_submission_claim" text DEFAULT ''::text,
  "active_plus_batch_id" integer,
  "active_plus_batch_key" text DEFAULT ''::text,
  "active_plus_item_id" integer,
  "plus_batch_status" text DEFAULT ''::text,
  "plus_reserved_at" text DEFAULT ''::text,
  "plus_archived_at" text DEFAULT ''::text,
  "plus_export_batch_key" text DEFAULT ''::text,
  "plus_export_key" text DEFAULT ''::text,
  "archive_batch_key" text DEFAULT ''::text
);

CREATE TABLE "app_config" (
  "key" text NOT NULL,
  "value_json" text NOT NULL,
  "updated_at" text NOT NULL
);

CREATE TABLE "archive_batches" (
  "id" integer NOT NULL,
  "batch_key" text NOT NULL,
  "name" text DEFAULT ''::text NOT NULL,
  "reason" text DEFAULT ''::text NOT NULL,
  "total_count" integer DEFAULT 0 NOT NULL,
  "product_count" integer DEFAULT 0 NOT NULL,
  "plus_count" integer DEFAULT 0 NOT NULL,
  "free_count" integer DEFAULT 0 NOT NULL,
  "other_count" integer DEFAULT 0 NOT NULL,
  "restored_count" integer DEFAULT 0 NOT NULL,
  "active_count" integer DEFAULT 0 NOT NULL,
  "cutoff_at" text DEFAULT ''::text NOT NULL,
  "created_at" text NOT NULL,
  "updated_at" text DEFAULT ''::text NOT NULL,
  "notes" text DEFAULT ''::text NOT NULL
);

CREATE TABLE "email_otp_events" (
  "id" bigint NOT NULL,
  "email" text NOT NULL,
  "code" text NOT NULL,
  "raw_subject" text DEFAULT ''::text,
  "raw_body" text DEFAULT ''::text,
  "received_at" text DEFAULT CURRENT_TIMESTAMP NOT NULL,
  "consumed" bigint DEFAULT 0,
  "consumed_by" text DEFAULT ''::text
);

CREATE TABLE "plus_activation_batch_items" (
  "id" integer NOT NULL,
  "batch_id" integer NOT NULL,
  "batch_key" text NOT NULL,
  "item_key" text NOT NULL,
  "account_id_ref" integer NOT NULL,
  "account_key" text NOT NULL,
  "email" text DEFAULT ''::text NOT NULL,
  "status" text DEFAULT 'queued'::text NOT NULL,
  "provider" text DEFAULT 'upi'::text NOT NULL,
  "channel" text DEFAULT 'upi'::text NOT NULL,
  "remote_task_id" text DEFAULT ''::text NOT NULL,
  "idempotency_key" text DEFAULT ''::text NOT NULL,
  "client_key_hash" text DEFAULT ''::text NOT NULL,
  "activation_attempt" integer DEFAULT 0 NOT NULL,
  "retry_count" integer DEFAULT 0 NOT NULL,
  "activation_error" text DEFAULT ''::text NOT NULL,
  "activation_error_code" text DEFAULT ''::text NOT NULL,
  "activation_display" text DEFAULT ''::text NOT NULL,
  "can_release" integer DEFAULT 0 NOT NULL,
  "cdk_consumed" integer DEFAULT 0 NOT NULL,
  "exported_at" text DEFAULT ''::text NOT NULL,
  "export_key" text DEFAULT ''::text NOT NULL,
  "archived_at" text DEFAULT ''::text NOT NULL,
  "submitted_at" text DEFAULT ''::text NOT NULL,
  "finished_at" text DEFAULT ''::text NOT NULL,
  "released_at" text DEFAULT ''::text NOT NULL,
  "last_polled_at" text DEFAULT ''::text NOT NULL,
  "created_at" text NOT NULL,
  "updated_at" text NOT NULL
);

CREATE TABLE "plus_activation_batches" (
  "id" integer NOT NULL,
  "batch_key" text NOT NULL,
  "name" text DEFAULT ''::text NOT NULL,
  "provider" text DEFAULT 'upi'::text NOT NULL,
  "channel" text DEFAULT 'upi'::text NOT NULL,
  "status" text DEFAULT 'queued'::text NOT NULL,
  "requested_count" integer DEFAULT 0 NOT NULL,
  "accepted_count" integer DEFAULT 0 NOT NULL,
  "skipped_count" integer DEFAULT 0 NOT NULL,
  "total_count" integer DEFAULT 0 NOT NULL,
  "reserved_count" integer DEFAULT 0 NOT NULL,
  "queued_count" integer DEFAULT 0 NOT NULL,
  "submitting_count" integer DEFAULT 0 NOT NULL,
  "submit_unknown_count" integer DEFAULT 0 NOT NULL,
  "submitted_count" integer DEFAULT 0 NOT NULL,
  "processing_count" integer DEFAULT 0 NOT NULL,
  "verifying_count" integer DEFAULT 0 NOT NULL,
  "verified_count" integer DEFAULT 0 NOT NULL,
  "failed_count" integer DEFAULT 0 NOT NULL,
  "releasable_count" integer DEFAULT 0 NOT NULL,
  "released_count" integer DEFAULT 0 NOT NULL,
  "exported_count" integer DEFAULT 0 NOT NULL,
  "archived_count" integer DEFAULT 0 NOT NULL,
  "cdk_consumed_count" integer DEFAULT 0 NOT NULL,
  "submit_rate_per_min" integer DEFAULT 0 NOT NULL,
  "max_in_flight" integer DEFAULT 0 NOT NULL,
  "progress_percent" integer DEFAULT 0 NOT NULL,
  "success_rate_percent" integer DEFAULT 0 NOT NULL,
  "last_error" text DEFAULT ''::text NOT NULL,
  "last_error_code" text DEFAULT ''::text NOT NULL,
  "error_summary_json" text DEFAULT '{}'::text NOT NULL,
  "created_by" text DEFAULT ''::text NOT NULL,
  "created_at" text NOT NULL,
  "started_at" text DEFAULT ''::text NOT NULL,
  "finished_at" text DEFAULT ''::text NOT NULL,
  "updated_at" text NOT NULL,
  "archived_at" text DEFAULT ''::text NOT NULL
);

CREATE TABLE "plus_activation_exports" (
  "id" integer NOT NULL,
  "export_key" text NOT NULL,
  "batch_id" integer NOT NULL,
  "batch_key" text NOT NULL,
  "kind" text DEFAULT 'plus_verified'::text NOT NULL,
  "format" text DEFAULT 'txt'::text NOT NULL,
  "file_path" text DEFAULT ''::text NOT NULL,
  "file_name" text DEFAULT ''::text NOT NULL,
  "count" integer DEFAULT 0 NOT NULL,
  "checksum" text DEFAULT ''::text NOT NULL,
  "include_already_exported" integer DEFAULT 0 NOT NULL,
  "archive_after_export" integer DEFAULT 1 NOT NULL,
  "created_by" text DEFAULT ''::text NOT NULL,
  "created_at" text NOT NULL
);

CREATE TABLE "provider_settings" (
  "id" bigint NOT NULL,
  "provider_type" text NOT NULL,
  "provider_name" text NOT NULL,
  "enabled" bigint DEFAULT 1 NOT NULL,
  "settings_json" text DEFAULT '{}'::text,
  "created_at" text NOT NULL,
  "updated_at" text NOT NULL
);

CREATE TABLE "proxies" (
  "id" bigint NOT NULL,
  "url" text NOT NULL,
  "exit_ip" text DEFAULT ''::text,
  "region" text DEFAULT ''::text,
  "success_count" bigint DEFAULT 0,
  "fail_count" bigint DEFAULT 0,
  "consecutive_fails" bigint DEFAULT 0,
  "is_active" bigint DEFAULT 1,
  "last_checked" text,
  "source" text DEFAULT 'manual'::text,
  "created_at" text DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE "registration_runs" (
  "id" text NOT NULL,
  "task_id" text,
  "mode" text DEFAULT 'phone'::text,
  "status" text DEFAULT 'pending'::text NOT NULL,
  "phone" text DEFAULT ''::text,
  "email" text DEFAULT ''::text,
  "sms_provider" text DEFAULT ''::text,
  "mailbox_provider" text DEFAULT ''::text,
  "proxy_ip" text DEFAULT ''::text,
  "proxy_region" text DEFAULT ''::text,
  "plan_type" text DEFAULT ''::text,
  "access_token_obtained" bigint DEFAULT 0,
  "refresh_token_obtained" bigint DEFAULT 0,
  "steps_completed" text DEFAULT '[]'::text,
  "errors" text DEFAULT '[]'::text,
  "started_at" text,
  "finished_at" text
);

CREATE TABLE "resource_pool" (
  "id" bigint NOT NULL,
  "resource_type" text NOT NULL,
  "provider" text NOT NULL,
  "resource_key" text NOT NULL,
  "payload_json" text DEFAULT '{}'::text,
  "status" text DEFAULT 'available'::text NOT NULL,
  "lease_id" text DEFAULT ''::text,
  "leased_at" text DEFAULT ''::text,
  "cooldown_until" text DEFAULT ''::text,
  "success_count" bigint DEFAULT 0 NOT NULL,
  "fail_count" bigint DEFAULT 0 NOT NULL,
  "last_error" text DEFAULT ''::text,
  "created_at" text NOT NULL,
  "updated_at" text NOT NULL
);

CREATE TABLE "sms_activations" (
  "id" bigint NOT NULL,
  "account_id_ref" bigint,
  "provider" text DEFAULT ''::text,
  "activation_id" text DEFAULT ''::text,
  "phone_number" text DEFAULT ''::text,
  "sms_url" text DEFAULT ''::text,
  "country" text DEFAULT ''::text,
  "status" text DEFAULT ''::text,
  "last_code" text DEFAULT ''::text,
  "ignored_codes" text DEFAULT '[]'::text,
  "created_at" text NOT NULL,
  "updated_at" text NOT NULL
);

CREATE TABLE "task_events" (
  "id" bigint NOT NULL,
  "task_id" text NOT NULL,
  "timestamp" text NOT NULL,
  "level" text DEFAULT 'info'::text NOT NULL,
  "event_type" text DEFAULT 'log'::text NOT NULL,
  "message" text NOT NULL,
  "data_json" text DEFAULT '{}'::text
);

CREATE TABLE "tasks" (
  "id" text NOT NULL,
  "task_type" text NOT NULL,
  "status" text NOT NULL,
  "account_id_ref" text,
  "params_json" text DEFAULT '{}'::text,
  "result_json" text DEFAULT '{}'::text,
  "created_at" text NOT NULL,
  "started_at" text DEFAULT ''::text,
  "finished_at" text DEFAULT ''::text,
  "updated_at" text NOT NULL,
  "error" text DEFAULT ''::text,
  "retryable" bigint DEFAULT 0 NOT NULL,
  "command_json" text DEFAULT '[]'::text,
  "log_file" text DEFAULT ''::text
);

ALTER TABLE ONLY "account_artifacts" ADD CONSTRAINT "account_artifacts_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "account_credentials" ADD CONSTRAINT "account_credentials_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "account_events" ADD CONSTRAINT "account_events_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "account_proxy" ADD CONSTRAINT "account_proxy_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "accounts" ADD CONSTRAINT "accounts_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "app_config" ADD CONSTRAINT "app_config_pkey" PRIMARY KEY (key);
ALTER TABLE ONLY "archive_batches" ADD CONSTRAINT "archive_batches_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "archive_batches" ADD CONSTRAINT "archive_batches_batch_key_key" UNIQUE (batch_key);
ALTER TABLE ONLY "email_otp_events" ADD CONSTRAINT "email_otp_events_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "plus_activation_batch_items" ADD CONSTRAINT "plus_activation_batch_items_account_id_ref_fkey" FOREIGN KEY (account_id_ref) REFERENCES accounts(id) ON DELETE CASCADE;
ALTER TABLE ONLY "plus_activation_batch_items" ADD CONSTRAINT "plus_activation_batch_items_batch_id_fkey" FOREIGN KEY (batch_id) REFERENCES plus_activation_batches(id) ON DELETE CASCADE;
ALTER TABLE ONLY "plus_activation_batch_items" ADD CONSTRAINT "plus_activation_batch_items_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "plus_activation_batch_items" ADD CONSTRAINT "plus_activation_batch_items_item_key_key" UNIQUE (item_key);
ALTER TABLE ONLY "plus_activation_batches" ADD CONSTRAINT "plus_activation_batches_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "plus_activation_batches" ADD CONSTRAINT "plus_activation_batches_batch_key_key" UNIQUE (batch_key);
ALTER TABLE ONLY "plus_activation_exports" ADD CONSTRAINT "plus_activation_exports_batch_id_fkey" FOREIGN KEY (batch_id) REFERENCES plus_activation_batches(id) ON DELETE CASCADE;
ALTER TABLE ONLY "plus_activation_exports" ADD CONSTRAINT "plus_activation_exports_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "plus_activation_exports" ADD CONSTRAINT "plus_activation_exports_export_key_key" UNIQUE (export_key);
ALTER TABLE ONLY "provider_settings" ADD CONSTRAINT "provider_settings_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "proxies" ADD CONSTRAINT "proxies_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "registration_runs" ADD CONSTRAINT "registration_runs_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "resource_pool" ADD CONSTRAINT "resource_pool_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "sms_activations" ADD CONSTRAINT "sms_activations_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "task_events" ADD CONSTRAINT "task_events_pkey" PRIMARY KEY (id);
ALTER TABLE ONLY "tasks" ADD CONSTRAINT "tasks_pkey" PRIMARY KEY (id);

CREATE INDEX IF NOT EXISTS idx_account_artifacts_ref ON public.account_artifacts USING btree (account_id_ref);
CREATE UNIQUE INDEX IF NOT EXISTS ux_account_artifacts_ref_type_path ON public.account_artifacts USING btree (account_id_ref, artifact_type, path);
CREATE UNIQUE INDEX IF NOT EXISTS ux_account_credentials_account_id_ref ON public.account_credentials USING btree (account_id_ref);
CREATE INDEX IF NOT EXISTS idx_account_events_key ON public.account_events USING btree (account_key, created_at);
CREATE INDEX IF NOT EXISTS idx_account_events_task ON public.account_events USING btree (task_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_account_proxy_account_id_ref ON public.account_proxy USING btree (account_id_ref);
CREATE INDEX IF NOT EXISTS idx_accounts_active_plus_batch ON public.accounts USING btree (active_plus_batch_id, plus_batch_status);
CREATE INDEX IF NOT EXISTS idx_accounts_archive_batch ON public.accounts USING btree (archive_batch_key);
CREATE INDEX IF NOT EXISTS idx_accounts_email ON public.accounts USING btree (email);
CREATE INDEX IF NOT EXISTS idx_accounts_phone ON public.accounts USING btree (phone_number);
CREATE INDEX IF NOT EXISTS idx_accounts_plus_archived ON public.accounts USING btree (plus_archived_at);
CREATE INDEX IF NOT EXISTS idx_accounts_stage ON public.accounts USING btree (stage);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON public.accounts USING btree (status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_account_key ON public.accounts USING btree (account_key);
CREATE INDEX IF NOT EXISTS idx_archive_batches_created ON public.archive_batches USING btree (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_otp_email ON public.email_otp_events USING btree (email, consumed);
CREATE INDEX IF NOT EXISTS idx_plus_items_batch_account ON public.plus_activation_batch_items USING btree (batch_id, account_key);
CREATE INDEX IF NOT EXISTS idx_plus_items_batch_status_updated ON public.plus_activation_batch_items USING btree (batch_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_plus_items_idempotency ON public.plus_activation_batch_items USING btree (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_plus_items_remote_task ON public.plus_activation_batch_items USING btree (remote_task_id);
CREATE INDEX IF NOT EXISTS idx_plus_items_status_updated ON public.plus_activation_batch_items USING btree (status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_plus_items_one_active_per_account ON public.plus_activation_batch_items USING btree (account_id_ref) WHERE (status = ANY (ARRAY['reserved'::text, 'queued'::text, 'submitting'::text, 'submit_unknown'::text, 'submitted'::text, 'processing'::text, 'verifying'::text, 'verified'::text, 'failed'::text, 'releasable'::text, 'exported'::text]));
CREATE INDEX IF NOT EXISTS idx_plus_batches_created ON public.plus_activation_batches USING btree (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_plus_batches_status_updated ON public.plus_activation_batches USING btree (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_plus_exports_batch_created ON public.plus_activation_exports USING btree (batch_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_settings_type_name ON public.provider_settings USING btree (provider_type, provider_name);
CREATE INDEX IF NOT EXISTS idx_proxies_active ON public.proxies USING btree (is_active);
CREATE INDEX IF NOT EXISTS idx_proxies_region ON public.proxies USING btree (region);
CREATE UNIQUE INDEX IF NOT EXISTS ux_proxies_url ON public.proxies USING btree (url);
CREATE INDEX IF NOT EXISTS idx_reg_runs_status ON public.registration_runs USING btree (status, started_at);
CREATE INDEX IF NOT EXISTS idx_resource_pool_lease ON public.resource_pool USING btree (lease_id);
CREATE INDEX IF NOT EXISTS idx_resource_pool_status ON public.resource_pool USING btree (resource_type, provider, status);
CREATE INDEX IF NOT EXISTS idx_resource_pool_type_status ON public.resource_pool USING btree (resource_type, provider, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_resource_pool_type_provider_key ON public.resource_pool USING btree (resource_type, provider, resource_key);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON public.task_events USING btree (task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON public.tasks USING btree (status);
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON public.tasks USING btree (status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON public.tasks USING btree (task_type);

COMMIT;
