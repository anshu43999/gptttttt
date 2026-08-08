# 纯 Go 全量指纹协议终态方案

**状态：** 终态规格 + **无人值守实施路线**（§23）  
**日期：** 2026-07-17（§23 / 进度表 2026-07-18 更新）  
**范围：** 邮箱协议注册主链（`--at` / email-protocol-register-token）  
**执行面：** **纯 Go 唯一协议执行面**（不再以 Node 池 / Python 协议体为终态）  
**关联：** `docs/EMAIL_PROTOCOL_GO_PLAN.md`（FSM/V2 API/桥接基线；**Bundle 以本文件 v2 为准**）、`docs/TRUE_100_CONCURRENCY_FINGERPRINT_PLAN.md`（100 并发与隔离）  
**进度权威：** 以本文件 **§23.6 进度表** 为准；代码与 §23 冲突时先改代码对齐本文件，再勾进度。
 
> **最新 HAR wire 合同：** `docs/LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md` 负责 d17/d24 capture normalization、Sentinel loader/source 关系、Header/redirect/Cookie replay 与 canary。本文继续作为 FingerprintBundle/TransportProfile/纯 Go终态基线；contract/replay release 未通过前，不得以本文的终态描述替代 wire gate。

---

## 0. 一句话

做一个 **单进程 Go daemon**，每个 job 持有完整、自洽、可 checkpoint 的身份：

```text
FingerprintBundle  +  TransportProfile  +  CookieJar  +  SentinelRealm  +  FSM
```

字段尽量 **全量**（Node 设备画像 ∪ Python 传输/头模板 ∪ 本项目 wire 对拍），  
一致性 **硬约束**，并发 **100 真隔离**，代理 **只走本机 bridge**。  
不是“改个 UA”，不是“堆假字段”，不是“默认开浏览器”。

---

## 1. 目标与非目标

### 1.1 目标（全部要）

| # | 目标 | 可观察验收 |
|---|---|---|
| G1 | 纯 Go 跑通注册主链并产出可校验 session | `access_token` + schema-valid session document |
| G2 | 全量设备画像 + Client Hints + 传输画像 | Bundle/Transport 字段齐、fixture 绿 |
| G3 | TLS/H2/Header 与锁定 Chrome 基线对拍 | ClientHello/H2/header-order hash 一致 |
| G4 | 100 并发无串号 | jar / profile / oai-did / proxy / mailbox 零串扰 |
| G5 | 字段自洽 fail-closed | 任意不一致拒绝发请求 |
| G6 | 取消/崩溃/OTP 等待可恢复且不双注册 | reconcile 语义正确 |
| G7 | Node/Python 协议执行面退役 | 生产默认纯 Go；Node 仅 oracle/应急 |

### 1.2 非目标（明确不做或降级）

| 项 | 决策 | 理由 |
|---|---|---|
| 100% 像素级真 Chrome 永久等价 | 不做 | profile 会漂移；用锁定版本 + 对拍 |
| 默认每 job 启动 Playwright/Camoufox | 不做 | 与 100 并发/纯协议冲突 |
| 字体/WebGL/Audio/插件全量博物馆 | 不做进 wire | 纯 HTTP 主链用不到；仅 Sentinel realm 需要的子集 |
| 第二套通用 TLS client 作 fallback | 禁止 | 只允许 `tls-client`（必要时 uTLS 补洞） |
| 直连或绕过 bridge | 禁止 | 无 BridgeGrant 不得发网 |
| 脑补未在源码/抓包出现的字段 | 禁止 | 只收录有依据的字段 |

### 1.3 “全量”的定义（本方案口径）

**全量 = 注册链路上会出现或会被校验的全部身份维度的并集**，且彼此一致：

1. **设备画像**（Node `device-profile.ts` 全字段 + 扩展）  
2. **Client Hints 全集**（Node 现有 + 高阶补齐）  
3. **传输画像**（TLS ClientHello / ALPN / H2 / header order，Python curl_cffi 能力在 Go 落地）  
4. **请求头模板全集**（HTML / XHR / OTP 例外 / Sentinel）  
5. **Cookie / oai-did / CSRF / PKCE 会话**  
6. **Sentinel 25 项指纹 + PoW + SDK realm**  
7. **代理 sticky + locale/timezone 策略绑定**  
8. **job 级隔离与 checkpoint**

不是“互联网上所有指纹名词都塞进 JSON”。

---

## 2. 终态架构

```text
┌─────────────────────────────────────────────────────────────┐
│ Python Dashboard / TasksService                             │
│  - 创建任务、资源 lease、邮箱 OTP 拉取、账号落库              │
│  - BridgeManager：本机 CONNECT 桥 + capability/generation    │
│  - 不执行 OpenAI 注册协议体                                  │
└───────────────────────────┬─────────────────────────────────┘
                            │ V2 HTTP (loopback)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Go email-protocol-worker（唯一协议执行面）                   │
│                                                             │
│  Admission(≤100) ──► JobRuntime                             │
│       │                 ├─ FingerprintBundle (durable)      │
│       │                 ├─ TransportProfile  (locked)       │
│       │                 ├─ tls-client HttpClient (1/job)    │
│       │                 ├─ CookieJar + deviceID(oai-did)    │
│       │                 ├─ HeaderPreset engine              │
│       │                 ├─ FSM S0–S14 + dispatcher          │
│       │                 ├─ Sentinel (PoW + Goja realm)      │
│       │                 └─ Checkpoint / cancel / reconcile  │
│       │                                                     │
│       └─ 出网唯一路径: http://127.0.0.1:<bridge> + Bearer   │
└─────────────────────────────────────────────────────────────┘
                            │ CONNECT only
                            ▼
                   上游代理（sticky exit IP）
                            │
                            ▼
                      OpenAI / ChatGPT
```

### 2.1 所有权边界

| 组件 | 负责 | 禁止 |
|---|---|---|
| Python | 任务、lease、邮箱、bridge grant、落库 | 跑注册 FSM、改指纹、直连 OpenAI |
| Go | 协议、指纹、TLS、Sentinel、session document | 选代理、租邮箱、写业务 DB 账号主库 |
| Bridge | 本机 CONNECT 认证与上游隧道 | 改请求头、注入 cookie |

### 2.2 一 job 一世界（硬不变量）

```text
1 JobRuntime =
  1 FingerprintBundle
+ 1 TransportProfile binding
+ 1 tls-client instance
+ 1 CookieJar
+ 1 server oai-did (deviceID, from wire)
+ 1 BridgeGrant snapshot (url+capability+generation)
+ 1 Sentinel realm factory（每次 challenge 新 realm 或等价隔离）
+ 1 FSM cursor + checkpoint
```

跨 job 共享禁止：client、jar、profile 指针、dispatcher、global UA、global proxy、Sentinel browser/page。

---

## 3. FingerprintBundle v2（全量设备画像）

> 相对 `EMAIL_PROTOCOL_GO_PLAN` §8 的 v1：**字段并集扩全 + 一致性引擎 + 生成/校验/序列化一体**。  
> Go 包：`go-email-protocol/internal/fingerprint`

### 3.1 顶层结构

```json
{
  "version": 2,
  "bundle_id": "fpb_...",
  "created_at": "RFC3339",
  "source": "generated|granted|checkpoint",
  "catalog_id": "chrome-windows-desktop-v2",
  "transport_profile_id": "chrome-142-win-h2-v1",
  "identity": { },
  "device": { },
  "locale": { },
  "geometry": { },
  "navigator": { },
  "client_hints": { },
  "header_identity": { },
  "sentinel_env": { },
  "noise": { },
  "proxy_affinity": { },
  "consistency": {
    "locked": true,
    "hash": "sha256:..."
  }
}
```

