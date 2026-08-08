# Email re-lease + OTP hardening (2026-07-24)

## Why

n=10 true-parallel canary (`n10p_seed` summary_20260724_093829): **8/10 OK, 0 CF**.

The two failures were **not concurrency**:

| Worker | Stage | Root cause | Evidence |
|---|---|---|---|
| w01 | S9 OTP | Graph never saw OpenAI mail | `last=graph_no_openai_code` after 6m |
| w06 | S11 create_account | Email already registered | wire `user_already_exists` |

## What we ship

### 1. S11 `user_already_exists` → re-lease

- Protocol already classifies S11 400 + `user_already_exists` as `email_already_used` (`classifyHTTPFailure`).
- CLI now treats that as **email burn + full S0 restart** inside `-email-tries` loop (same as early S6 email-used).
- Mark: `resource_pool.status=used`.

### 2. OTP dead mailbox → re-lease

- On `graph_no_openai_code` / OTP timeout / `invalid_grant`, CLI:
  - marks mailbox `cooldown` (or `disabled` for token auth failures)
  - writes FAIL artifact
  - **re-leases a new email** and restarts S0 when tries remain

### 3. Graph OTP scans Junk

- `tryFetchOutlookOTP` polls well-known folders: **`inbox` then `junkemail`**.
- Prefer inbox code when both present.
- Mock-host rewrite keeps unit tests host-portable.

### 4. Helpers (CLI)

- `isEmailAlreadyUsedErr`
- `isOTPMailboxRetryable`
- `mailboxStatusForErr` (aligned with job `mailboxStatusForFailure`)

## Flow

```text
for try in 1..email-tries:
  lease email
  S0 → S9
  if email-used / proxy-dead → re-lease / remint
  WaitForOTP (inbox + junk)
  if OTP dead mailbox → mark cooldown → re-lease
  S10 → S14
  if S11 user_already_exists → mark used → re-lease
  if access_token → success
```

## Tests

- `cmd/pure-go-register/main_helpers_test.go` — classifier unit tests
- `mailbox.TestWaitForOTPOutlookGraphJunkFolder` — junk recovery
- existing `protocol.TestClassifyHTTPFailureAlreadyExists`

## Not claimed

- Does not invent OpenAI mail when Graph truly has none.
- Does not auto-solve CF.
- Does not change job daemon path beyond existing `email_already_used` classification (daemon already had it).
