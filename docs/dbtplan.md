# GPT Register 全量 Patch 迁移与稳固 DBT 计划

## 目标

本计划只解决一件事：

> 把当前“邮箱注册 -> 手动 Plus -> 绑定手机/CPA”链路，**彻底迁移到 Patch 主链**，并把当前绑定老失败的问题在同一轮内一并稳固掉。

这里的“彻底”包含两层含义：

1. **浏览器栈彻底统一**：后半段不再以 Camoufox 作为主链依赖。
2. **绑定流程彻底稳固**：不能只是“能跑”，而要把号码资源、OTP 提交、callback、token 规范化、失败分类、回写闭环一起做完。

## DBT 定义

- **D = Design**：设计与边界收敛；明确模块职责、兼容契约、并行边界、旧实现去留。
- **B = Build**：实现代码；必须是最终实现，不做临时桥、假 fallback、注释 TODO 交付。
- **T = Test**：验证与验收；覆盖关键成功路径、关键失败路径、资源回写与状态分类。

## 依据

### 当前 GPT Register 现状

- 邮箱注册已默认走 Patch：
  - `api/register.py:67-77`
  - `registration/email_register.py:139-150`
  - `core/browser/session.py:197-244`
  - `platforms/chatgpt/fast_email_register.py:62-68`
- 绑定/CPA 仍走 Camoufox：
  - `full_pipeline.py:2253`
- 当前绑定主链：
  - `full_pipeline.py:2220-2368`
  - `platforms/chatgpt/browser_register.py:2052-2313`
  - `platforms/chatgpt/browser_register.py:2549-2828`
- 当前已确认问题：
  - 动态 bind phone lease 不进统一回写
  - `phone_cleanup()` 失败路径未执行
  - `UserProvidedSmsProvider.cancel()` 被 no-op 覆盖
  - `status=0` 被直接判终局失败
  - `recently used` 未正确打回号码池
  - 最终错误被压扁成“未获取到 access_token”

### paylink 项目给出的新启发

高价值参考：`E:/project/openai-register-paylink-ui/app.py`

- JSON OAuth 授权流：`1996-2376`
- 浏览器注册 / session 提取：`2379-3657`
- 浏览器上下文内 phone send/validate API：`2995-3069`
- 严格 token 规范化：`1090-1124`, `1209-1228`, `1327-1346`

结论：

1. 绑定手机/RT 这段不必继续死绑在重页面状态机里。
2. add-phone send/validate 可以 API 化。
3. token 结果必须严格规范化，不能接受半成功。
4. live context handoff 可借鉴，但不能替代 GPT Register 当前的 `resume_file + browser_storage_state_path` 契约。

---

# 总体设计

## 目标链路

从当前：

```text
Patch 邮箱注册
-> 手动 Plus
-> Camoufox resume-oauth
-> add-phone / OAuth / CPA
```

迁移为：

```text
Patch 邮箱注册
-> 手动 Plus
-> Patch resume-bind
   -> JSON OAuth flow 优先
   -> add-phone API send/validate 优先
   -> Patch 页面流兜底
   -> CPA callback submit
```

## 最终模块划分

### 模块 A：Patch 浏览器会话层
职责：
- 统一注册与 resume-bind 的浏览器启动与 storage_state 恢复
- 统一 proxy / locale / timezone / accept-language / profile 语义
- 彻底解除后半段对 Camoufox 主链依赖

核心文件：
- `core/browser/session.py`
- `full_pipeline.py`
- `platforms/chatgpt/browser_register.py`

### 模块 B：绑定资源与错误分类层
职责：
- bind phone 生命周期闭环
- 结构化错误分类
- `recently used` / `status=0` / timeout / invalid 等状态回写
- 动态 lease 进入统一 report/release/cooldown

核心文件：
- `core/base_sms.py`
- `application/resource_pool_service.py`
- `registration/phone_bind.py`
- `platforms/chatgpt/browser_register.py`
- `full_pipeline.py`

### 模块 C：Patch Resume Bind 引擎
职责：
- 从 `resume_file + storage_state` 恢复
- 走 JSON OAuth bind flow
- 获取 callback/code
- 继续提交 CPA callback 或本地 token 交换