`consistency.hash` = 规范化 JSON（除 `created_at` 外）的 digest；checkpoint 与请求前必须复算一致。

### 3.2 identity

| 字段 | 类型 | 规则 | 依据 |
|---|---|---|---|
| `profile_uuid` | string UUID | job 内不变；**不是** server oai-did | Node `DeviceProfile.id` |
| `device_id_seed` | string | 可选；仅本地日志归因 | 本项目 |
| `family` | `desktop` \| `mobile` | 决定合法 catalog | Node |
| `browser` | `chrome` \| `edge` | mobile 仅 chrome | Node |
| `os` | `windows` \| `android` | 与 family 绑定 | Node |
| `os_version` | string | Windows 可用 `10.0`；Android `12.0.0`–`15.0.0` | Node |
| `impersonate_label` | string | 与 TransportProfile 对齐，如 `chrome_142` | Python curl_cffi 语义 |

### 3.3 device / UA

| 字段 | 规则 |
|---|---|
| `user_agent` | **完整四段版本** `Chrome/M.0.build.patch`；禁止 `M.0.0.0` 糊弄（除非 fixture 明确要求） |
| `ua_major` | 从 UA 解析，整数 |
| `ua_full_version` | `M.0.build.patch` 字符串 |
| `edge_version` | browser=edge 时必填，与 UA `Edg/` 一致 |
| `android_model` | mobile 时必填（Pixel/SM/… catalog） |

生成时 **同一 RNG 一次写完** UA 与版本字段，禁止后改 UA 不改 hints。

### 3.4 locale（整组原子）

| 字段 | 说明 |
|---|---|
| `locale` | 如 `en-US` |
| `languages` | `["en-US","en"]` |
| `accept_language` | wire 头 |
| `timezone_id` | IANA，如 `Asia/Tokyo` |

**禁止** 随机拼 locale + 不相关 timezone。  
必须从 **LocaleTuple catalog** 整组抽取。

### 3.5 geometry（整组原子）

| 字段 | desktop | mobile |
|---|---|---|
| `viewport_width/height` | catalog | catalog |
| `screen_width/height` | catalog | = viewport 或 catalog |
| `outer_width/height` | viewport + chrome offset | = viewport |
| `device_scale_factor` | 1 / 1.25 / 1.5… | 2.6–3 |
| `color_depth` | 24 | 24 |
| `pixel_depth` | 24 | 24 |

Catalog 至少覆盖 Node 现网集合：

- Desktop VP：1365×768、1440×900、1536×864、1600×900、1710×1067、1920×1080  
- Mobile VP：360×800、390×844、393×873、412×915、430×932  
- 比例：desktop ≈ 68%，mobile ≈ 32%（可配置，默认沿用）

### 3.6 navigator

| 字段 | 规则 |
|---|---|
| `hardware_concurrency` | desktop: 4/8/12/16；mobile: 4/6/8 |
| `device_memory` | desktop: 4/8/16；mobile: 4/6/8 |
| `js_heap_size_limit` | catalog 有限集 |
| `platform` | desktop `Win32`；mobile `Linux armv8l` |
| `vendor` | `Google Inc.` |
| `max_touch_points` | desktop 0；mobile 5/10 |
| `has_touch` | 与 family 一致 |
| `is_mobile` | 与 family 一致 |

### 3.7 client_hints（派生缓存，全量）

由 bundle **一次派生并冻结**（不得每次请求重算漂移）：

| 字段 | wire 头 | 来源 |
|---|---|---|
| `sec_ch_ua` | `sec-ch-ua` | brands major |
| `sec_ch_ua_full_version_list` | `sec-ch-ua-full-version-list` | brands full |
| `sec_ch_ua_mobile` | `sec-ch-ua-mobile` | `?0`/`?1` |
| `sec_ch_ua_platform` | `sec-ch-ua-platform` | `"Windows"` / `"Android"` |
| `sec_ch_ua_platform_version` | `sec-ch-ua-platform-version` | Win 首版保留 Node 行为 `"15.0.0"`；Android = os_version |
| `sec_ch_viewport_width` | `sec-ch-viewport-width` | `"${viewport_width}"` |
| `sec_ch_ua_full_version` | `sec-ch-ua-full-version` | **v2 新增** 单值 full |
| `sec_ch_ua_arch` | `sec-ch-ua-arch` | desktop `"x86"`；mobile `"arm"`（catalog） |
| `sec_ch_ua_bitness` | `sec-ch-ua-bitness` | desktop `"64"`；mobile `"64"` 或 catalog |
| `sec_ch_ua_model` | `sec-ch-ua-model` | mobile 填 model；desktop `""` |

Brand 规则：

- Chrome：`Google Chrome` + `Chromium` + `Not.A/Brand`  
- Edge：`Microsoft Edge` + `Chromium` + `Not.A/Brand`  
- major/full **全部**从同一 UA 解析

> 某 endpoint fixture 若证明不发送高阶 CH，由 **HeaderPreset** 决定是否 omit，而不是删掉 Bundle 字段。

### 3.8 header_identity（默认身份头）

| 字段 | 用途 |
|---|---|
| `user_agent` | 同 device.user_agent |
| `accept_language` | 同 locale |
| `accept_encoding_default` | 如 `gzip, deflate, br`；`zstd` 仅当 decoder+fixture 全过 |
| `priority_default_fetch` | 如 `u=1, i`（以抓包为准） |

### 3.9 sentinel_env（29 字段，从 Bundle 投影）

与计划 §9.1 对齐，**全部从 Bundle 构造**，不得另起炉灶：

- identity/locale：userAgent, language, languages, locale, timezoneId  
- geometry：screen/inner/outer/DPR  
- navigator capability：hardwareConcurrency…isMobile  
- screen：colorDepth, pixelDepth  
- per-call：timeOrigin  
- release-sticky：scriptSources, buildHash, documentKeys, windowKeys, searchParamKeys  

### 3.10 noise（可选元数据，默认可关）

用于日志归因 / 未来 realm 扩展，**默认不进入 OpenAI 业务头**：

| 字段 | 说明 |
|---|---|
| `gpu_vendor` / `gpu_model` | 有限池（Python stage 同源思路） |
| `canvas_hash` | 稳定伪值（非真 canvas） |
| `math_fingerprint` | 可选 |
| `enabled` | false 时整段忽略 |

### 3.11 proxy_affinity

| 字段 | 说明 |
|---|---|
| `expected_country` | 来自 resource grant |
| `exit_ip` | grant 快照 |
| `timezone_policy` | `strict_match` \| `allow_global_en` \| `catalog_only` |
| `locale_policy` | 同上 |

**strict_match：** 出口国与 timezone/locale catalog 必须同区（JP→Asia/Tokyo 类）。  
不匹配 → admission/start fail-closed，不发注册请求。

### 3.12 一致性引擎（必须实现）

启动 job、每次发请求前、checkpoint 恢复后：

```text
CHECK:
  family ↔ browser ↔ os ↔ is_mobile ↔ has_touch ↔ max_touch_points
  ua_major == client_hints majors == transport_profile browser major
  platform ↔ os
  viewport ∈ geometry tuple
  sec_ch_ua_mobile == is_mobile
  sec_ch_viewport_width == viewport_width
  transport_profile_id 已锁定且 module graph hash 匹配
  proxy_affinity 策略通过
  consistency.hash 匹配
ELSE:
  fail closed (failure_code=fingerprint_inconsistent)
```

---

## 4. TransportProfile v2（全量传输画像）

> Go 包：`internal/transport`  
> 库：`github.com/bogdanfinn/tls-client`（锁定版本见 §11）  
> 低层补洞：`refraction-networking/utls` 仅当 tls-client 无法表达 fixture

### 4.1 必须捕获并对拍的维度

| 维度 | 内容 |
|---|---|
| TLS ClientHello | cipher suites、extension 集合与顺序、关键、sigalgs、ALPN、versions、key share 形态 |
| ALPN | `h2`, `http/1.1` |
| HTTP/2 | SETTINGS 键值与顺序、WINDOW_UPDATE、PRIORITY 行为、伪头顺序 |
| Header order | 按 endpoint class 的 key 顺序（fhttp HeaderOrderKey） |
| 压缩 | 声明的 accept-encoding 与真实解码能力一致 |
| 证书校验 | **必须 true**（禁止抄 Node DEFAULT_INSECURE_TLS） |
| CONNECT | Bearer capability 仅出现在 CONNECT；origin 侧不可见 |

### 4.2 Profile 文档结构

```json
{
  "id": "chrome-142-win-h2-v1",
  "browser": "chrome",
  "browser_major": 142,
  "os": "windows",
  "go_version": "go1.22.12",
  "module_graph_fixture": "sha256:...",
  "tls_client_hello_fixture": "sha256:...",
  "tls_extension_order_fixture": "sha256:...",
  "alpn": ["h2", "http/1.1"],
  "http2_settings_fixture": "sha256:...",
  "http2_connection_flow_fixture": "sha256:...",
  "http2_pseudo_header_order": [":method", ":authority", ":scheme", ":path"],
  "header_presets": {
    "document_navigation": "sha256:...",
    "same_origin_fetch": "sha256:...",
    "cross_origin_oauth": "sha256:...",
    "otp_sparse": "sha256:...",
    "sentinel_req": "sha256:..."
  },
  "response_content_encoding_fixture": "sha256:...",
  "redirect_max_hops": 10,
  "certificate_validation": true,
  "bridge_required": true,
  "status": "active|capture_required|retired"
}
```

### 4.3 对拍协议（发布闸门，不可省略）

1. **基线捕获**：同 browser/major/OS 的受控抓包（或可信基线库）→ 规范化字段（剔除 client_random/session_id/公钥材料）。  
2. **无代理 echo**：Go client 打受控 TLS/H2 echo → 比 ClientHello/H2。  
3. **最小 diff 修 profile**：只补 fixture 指出的缺/多/错序；记录插入位置。  
4. **带 bridge 再测**：CONNECT 带 capability；origin 捕获证明 capability 未泄漏。  
5. **业务头 preset**：每个 endpoint class 独立 fixture，不是 map 乱序。  
6. **任一 drift** → 新 profile id，旧 id retire；禁止原地覆盖。

### 4.4 Per-job client 构造

```text
NewJobClient(bundle, transportProfile, bridgeGrant) -> HttpClient
  - WithClientProfile(transportProfile)
  - SetProxy(bridgeGrant.URL)  // 仅 127.0.0.1
  - WithConnectHeaders(Proxy-Authorization: Bearer <capability>)
  - CookieJar = job jar
  - Timeouts / MaxConnsPerHost=2 / MaxIdleConnsPerHost=1
  - Certificate validation = true
  - 禁止 fallback net/http 默认 transport
```

终态 / cancel：`CloseIdleConnections()` + 丢弃 client。

---

## 5. HeaderPreset 全量矩阵

> 计划已强调：**profile 名称 ≠ 浏览器 HTTP 请求**。  
> 所有 OpenAI 请求头由本项目显式组装。

### 5.1 Preset 类

| preset_id | 用途 | 典型点 |
|---|---|---|
| `document_navigation` | GET HTML / 导航 | accept html 链、upgrade-insecure-requests、sec-fetch-user=?1 |
| `same_origin_fetch` | auth.openai.com XHR | accept json、origin/referer、sec-fetch-* cors |
| `cross_origin_oauth` | chatgpt.com ↔ auth 跨站 | sec-fetch-site cross-site 等以抓包为准 |
| `otp_sparse` | email-otp send/validate 等 | **故意少字段**也要精确复刻，禁止“补全更像浏览器” |
| `sentinel_req` | sentinel.openai.com | UA + 必要头；按 fixture |
| `callback_navigation` | OAuth callback GET | document 类 |

### 5.2 身份头注入规则

每个 preset 声明：

- **required identity keys**（UA、accept-language、哪些 CH）  
- **forbidden keys**（该步不得出现）  
- **key order**（完整有序列表）  
- **value overrides**（endpoint 特有）

引擎：

```text
headers = preset.base_ordered
merge identity from Bundle (only allowed keys)
apply endpoint overrides
assert no forbidden keys
assert order == fixture order
```

### 5.3 Python 包应并入的行为（全量吸收）

| 行为 | 来源 | Go 落地 |
|---|---|---|
| impersonate major ↔ UA major | codex_oauth / curl_cffi | TransportProfile.browser_major 绑定 |
| HTML vs auth fetch 两套头 | `_build_http_html_headers` / `_build_http_auth_fetch_headers` | preset 分裂 |
| Datadog RUM 头 | `_build_http_datadog_trace_headers` | **可选** `telemetry.datadog_rum=true`；默认关；开启则每请求新 trace，禁止常量 trace |
| Sentinel SO-Token | Python sentinel headers | 有则带，无则 omit |
| storage_state 终态 | Python finalize | Go 返回 session document；Python 落盘 |

### 5.4 Node 应并入的行为

| 行为 | 来源 | Go 落地 |
|---|---|---|
| 完整 DeviceProfile | device-profile.ts | Bundle v2 |
| createBrowserHeaders CH 集合 | openai.ts | client_hints + preset |
| 注册 FSM 与 OTP 少字段例外 | openai.ts | FSM + otp_sparse |
| CookieJar per client | openai.ts | per-job jar |
| fetch retry 边界 | openai.ts | 仅幂等 stage 有限重试 |

---

## 6. 会话、Cookie、oai-did

| 概念 | 规则 |
|---|---|
| CookieJar | 每 job 独立；持久化进 checkpoint（加密字段策略与 cryptostore 一致） |
| server `oai-did` | **只从 wire Set-Cookie 读取**；赋值 `deviceID` |
| `profile_uuid` | 本地画像 ID；**不得**假装成 server oai-did |
| CSRF | cookie `__Host-next-auth.csrf-token` 或 JSON csrfToken |
| PKCE/state | 若走 OAuth 支路，job 内 sticky |
| redirect | 默认 follow；manual walker ≤10 hops |
| 成功定义 | access_token + schema-valid session document；不是“到了 callback” |

---

## 7. 注册 FSM（纯 Go 全量）

主链 endpoint 与 S0–S14 **以 `EMAIL_PROTOCOL_GO_PLAN.md` §6 为权威**，本方案要求：

| 要求 | 说明 |
|---|---|
| 全状态实现 | S0–S14 + continuation dispatcher 全分支 |
| 每状态绑定 preset | 表驱动：state → method/url builder/preset/sentinel flow |
| 每状态字段矩阵 | query/body/header **精确**；fixture 锁定 |
| ambiguous 语义 | S7/S10/S11/callback 发送后响应丢失 → `ambiguous_after_send` / `reconcile_required`，禁止盲重放 |
| phone 支路 | 非主链；需要时另开 FSM 表，不得污染 email 主链 preset |
| 无 Node 子进程 | `mailat_runner` 仅过渡；终态删除 |

状态机代码位置（目标）：