核心文件：
- `full_pipeline.py`
- 新增 `registration/patch_resume_bind.py`（或 `platforms/chatgpt/patch_resume_bind.py`）
- `platforms/chatgpt/oauth.py`

### 模块 D：Add-phone Transport 层
职责：
- 优先 browser-context API send/validate
- 保留现有 Patch 页面流兜底
- provider hook 与资源回写不丢

核心文件：
- `platforms/chatgpt/browser_register.py`
- 新 Resume Bind 引擎文件

### 模块 E：严格 token 规范化层
职责：
- access / refresh / id token 完整性校验
- account_id / exp 必填
- 双 token endpoint fallback
- 明确区分：局部成功 / 真成功

核心文件：
- `platforms/chatgpt/oauth.py`
- `full_pipeline.py`
- 可能新增 token normalization helper

---

# 并行策略

## 可以并行的工作流

### Workstream 1 — 资源闭环与错误分类
范围：模块 B

并行理由：
- 主要改资源回写、错误分类、cleanup
- 不依赖新 JSON bind 引擎先落地
- 可先稳住现有链路的最危险失败模式

### Workstream 2 — 浏览器层统一到 Patch
范围：模块 A

并行理由：
- 主要是启动与 session 恢复抽象
- 与资源回写耦合小
- 是后续新 bind 引擎的前置基础，但实现可先行完成

### Workstream 3 — JSON Resume Bind 引擎
范围：模块 C + E

并行理由：
- 可以基于现有 `resume_file` 契约独立开发
- 与 Workstream 1 的共享面主要是错误分类常量/结果结构
- 与 Workstream 2 的共享面主要是 BrowserSession 使用方式

### Workstream 4 — add-phone API transport
范围：模块 D

并行理由：
- 可在保留现有 UI-first 流的前提下独立落地
- 只要遵守现有 phone callback/provider hook 契约即可

## 不应并行的点

### 1. `resume_file` / `browser_storage_state_path` 契约
原因：
- 这是所有后续流程共享的核心契约
- 一旦多人同时改 shape，极易互相踩坏

处理：
- 由主设计确定后，只允许单 workstream 修改 schema

### 2. 最终 `full_pipeline.py` 的 resume-oauth 主入口切换
原因：
- 这是总装点
- 同时改启动逻辑、bind 主路径、结果回写会互相冲突

处理：
- 等 Workstream 1/2/3 的基础代码都到位后再统一接线

### 3. 端到端验收测试
原因：
- 必须由主 agent 统一执行
- 否则子 agent 会各自围绕局部实现假定成功标准

---

# DBT 明细（按小项目拆分）

## 小项目 1：绑定资源闭环与错误分类

### D
- 明确 bind phone 的终态集合：
  - `success`
  - `released`
  - `recently_used`
  - `invalid`
  - `timeout`
  - `otp_submit_failed`
  - `transport_failed`
- 明确 `resume-oauth` 错误分类集合：
  - `oauth_authorize_timeout`
  - `oauth_add_phone_required`
  - `phone_recently_used`
  - `phone_otp_submit_status_0`
  - `cpa_callback_failed`
  - `oauth_no_callback_code`
  - `oauth_no_access_token_after_callback`

### B
- 修 `UserProvidedSmsProvider.cancel()` no-op 覆盖 bug
- `resume-oauth` 失败路径强制 `phone_cleanup()`
- 动态 bind phone 纳入统一 report/release/cooldown
- `status=0` 不再直接终局
- `recently used` 必须回写资源池
- 不再把错误压扁成统一“未获取到 access_token”

### T
- 失败任务后 bind phone 不残留 `leased`
- `recently used` 进入 fail/cooldown
- `status=0` 有独立错误分类
- 失败原因落库/日志可区分

## 小项目 2：resume-oauth 浏览器栈统一到 Patch

### D
- 定义 resume-bind 的 session 恢复规则：
  - `storage_state` 为主
  - profile 仅做隔离
  - 不允许 persistent context 静默覆盖 resume storage
- 定义 BrowserSession 在 resume 流中的唯一入口地位