```text
internal/protocol/fsm.go          # cursor + transitions
internal/protocol/states.go       # S0–S14 handlers
internal/protocol/dispatcher.go   # continue_url 路由
internal/protocol/shapes.go       # request/response types
internal/protocol/fixtures/       # s0–s15,c*,l*,t*
```

---

## 8. Sentinel 全量

### 8.1 能力矩阵

| 能力 | 必须 | 实现 |
|---|---|---|
| requirements POST | 是 | Go HTTP + Bundle UA |
| 25 项 fingerprint 数组顺序 | 是 | 与 TS `collectFingerprintData` 字节级 fixture |
| PoW `gAAAAAB` / requirements `gAAAAAC` | 是 | 纯 Go，最多 500_000 attempts，可取消 |
| 每 action 新 token | 是 | 不缓存跨 flow |
| Turnstile `dx` | 是（当 required） | Goja 隔离 realm + SDK pin；失败 `protocol_incompatible` |
| oai-did cookie on sentinel domain | 是 | job jar 写入规则与 TS 一致 |
| 共享 browser/page | **禁止** | 无全局 Playwright |

### 8.2 Realm 注入（全量从 Bundle）

注入 window/navigator/screen/document 的值 **全部**来自 FingerprintBundle，包括但不限于：

UA、language(s)、hardwareConcurrency、deviceMemory、platform、vendor、maxTouchPoints、  
screen/inner/outer、DPR、colorDepth、timezone（Intl / offset 行为按 fixture）、  
mobile/touch 语义。

SDK 源码：hash pin + 版本目录；drift → 拒发版。

### 8.3 窄逃生口（非默认）

仅当连续 canary 证明纯 Go realm 无法通过某一 class 挑战时：

- 允许 **可选** `sentinel_escape=browser_once` 配置  
- 每 job 独立 context，用完即毁  
- **默认关闭**；开启需单独成功率与资源报表  
- 不得成为 100 并发默认路径

---

## 9. 代理、Bridge、sticky IP

| 规则 | 内容 |
|---|---|
| 唯一出网 | `BridgeGrant.url`（loopback HTTP CONNECT） |
| 认证 | CONNECT `Proxy-Authorization: Bearer <capability>` |
| 禁止 | 上游账密进 Go 日志/checkpoint 明文；direct；SOCKS4 无 fixture |
| sticky | 同一 job 生命周期固定 exit_ip / proxy_key |
| 亲和 | Bundle.proxy_affinity 与 grant.expected_country 校验 |
| 生命周期 | Python 持有 bridge；Go cancel/terminal 后 capability retire；丢失 ACK → reconcile |
| 并发 | 同 proxy_key / 同 mailbox 互斥策略按 admission（MaxPerProxy/MaxPerMailbox） |

---

## 10. 100 并发与隔离（全量要求）

并入 `TRUE_100` 与 Go 计划，终态必须同时满足：

| ID | 要求 |
|---|---|
| C1 | `running ≤ 100` 硬顶，队列有界 |
| C2 | 同 proxy_key / email_key / domain cap 不超额双占 |
| C3 | 100 OTP 同时等待无串码、无重复 submit |
| C4 | jar/profile/oai-did/sentinel/continuation 零串扰 |
| C5 | 资源不足 → admission backpressure，不复用、不直连 |
| C6 | cancel 后无在途请求、无泄漏 goroutine/连接 |
| C7 | 重启后 nonterminal job 按 fingerprint+lease_fence 恢复或 reconcile |

---

## 11. 工具链与依赖锁定

| 项 | 锁定策略 |
|---|---|
| Go | 1.22.12（升级必须整包重对拍） |
| tls-client | 已验证组合优先：`v1.9.1` + fhttp `v0.5.34` + utls `v1.6.5` |
| 更高 tls-client | 需 Go≥其 go.mod 要求 + 全量 fixture 重生 |
| goja | Sentinel SDK realm |
| sqlite | ledger（modernc 或已有选择） |
| GOTOOLCHAIN | `local`，防静默换编译器 |

`TransportProfile` 绑定：`go_version + module_graph_hash + fixture_hashes`。

---

## 12. 代码落点（终态包图）

```text
go-email-protocol/
  cmd/email-protocol-worker/
  internal/
    admission/          # 100 slot, proxy/mailbox caps
    api/                # V2 HTTP
    job/                # Runtime, checkpoint, cancel
    ledger/             # durable job state
    fingerprint/        # Bundle v2, catalog, consistency
    transport/          # Profile, client factory, bridge dial
    headerpreset/       # ordered presets + assert
    session/            # jar, oai-did, csrf helpers
    protocol/           # FSM S0–S14
    sentinel/           # req, pow, realm, dx
    cryptostore/        # secrets at rest
    fixture/            # catalogue loader + redact
    plusverify/         # 可选后续
    telemetry/          # metrics, redaction
  testdata/
    fingerprint-catalog/
    transport-fixtures/
    protocol-fixtures/
    sentinel-fixtures/
    header-presets/
  tools/
    fixture-recorder/
    tls-echo-compare/
    header-order-dump/
```

**删除/退役（终态）：**

- 生产路径对 Node `tsx openai.ts` 的依赖  
- Go 内 `mailat_runner` 真执行（可留 oracle 测试 build tag）  
- 任何 global proxy dispatcher  

---

## 13. V2 API 与资源授予（摘要）

权威细节见 `EMAIL_PROTOCOL_GO_PLAN.md` §5。终态强制：

创建体携带：

- `profile`: FingerprintBundle v2（或 `profile_catalog_id` + 由 Go 生成后回写 checkpoint）  
- `resource_grant.bridge` 全字段  
- `request_fingerprint` 幂等  
- `expected_country` / `exit_ip`  

同 fingerprint 重放同 job；fingerprint 变 → 409。  
成功响应：token + session document；**不**返回 daemon 本地路径当成功依据。

---

## 14. 数据流（单 job 全量）

```text
1. Python lease 邮箱+代理，创建 bridge，生成/装载 Bundle（或委托 Go 生成）
2. POST /v2/email-register  (idempotency + grant + bundle)
3. Go admission
4. Go lock consistency(bundle, transport, grant)
5. NewJobClient
6. FSM:
     S0 context
     S1 GET chatgpt.com → oai-did
     S2 csrf
     S3 signin/openai
     S4 follow
     S5 sentinel authorize_continue
     S6 authorize/continue
     S7 register + sentinel username_password_create
     S8 email-otp/send
     S9 waiting_for_otp  ← Python 投递 code
     S10 validate
     S11 create_account + sentinel oauth_create_account
     S12 callback
     S13 session
     S14 succeed document
7. Python 落库 / 释放 lease / retire bridge
```

任一步一致性失败、bridge 失效、ambiguous 发送 → 进入文档化失败码，不编造成功。

---

## 15. 失败码（指纹/传输相关摘录）

| code | 含义 |
|---|---|
| `fingerprint_inconsistent` | Bundle 自洽失败 |
| `transport_profile_mismatch` | UA/CH 与 TLS profile 不一致 |
| `transport_fixture_drift` | 对拍失败/版本漂移 |
| `bridge_required` | 无有效 grant |
| `bridge_capability_rejected` | CONNECT 被拒 |
| `proxy_affinity_mismatch` | 出口国与 locale 策略冲突 |
| `oai_did_missing` | 未拿到 server device id |
| `protocol_incompatible` | Sentinel SDK/协议漂移 |
| `sentinel_pow_exhausted` | PoW 超限 |
| `ambiguous_after_send` | 可能已注册，禁重放 |
| `admission_rejected` | 100 满或资源 cap |

---

## 16. 测试与验收（全量清单）

### 16.1 指纹

- [ ] Bundle v2 所有字段可生成、可序列化、可 checkpoint  
- [ ] 一致性引擎单测覆盖：mobile/desktop、edge/chrome、故意打脏 UA/CH  
- [ ] Client Hints 与 UA 解析黄金向量  
- [ ] proxy_affinity strict/allow 策略单测  

### 16.2 传输

- [ ] 无代理 TLS echo 对拍通过（锁定 profile）  
- [ ] HTTP/2 settings/伪头 fixture 通过  
- [ ] header order 每 preset 黄金文件  
- [ ] bridge CONNECT 带 Bearer；origin 无 capability  
- [ ] 证书校验开启；错误证书失败  

### 16.3 协议

- [ ] S0–S14 + dispatcher 全分支 fixture  
- [ ] OTP wrong/expired/replay  
- [ ] ambiguous stage 不重放  
- [ ] 成功定义含 session schema  

### 16.4 Sentinel

- [ ] 25 项顺序与编码 fixture  
- [ ] PoW 可取消、可耗尽  
- [ ] realm 隔离：两 job 并行无串 navigator  
- [ ] SDK hash pin drift → protocol_incompatible  

### 16.5 并发

- [ ] 100 barrier：running≤100  
- [ ] 同 proxy/email 不双占  
- [ ] 100 OTP 等待无串扰  
- [ ] cancel/restart/reconcile  

### 16.6 切流

- [ ] canary：Go vs Node 成功率/验证码率/耗时  
- [ ] 默认切 Go 后 Node 仅 oracle  
- [ ] 文档与配置移除 Node 主路径  

---

## 17. 配置面（终态）

```yaml
email_protocol:
  backend: go                    # 终态唯一：go
  worker_url: http://127.0.0.1:18765
  max_active: 100
  fingerprint:
    bundle_version: 2
    desktop_ratio: 0.68
    timezone_policy: strict_match   # 或 allow_global_en
    noise_enabled: false
    datadog_rum: false
  transport:
    profile_id: chrome-142-win-h2-v1
    require_bridge: true
    certificate_validation: true
  sentinel:
    pow_max_attempts: 500000
    escape: off                   # off | browser_once
  admission:
    max_per_proxy: 1
    max_per_mailbox: 1
```

---

## 18. 与现有文档关系

| 文档 | 关系 |
|---|---|
| `EMAIL_PROTOCOL_GO_PLAN.md` | FSM、V2 API、bridge、Sentinel 算法权威；本方案 **吸收并冻结为纯 Go 全量终态** |
| `TRUE_100_CONCURRENCY_FINGERPRINT_PLAN.md` | 并发/隔离/OTP/ledger；本方案 **默认全部生效** |
| 本文件 | **纯 Go + 全量指纹的单一终态规格**；冲突时以“更严、更全量、fail-closed”为准 |

### 18.1 对历史摇摆的终态裁决

| 议题 | 裁决 |
|---|---|
| Node 池 vs 纯 Go | **纯 Go** |
| 是否默认浏览器 | **否** |
| 指纹是否全量 | **是（本文件 §3–§5 定义的全量）** |
| SQLite | 短期可；不阻塞纯 Go 协议；账号库迁移另案 |
| 动态 IP | sticky per job；亲和策略见 Bundle.proxy_affinity |

---

## 19. 实施现实说明（不降范围）

| 问题 | 答案 |
|---|---|
| 能不能全量？ | **能**——按本文件字段与对拍定义 |
| 能不能纯 Go？ | **能**——控制面已有 G1 骨架；G2 指纹包已落地；缺 TLS/HeaderPreset/FSM/Sentinel 真实现与切流 |
| 会不会麻烦？ | 会；**麻烦是本方案的一部分**，不删减字段来省事 |
| 100% 永不可检测？ | 不承诺；承诺的是完整模拟面 + 锁定对拍 + 隔离 |
| 现在仓库已有什么？（2026-07-18） | G0 fixtures；G1 daemon/ledger/admission/API/synthetic runner；**`internal/fingerprint` Bundle v2 生成/Freeze/一致性/单测**；`internal/transport` Profile 扩展 + bridge 校验 + OptionsFactory |
| 现在仓库还缺什么？ | Create 路径强制 Bundle 校验；HeaderPreset 有序引擎；tls-client 真 client；S0–S14 真请求；Sentinel 真引擎；Python 生产切流与 canary |

---

## 20. 完成定义（Done）

同时满足才算“纯 Go 全量指纹上线”：

1. 生产注册默认只打 Go worker，无 Node 协议子进程。  
2. 每个成功账号可追溯：`bundle_id` + `transport_profile_id` + `exit_ip` + `job_id`。  
3. §16 全部清单勾选（与 §23 门禁一致）。  
4. 100 并发压测报告存档。  
5. TLS/H2/header fixture 与 go.mod 版本绑定入库。  
6. 一致性引擎在 CI 必跑。  
7. 本文件与 `EMAIL_PROTOCOL_GO_PLAN.md` 无未裁决冲突（Bundle 以本文件 v2 为准）。

---

## 21. 附录 A — Bundle v2 字段总表（速查）

```
version, bundle_id, created_at, source, catalog_id, transport_profile_id

identity.profile_uuid
identity.family
identity.browser
identity.os
identity.os_version
identity.impersonate_label

device.user_agent
device.ua_major
device.ua_full_version
device.edge_version?
device.android_model?

locale.locale
locale.languages[]
locale.accept_language
locale.timezone_id

geometry.viewport_width
geometry.viewport_height
geometry.screen_width
geometry.screen_height
geometry.outer_width
geometry.outer_height
geometry.device_scale_factor
geometry.color_depth
geometry.pixel_depth

navigator.hardware_concurrency
navigator.device_memory
navigator.js_heap_size_limit
navigator.platform
navigator.vendor
navigator.max_touch_points
navigator.has_touch
navigator.is_mobile

client_hints.sec_ch_ua
client_hints.sec_ch_ua_full_version_list
client_hints.sec_ch_ua_mobile
client_hints.sec_ch_ua_platform
client_hints.sec_ch_ua_platform_version
client_hints.sec_ch_viewport_width
client_hints.sec_ch_ua_full_version
client_hints.sec_ch_ua_arch
client_hints.sec_ch_ua_bitness
client_hints.sec_ch_ua_model

header_identity.user_agent
header_identity.accept_language
header_identity.accept_encoding_default
header_identity.priority_default_fetch

sentinel_env.* (29 keys projection)

noise.gpu_vendor?
noise.gpu_model?
noise.canvas_hash?
noise.math_fingerprint?
noise.enabled

proxy_affinity.expected_country
proxy_affinity.exit_ip
proxy_affinity.timezone_policy
proxy_affinity.locale_policy

consistency.locked
consistency.hash
```

## 22. 附录 B — 源码依据索引

- Node 画像：`E:/project/mailat/mailat/codex_register/src/device-profile.ts`  
- Node 协议/头：`.../openai.ts`（createBrowserHeaders、注册链、OTP）  
- Node Sentinel：`.../sentinel.ts`、`sentinel-browser.ts`  
- Python 传输/头：`_compare/iCloudICloud/X9-Free/_credential_toolcore/codex_oauth.py`  
- Python stage 画像：`.../http_stage_features.py`  
- Go 骨架：`go-email-protocol/`  
- 既有蓝图：`docs/EMAIL_PROTOCOL_GO_PLAN.md`、`docs/TRUE_100_CONCURRENCY_FINGERPRINT_PLAN.md`  
- tls-client：`github.com/bogdanfinn/tls-client`  
- 对拍方法论参考：`E:/Download/go_tls_fingerprint_align_notes.md`（方法论可用；Firefox/PIX 业务字段禁止照搬）