### B
- `full_pipeline.py:step_oauth_from_saved_session()` 改为通过 `BrowserSession` 启动
- honor `browser_engine=patchright`
- 解耦 `browser_register.py` 顶层 Camoufox import
- 清理 resume 流对 Camoufox 的主链依赖

### T
- 无 Camoufox 环境下可 import helper
- `browser_engine=patchright` 下 resume 流能启动
- storage_state 能正确恢复并进入后续流程

## 小项目 3：JSON Resume Bind 引擎

### D
- 定义 JSON bind flow 步骤：
  1. prepare authorize URL
  2. authorize continue
  3. email OTP send/validate
  4. add-phone send/validate
  5. workspace select
  6. follow redirects
  7. extract callback/code
  8. CPA submit 或本地 token exchange
- 明确何时走 JSON 主路径、何时退回页面流

### B
- 新增 `patch_resume_bind` 模块
- 接入现有 `resume_file` / `browser_storage_state_path` / proxy 契约
- 复用现有 CPA callback 提交
- 实现双 token endpoint fallback
- 严格 token normalization

### T
- JSON flow 能推进到 callback/code
- CPA 模式能成功 submit callback
- local 模式能成功拿到完整 token record
- 缺少 refresh/id/account_id/exp 时明确失败

## 小项目 4：add-phone API transport 优先

### D
- 设计 send/validate API path
- 设计 UI fallback path
- 设计 provider hook 触发点：
  - `mark_send_failed`
  - `mark_send_succeeded`
  - `mark_code_failed`
  - `report_success`
  - `cleanup`

### B
- 在 add-phone 流中优先 browser-context API send/validate
- selector/UI 只作为 fallback
- route error / Cloudflare / transport failure 独立分类

### T
- add-phone 页面变化时 API path 仍可推进
- route error 不再混入 phone bad number
- provider hook 语义与旧链一致

## 小项目 5：严格 token 规范化

### D
- 统一成功定义：
  - 非完整 token record 一律不是 bind success
- 统一字段要求：
  - access_token
  - refresh_token
  - id_token
  - account_id
  - exp

### B
- 增加 normalization helper
- 把 `oauth.py` / `full_pipeline.py` / export 路径统一接到该 helper
- 双 endpoint token fallback

### T
- 半成功 payload 不再被当成功
- endpoint A 失败时 endpoint B 可尝试
- token record 结构稳定

---

# 总装顺序

## 必须先完成
1. 小项目 1：绑定资源闭环与错误分类
2. 小项目 2：resume-oauth 浏览器栈统一到 Patch

## 然后并行推进
3. 小项目 3：JSON Resume Bind 引擎
4. 小项目 4：add-phone API transport 优先
5. 小项目 5：严格 token 规范化

## 最后统一接线
- 将 `resume-oauth` 主路径切到：
  - Patch BrowserSession
  - JSON bind flow 主路径
  - page flow fallback
  - CPA submit/local token exchange

---

# 子 agent 执行策略

## 主 agent职责
- 审核设计是否偏离目标
- 统一总装与冲突决策
- 执行验收测试
- 对失败 workstream 派发修复子 agent

## 子 agent职责
- 各自完成一个小项目的 Build
- 不跑全量 lint/format
- 不做最终验收结论
- 发现跨 workstream 契约冲突时，立刻回报主 agent

---

# 验收标准

全部完成后，必须同时满足：

1. 邮箱注册仍走 Patch 链，行为不回退。
2. `resume-oauth` 主链不再依赖 Camoufox 启动。
3. 绑定手机 + CPA 可在 Patch 主链下完成。
4. `recently used`、`status=0`、timeout、transport failure 都有明确分类与资源回写。
5. token 结果必须完整；半成功不能入库为成功。
6. `resume_file + browser_storage_state_path` 仍是可靠主契约。
7. 所有关键失败都能从任务日志/账号记录中直接定位，而不是只剩“未获取到 access_token”。

---

# 不做的事

本轮不做：
- 复制 paylink 的 GUI/state.json 架构
- 用 live context 替代 resume 文件契约
- 仅做“Camoufox -> Patchright”字面替换
- 先改前端样式/交互而忽略后端主链

这轮目标是：**彻底迁移 + 彻底稳固。**