---

**终态口号：**  
**一个 Go daemon，一百个隔离世界；一份全量 Bundle，一条真 TLS；能对拍才上线，不一致就失败。**

---

## 23. 无人值守实施路线（Autonomous Execution）

> **用途：** 给编码 agent / 人机接力用。按阶段顺序推进，**不得跳过门禁**，**不得破坏生产默认路径**，直到 §20 Done。  
> **主文档：** 本文件。FSM/API 细节查 `EMAIL_PROTOCOL_GO_PLAN.md`；并发隔离查 `TRUE_100_CONCURRENCY_FINGERPRINT_PLAN.md`。  
> **冲突裁决：** 更严、更全量、fail-closed；Bundle schema **以本文件 v2 为准**（旧文 v1 仅兼容投影）。

### 23.1 总原则（硬）

1. **正确性 > 速度 > 花活。** 不编造 OpenAI 字段；无 fixture/源码依据不写死 header/body。  
2. **fail-closed。** 指纹不一致、无 bridge、证书失败、ambiguous 发送 → 拒绝/失败码，不“差不多继续”。  
3. **一 job 一世界。** 禁止跨 job 共享 client / jar / profile 指针 / dispatcher / UA / proxy / Sentinel realm。  
4. **出网只走 loopback bridge + capability。** 禁止 direct connect、禁止把 capability 打到 origin。  
5. **生产默认路径保护。** 在 **Phase G canary 绿之前**，禁止把 Python 生产默认 `email_protocol.backend` 切到 `go`；禁止删除 Node/mailat 协议路径。  
6. **每阶段结束必须：** `cd go-email-protocol && GOTOOLCHAIN=local go test ./... -count=1` 全绿；再勾 §23.6。  
7. **禁止破坏性动作（未到对应阶段）：**  
   - 删/改生产 Node 入口、`mailat` 真跑路径  
   - 盲目 `go get` 升大版本不锁 fixture  
   - 改 Python `max_parallel_tasks` / register bucket 到 100（属 Phase G 配套，且需用户环境确认）  
   - 提交密钥、真实 cookie、真实 token 进仓库  
   - 用 Firefox/PIX 业务 profile 冒充本项目 Chrome 基线  
8. **允许的安全动作：** 新增包/测试/fixture、扩展校验、合成 runner 旁路加真路径 feature flag、文档勾选、README 状态。  
9. **停机条件（必须停并报告，不得硬闯）：**  
   - 需要真实 OpenAI 网络/账号才能继续且本地无 fixture  
   - tls-client 某 wire 字段无法表达且需架构分叉  
   - 与用户生产配置强耦合且会改默认行为  
   - 测试持续红且根因不明超过该阶段合理修复范围  
10. **进度只写本文件 §23.6 与 `go-email-protocol/README.md` Phases 表**；不另起互相打架的 STATUS.md。

### 23.2 阶段总览

| Phase | 名称 | 目标 | 破坏风险 | 依赖 |
|---|---|---|---|---|
| **A** | FingerprintBundle v2 | 生成/冻结/一致性/单测 | 低 | 无 |
| **B** | Create 强制校验 + 画像绑定 | job.Create 只接受合法 Bundle；profile_id=bundle_id | 低 | A |
| **C** | HeaderPreset 引擎 | 有序 header 模板 + 黄金测试（无真网） | 低 | A |
| **D** | tls-client 真 Transport | per-job client + bridge CONNECT + 证书校验；feature flag | 中（新依赖） | A,B |
| **E** | FSM 真请求 S0–S14 | 替换 synthetic 主路径（可 build tag / flag 双轨） | 中 | C,D |
| **F** | Sentinel 真引擎 | requirements/PoW/goja realm；fail-closed pin | 中 | E 部分可并行骨架 |
| **G** | Python 接入与 canary | 完整 Bundle 下发；小流量对照 Node；**再**默认切 Go | **高（默认路径）** | E+F 门禁绿 |
| **H** | 100 并发与退役 | 压测、oracle 化 Node、文档收尾 | 中 | G |

**顺序规则：** A→B→C 可紧接；D 在 B 后；E 需要 C+D；F 可在 E 骨架后并行补全，但 **生产切流必须 E+F 都绿**；G/H 最后。

### 23.3 阶段详单

#### Phase A — FingerprintBundle v2（已完成基线）

**路径：** `go-email-protocol/internal/fingerprint/`

| 项 | 要求 |
|---|---|
| 类型 | Bundle v2 全字段（§3 + 附录 A） |
| API | `Generate` / `Freeze` / `Validate` / `AssertReady` / `ParseJSON` / `IdentityHeaders` / `ToV1` |
| CH | 含 full-version-list、platform-version、viewport-width、arch、bitness、model |
| 一致性 | family↔os↔touch；UA major↔CH↔transport_profile_id；proxy_affinity |
| 测试 | desktop/mobile、strict JP、脏 UA、hash 篡改、JSON round-trip、Edge brand |
| 禁止 | 懒版本 `M.0.0.0` 冒充真 build；跨 job 复用同一 Bundle 指针当可变状态 |

**门禁：** `go test ./internal/fingerprint/ -count=1` 绿。

#### Phase B — Create 路径绑定（下一步默认起点）

**路径：** `internal/job/runtime.go`、`types.go`、相关测试；必要时 `internal/api`

| 步骤 | 做什么 | 验收 |
|---|---|---|
| B1 | `validateCreate`：若 `profile` 非空，则 `fingerprint.ParseJSON` + `AssertReady`；失败 → `fingerprint_inconsistent` / 对应 Error.Code | 单测脏 profile 被拒 |
| B2 | 空 profile：G1 兼容——**仅当** runner 仍为 synthetic **且** 配置 `allow_empty_profile=true`（默认 **false 于新测试**；生产 worker 默认 false 需在切流前统一）。过渡期：缺省则 **服务端 Generate 桌面画像并写回 checkpoint**，日志标明 `source=server_generated` | 创建后 ledger 有完整 JSON |
| B3 | `profileIDFrom`：优先 `bundle_id`，其次 `identity.profile_uuid`，兼容旧 `id` | 幂等键行为不变 |
| B4 | `spawnRuntime` 持有解析后的 `*fingerprint.Bundle`（或只读 JSON + 缓存 headers），禁止再静默忽略 | 运行时 IdentityHeaders 可用 |
| B5 | 与 `resource_grant.expected_country` / exit_ip 对齐 `proxy_affinity`（请求体 country 覆盖或校验） | 冲突 → `proxy_affinity_mismatch` |

**门禁：** job 包单测 + `go test ./...`；**不改** Python 默认 backend。

#### Phase C — HeaderPreset 引擎

**路径：** 新建 `internal/headerpreset/`

| 步骤 | 做什么 |
|---|---|
| C1 | 定义 preset 名：`document_navigation` / `same_origin_fetch` / `cross_origin_oauth` / `otp_sparse` / `sentinel_req` |
| C2 | 从 Bundle.IdentityHeaders + 静态 Accept/sec-fetch 模板 **按固定顺序** 组 header |
| C3 | OTP 路径 **省略** 多余 CH（对齐 Node/Python 例外），单测锁顺序 |
| C4 | `testdata/header-presets/*.golden` 或 hash；顺序变则失败 |
| C5 | Datadog RUM 默认 **关**；开启则每请求新 trace id |

**依据：** Node `createBrowserHeaders`、Python `_build_http_*_headers`；无依据字段不写。  
**门禁：** headerpreset 单测绿；仍可不发真网。

#### Phase D — tls-client Transport

**路径：** `internal/transport/`，`go.mod` 锁定 `github.com/bogdanfinn/tls-client`

| 步骤 | 做什么 |
|---|---|
| D1 | 增加 `TlsClientFactory`（名可变）实现 `OptionsFactory`；`FakeFactory` 保留为默认测试 |
| D2 | `NewWithOptions`：校验 bridge loopback + capability；`SetProxy(bridgeURL)`；CONNECT Bearer |
| D3 | `certificate_validation=true`；禁止照搬 Node `INSECURE_TLS` |
| D4 | Profile 与 Bundle.ua_major 绑定；mismatch 创建失败 |
| D5 | 无代理时的 **受控 TLS echo** 对拍工具/测试（可 skip short）；fixture 入库 `testdata/transport-fixtures/` |
| D6 | worker 配置：`transport.factory=fake|tls`；**默认 fake 直到 E 门禁**，避免半吊子打生产 |

**门禁：** 单元测试 +（若有网络）echo 对拍；module 版本写入 Profile 元数据字段。  
**停机：** 无法表达的 ClientHello 字段 → 记 `transport_fixture_drift`，扩展 adapter，**不**引入第二套通用 client。

#### Phase E — FSM 真请求

**路径：** `internal/job/`、`internal/protocol/`、fixtures

| 步骤 | 做什么 |
|---|---|
| E1 | 保留 synthetic runner 作测试；新增 `protocol.Runner` 真状态机 |
| E2 | 按 `EMAIL_PROTOCOL_GO_PLAN` S0–S14 逐步：先 S0–S3 导航/csrf/signin，再 OTP，再 create_account |
| E3 | 每步：HeaderPreset + jar + 响应字段写入 checkpoint |
| E4 | ambiguous_after_send：POST 后不确定 → 停自动 replay |
| E5 | 成功：session document schema 校验；**不**用本地路径冒充成功 |
| E6 | 与现有 ledger 状态机对齐：waiting_for_otp / cancel / reconcile |

**门禁：** fixture 驱动单测优先；真网 canary 仅在本地显式 flag。  
**禁止：** 在 E 未绿时改 Python 默认走 Go 真注册。

#### Phase F — Sentinel

**路径：** `internal/sentinel/`

| 步骤 | 做什么 |
|---|---|
| F1 | requirements 解析 + PoW 循环（可取消、可耗尽） |
| F2 | goja realm：注入 Bundle 投影的 navigator/screen（§7） |
| F3 | SDK build/hash pin；未知 → `protocol_incompatible` |
| F4 | 25 项 payload 顺序 fixture |
| F5 | 两 job 并行 realm 无串扰测试 |

**门禁：** sentinel 单测 + 与 E 联调 fixture。

#### Phase G — Python 接入与 canary（高风险，逐步）

| 步骤 | 做什么 | 保护 |
|---|---|---|
| G1 | Python 下发 **完整 Bundle v2 JSON**（或委托 Go Generate 后回写） | 字段来自 `fingerprint.Generate` 或等价 |
| G2 | 配置 `email_protocol.backend: go|node` **默认仍 node/mailat** 直到 G4 | 不改 example 默认除非注释说明 |
| G3 | 小流量 canary：同批对照成功率/验证码率/耗时 | 报告落 `data/` 或 docs 附件，无密钥 |
| G4 | canary 达标后才改默认 backend=go | **需在进度表标注 canary 证据路径** |
| G5 | 控制面 100 并发：tasks_service 上限与资源供给 **单独变更**，附回归 | 不得在指纹未稳时只开大并发 |

#### Phase H — 压测与退役

- 100 barrier、同 proxy/email 不双占、OTP 不串扰  
- Node 仅 oracle build tag / 文档化应急开关  
- §16 全勾；§20 全满足  
- README Phases 全部 done  

### 23.4 每步工作流（agent 必遵）

```text
1. 读 §23.6 找第一个未完成 Phase 的第一个未勾步骤
2. 只做该步骤与其直接单测；不顺手改生产默认
3. go test ./... -count=1
4. 绿 → 勾选 §23.6 与 README；红 → 修到绿或触发停机条件
5. 进入下一步；大阶段结束写一行「证据：测试命令 + 关键文件」
```

### 23.5 失败码映射（实现时复用）

| 场景 | code |
|---|---|
| Bundle.Validate 失败 | `fingerprint_inconsistent` |
| hash 不匹配 | `fingerprint_hash_mismatch` |
| 未 Freeze | `fingerprint_not_locked` |
| UA major ≠ transport | `transport_profile_mismatch` |
| 出口国与 tz | `proxy_affinity_mismatch` |
| 无 bridge | `bridge_required` |
| capability | `bridge_capability_rejected` |
| TLS 对拍漂 | `transport_fixture_drift` |

与 `fingerprint.Error.Code` / job API 错误体对齐。

### 23.6 进度表（权威勾选）

> 更新规则：完成即改 `[ ]`→`[x]`，并改「最后更新」日期。

**最后更新：** 2026-07-18（D5 echo + PG dual-backend 骨架 + G4/H 门槛）

#### Phase A — FingerprintBundle v2
- [x] A1 Bundle v2 类型与 JSON schema（`bundle.go`）
- [x] A2 catalog 生成 desktop/mobile（`catalog.go`）
- [x] A3 Client Hints 派生（`hints.go`）
- [x] A4 一致性引擎 + 错误码（`consistency.go`）
- [x] A5 单测覆盖生成/亲和/脏数据/hash/JSON（`bundle_test.go`）
- [x] A6 Transport Profile 字段扩展 + OptionsFactory + bridge 校验（`transport/profile.go`, `factory.go`）

#### Phase B — Create 绑定
- [x] B1 Create/validate 强制 ParseJSON+AssertReady（或明确 server generate）
- [x] B2 profile_id 取 bundle_id / profile_uuid
- [x] B3 Runtime 暴露 Bundle / IdentityHeaders
- [x] B4 grant 与 proxy_affinity 校验
- [x] B5 job 单测 + `go test ./...`（证据：2026-07-18 `go test ./... -count=1` 全绿）

#### Phase C — HeaderPreset
- [x] C1 包骨架与 preset 名（`internal/headerpreset`）
- [x] C2 顺序组头 + OTP sparse
- [x] C3 golden / order 单测
- [x] C4 Datadog 默认关（开启则每请求新 trace）

#### Phase D — tls-client
- [x] D1 依赖锁定写入 go.mod（`github.com/bogdanfinn/tls-client v1.9.1`）
- [x] D2 TlsClientFactory + bridge CONNECT（`-tags tlsclient`）
- [x] D3 证书校验开（不调用 InsecureSkipVerify）
- [x] D4 major → Chrome_* profile 映射（`ChromeProfileNameForMajor`）
- [x] D5 echo fixture 对拍（`ProbeTLSEcho` + peet.ws/browserleaks；`-short` / 网络失败 skip；离线 parse 单测）
- [x] D6 factory 配置开关，**默认 fake**（worker `-transport=fake|tls`）

#### Phase E — FSM
- [x] E1 真 Runner 与 synthetic 双轨骨架（`protocol.Engine` + `RunnerConfig.ProtocolMode`）
- [x] E2 S0–S3 形状表（`s0_s3.go`）+ Engine 走 S0→S9（无真网）
- [x] E3 S4–S10 live handlers（fixture Do 驱动；真网需 Client+RequireExplicit）
- [x] E4 S11–S14 session tail（fixture Do；access_token 成功定义）
- [x] E5 ambiguous_after_send 标记（S7/S10/S11 发送后错误）
- [x] E6 fixture/engine 单测（`live_test.go` S0→S14）

#### Phase F — Sentinel + Fingerprint HAR 对齐
- [x] F1–F5c：PoW / pin / drift / Turnstile 骨架（见既有勾选）
- [x] **FP-HAR1** 用户 HAR 彻查：UA=Firefox/150；**无** sec-ch-ua*；Accept-Language=pt-BR…；encoding 含 zstd
- [x] **FP-HAR2** Go 支持 `BrowserFirefox` + `ForceBrowser` + 空 Client Hints（禁止 FF+CH 混用）
- [x] **FP-HAR3** pt-BR / America/Manaus 目录 + BR proxy_affinity；viewport 1280；hw=14
- [x] **FP-HAR4** HeaderPreset Firefox 路径：不发 CH、document Accept 简化、zstd
- [x] **FP-HAR5** live fixture 入库（完整字段，包内 testdata）；XOR key=request `p` 已证实
- [x] **FP-HAR6 / F5d3** 真实 dx → 可用 `t`（SDK 路径已通）
  - 已：XOR key=`request.p`；Firefox env；AnyStub；VM settle（短 `ZDB_` 仅 VM 回退路径）
  - 已：goja SDK sandbox 对齐 Node `loadSdkTurnstileRunner`
    - 根因1：自定义 JS `atob` 去 padding 后多解 1 字节 → nested `JSON.parse` SyntaxError
    - 根因2：`window.Reflect` 未注入 → 嵌套程序 `Reflect.set` 在 instruction 30 崩
  - 已：live fixture SDK 产出 **~760B** `t`，**前缀与 capture 一致**（Node oracle ~664B，同样非字节级全等）
  - 未：与 browser capture 字节级全等（非注册阻断）
- [x] **F5e / SO** sessionObserver 真路径 + 线上可用（2026-07-18）
  - 真路径：`D(req,p)` → `Ot(jt(collector_dx,p))` → 合成 pointer/key 事件 → `Nt(snapshot_dx)`（**不是** Turnstile `_n`）
  - 事件总线：goja sandbox `window/document.addEventListener` + 合成人机轨迹
  - 产出：`so` base64 **~540–544B**（decoded ~403–407）；HAR gold **524B**（decoded 391）同族、**非字节级全等**
  - 线上：`openai-sentinel-so-token` 已挂 S11 `create_account`；纯 Go 注册 SUCCESS（例 pk=2505/2506）
  - **未办（非阻断，可后补）：**
    - [ ] SO **HAR gold 字节级**对齐 — 需浏览器页内 hook 录 `__oai_so_*` + pointer/key 时间序列；普通 Fiddler/HAR **不够**（HTTP 无 DOM 事件）
    - [ ] 可选：Playwright/Patchright `addInitScript` 事件 dump 工具（用户暂缓）
- [x] D5 TLS/H2 echo — **已做**（live Chrome_133 → h2 + ja3/ja4；见 `internal/transport/echo*.go`）

#### Cutover CLI (2026-07-18 续)
- [x] `internal/proxy`：resource_pool 代理 CAS 租约（lajiao_credentials）
- [x] `cmd/pure-go-register` 默认从 DB 租代理（`-proxy`/`-proxy-file` 覆盖）
- [x] `internal/accounts`：成功写入 accounts + account_credentials
- [x] worker：`-pure-go -protocol-mode=live -transport=direct` 可选（默认仍 mailat）
- [x] live OTP 后继续 S10–S14（`runLiveFromOTP`）
- [x] G0 接线：`go_email_protocol_transport=direct` / `GO_EMAIL_PROTOCOL_PURE_GO=1` opt-in（默认仍 python/mailat）
- [x] bridge 校验允许 socks5://… capability=direct
- [x] Phase G3 CLI concurrent canary (6/8) + worker V2 path smoke
- [ ] Phase G4 默认 backend=go（需 worker 路径稳定成功率后再切）

### 23.7 当前指针

NEXT = G4 canary 压成功率（不改默认）→ 达标后再 flip backend=go
       并行：Phase H 阶梯压测前置
DONE_D5 = tls-client echo ProbeTLSEcho Chrome_133 http=h2 ja3/ja4; offline parse tests; -short skip
DONE_PG_FULL_CUT = env.db → PostgreSQL；全量 378180 rows 已导入；Python/Go 主写路径 fail-closed；Dashboard 已重启到 PG
DONE_G4_CANARY_20260718 = 8/8 + 12/12 + 16/16 after OTP window/pace hardening; historical 0/8 otp_timeout ×2; no default flip pending a distinct-window batch
DONE_GO_STORE = internal/store OpenMain sqlite + postgres fail-closed; wired accounts/proxy/mailbox
DONE_PG_LIVE_SAMPLE = local PG16 :5432 gpt_register; schema full; sample tables import + Python smoke
DONE_PG_MIGRATE_TOOL = tools/migrate_sqlite_to_pg.py export/import + schema_pg.sql; pytest green
         env: GPT_REGISTER_DB_BACKEND=postgres + DATABASE_URL/GPT_REGISTER_DATABASE_URL
         需: pip install 'psycopg[binary]>=3.1' + 可连 PG；init_db PG 跳过 SQLite PRAGMA 回填
DONE_SO = true path Et/collector→events→Nt(snapshot); synthetic traffic; so~540B live-accepted
DONE_SO_NOT = HAR gold 524B byte-identical (needs in-page event capture; user deferred)
DONE_G3_cli_canary = pure-Go concurrent 6/8 SUCCESS on export4+5 pool
DONE_G3_worker_path = worker -pure-go live+direct SOCKS reaches S6/OTP
DONE_F5e = pure-Go live register SUCCESS (S0–S14) + so-token on create_account
G4_GATE = 默认 backend 仍 python；opt-in: email_protocol_backend=go / GO_EMAIL_PROTOCOL_PURE_GO=1
         flip 条件: worker V2 连续多批成功率稳定后再改默认
H_GATE = 先 10→25→50 真注册压测 + 写库队列/PG；禁止未达标退役 Node/mailat
DONT = 不要在 G4 前把 Python 默认 backend 切到 go；不要删 mailat/Node 路径
       不要用 Fiddler 解密抠 SO 事件；不要无 DATABASE_URL 强切 postgres
TEST = go test ./... -count=1 ; go test -tags tlsclient ./internal/transport/ -count=1
       pytest tests/test_db_backend.py -q
OPEN =
  1. G4 canary 多批成功率 → 再默认 backend=go
  2. Phase H：10→25→50→100 真注册压测，验证 OTP/HME、资源池、PG 写库与取消/失败收敛；门槛后才退役 Node/mailat
  3. SO gold 字节级（用户暂缓；需浏览器页内事件录制）
DOC = docs/DB_POSTGRES_AND_CUTOVER.md
```

### 23.8 §16 验收勾选同步（指纹子集）

- [x] Bundle v2 可生成、可序列化、可 Freeze（checkpoint 就绪）  
- [x] 一致性引擎单测：mobile/desktop、edge/chrome、脏 UA/CH  
- [x] Client Hints 与 UA 解析基础向量  
- [x] proxy_affinity strict/allow 策略单测  
- [x] Create 路径强制校验（Phase B：`internal/job/profile.go` + API Code 映射）  
- [x] HeaderPreset 引擎 + OTP sparse（Phase C）  
- [x] TLS/H2 echo（D5：profile pin + live peet 对拍；网络不可达 skip）
- [x] FSM 双轨 + live S0–S14 fixture 路径（E1–E6）  
- [x] Sentinel PoW + pin SDK + Turnstile 路径骨架（F1–F5c）+ SO 真路径可用（非 gold 字节级）
- [ ] 真实 dx/SO **浏览器字节级**对拍 / 100 并发 / 切流（→ 后补 + H）

---
