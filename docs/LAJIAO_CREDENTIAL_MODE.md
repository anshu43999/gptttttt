# 辣椒 HTTP 账号密码模式完整使用指南

## 概述

辣椒 HTTP 代理有两种购买方式：

| 模式 | 原理 | 配置 key |
|------|------|----------|
| **API 提取** | 用 URL key 调辣椒 API，返回 `ip:port` 列表 | `lajiao_proxy_mode: api` |
| **账号密码** | 给一个固定账号（如 `jdebv23194-r`），买几个 IP 就绑几个 IP | `lajiao_proxy_mode: credentials` |

本文档讲解**账号密码模式**的完整使用方法。

---

## 一、工作原理

> 总架构：**你的代码 → 本地 HTTP 代理 (127.0.0.1) → 上游 SOCKS5 认证网关 → 目标网站**

### 问题

浏览器自动化会遇到一个死结：

1. **辣椒凭证模式给你的是一条 HTTP CONNECT 代理**（类似 `curl -x http://user:pass@gate:port`）
2. **Camoufox 浏览器只支持 SOCKS5 代理**，不支持 HTTP CONNECT
3. 即使你把 `lajiao_proxy_credential_protocol` 改成 `socks5`，Camoufox 和 Playwright 的 SOCKS5 客户端也**不支持用户名密码认证**

### 解法：本地 HTTP → SOCKS5 认证桥

项目内置了一个 `_LocalHttpToSocksBridge` 类，自动：

1. 在 `127.0.0.1` 上启动一个随机端口的 **本地 HTTP 代理**
2. 浏览器通过这个本地代理（无认证）连接
3. 桥收到 HTTP CONNECT 请求后，**用用户名密码认证连接上游辣椒 SOCKS5 网关**
4. 双向转发数据

```
┌──────────────┐     HTTP CONNECT      ┌─────────────────┐     SOCKS5 + 认证    ┌───────────────────┐
│ Camoufox     │ ────────────────────> │ 本地桥          │ ──────────────────> │ 辣椒网关          │
│ (浏览器)     │ <──────────────────── │ 127.0.0.1:54321 │ <────────────────── │ gate.lajiao.com   │
│              │   双向数据转发         │ (无认证)         │   双向数据转发       │ (用户名+密码认证)  │
└──────────────┘                       └─────────────────┘                      └───────────────────┘
```

字节级握手过程（代码逐字节实现了完整的 RFC 1928 SOCKS5 协议）：

```
浏览器 ──HTTP CONNECT chatgpt.com:443──> 本地桥

本地桥 ──SOCKS5 GREET [5,1,2]────────> 辣椒网关    # 请求用户名密码认证
本地桥 <──SOCKS5 REPLY [5,2]────────── 辣椒网关    # 确认需要认证
本地桥 ──AUTH [1,len(user),user,len(pass),pass]──> 辣椒网关    # 发送用户名密码
本地桥 <──AUTH OK [1,0]─────────────── 辣椒网关    # 认证成功
本地桥 ──SOCKS5 CONNECT [5,1,0,3,域名,443]──> 辣椒网关    # 请求连接目标
本地桥 <──SOCKS5 OK [5,0,...]───────── 辣椒网关    # 隧道建立

本地桥 ──HTTP 200 Connection Established──> 浏览器

          <════════════ 双向数据中继 ════════════>
```

---

## 二、配置

### 2.1 最小配置

```yaml
# config.yaml

# 核心开关
lajiao_proxy_mode: credentials
lajiao_proxy_credential_protocol: http   # 辣椒账号密码网关用 HTTP CONNECT 隧道
lajiao_proxy_credentials: |
  jdebv23194-r:你的密码@gate1.lajiaohttp.com:10001
  jdebv23194-r:你的密码@gate2.lajiaohttp.com:10002

# 国家出口控制
lajiao_proxy_expected_country: "JP"    # 强制校验出口 IP 是否为日本
lajiao_proxy_regions: "JP"            # 告诉辣椒我要日本节点

# 代理轮换
rotate_proxy_each_attempt: true       # 每次注册失败换一个代理 IP

# Camoufox 自动对齐
camoufox_geoip: true                  # 让浏览器时区/语言跟随代理出口 IP
```

### 2.2 完整配置项

```yaml
# ──── 凭证模式专用 ────
lajiao_proxy_mode: credentials           # api | credentials
lajiao_proxy_credential_protocol: http   # 辣椒凭证教程用 curl -x http://...
                                          # 如果确认是 SOCKS5 入口再改 socks5
lajiao_proxy_credentials: |              # 每行一个 user:pass@host:port
  user1:pass1@gate.example.com:10001
  user2:pass2@gate.example.com:10002
lajiao_proxy_credentials_file: ""        # 或者从文件读取，每行格式同上
                                          # 两处都配则合并去重

# ──── 代理选择与验证 ────
lajiao_proxy_expected_country: "JP"      # 为空则不校验国家
                                          # 填 JP/US/IN/BR，出口 IP 不对则跳过
lajiao_proxy_regions: "JP"              # 显示名称（日志用）
lajiao_proxy_timeout: 15                # 每个代理的超时时间（秒）
lajiao_proxy_max_candidates: 60         # 最多检查多少个候选
lajiao_proxy_select_deadline: 300       # 找代理最多等多久（秒）

# ──── 轮换策略 ────
rotate_proxy_each_attempt: true          # true=每次注册失败换代理
                                          # false=一直用同一个

# ──── 浏览器集成 ────
camoufox_geoip: true                     # 让 Camoufox 时区/语言跟随出口 IP
```

### 2.3 账号密码文件格式

`proxies.txt`（或直接写在 YAML 中）：

```
# 每行一个代理，格式：用户名:密码@主机:端口
# 可以加 http:// 前缀，不加则默认 http
jdebv23194-r:abc123def@jp01.lajiaohttp.com:10001
jdebv23194-r:abc123def@jp02.lajiaohttp.com:10002
jdebv23194-r:abc123def@jp03.lajiaohttp.com:10003
```

### 2.4 配置文件引用

```yaml
# 方式A：直接写在 config.yaml 里
lajiao_proxy_credentials: |
  jdebv23194-r:pass@gate1.lajiaohttp.com:10001
  jdebv23194-r:pass@gate2.lajiaohttp.com:10002

# 方式B：从文件读取
lajiao_proxy_credentials_file: "proxies.txt"

# 方式C：两者都用（合并去重）
lajiao_proxy_credentials: |
  jdebv23194-r:pass@gate1.lajiaohttp.com:10001
lajiao_proxy_credentials_file: "proxies.txt"
```

---

## 三、代码自动做了什么

### 3.1 启动时

```
1. 读取 lajiao_proxy_credentials + lajiao_proxy_credentials_file → 合并去重
2. 对于每行 user:pass@host:port → 补全协议前缀（默认 http://）
3. 生成候选代理列表
4. 注册前选代理时逐个验证 → 选择可用的
```

### 3.2 选代理时（`_select_fresh_proxy_for_attempt`）

```
1. 如果 rotate_proxy_each_attempt = false → 用当前的，不换
2. 否则从候选列表逐个取代理
3. 对每个候选：
   a. 用 https://api.ipify.org 测通性 → 获取出口 IP
   b. 用 ipinfo.io 测出口国家 → 和 lajiao_proxy_expected_country 对比
   c. 不匹配 → 跳过，日志: "辣椒 HTTP 代理国家不匹配"
   d. 出口 IP 已用过 → 跳过，日志: "代理出口 IP 已用过"
   e. 匹配且未用过 → 选中
4. 记录出口 IP 到 used_proxy_ips（保证本 run 内不重复）
5. 设置 camouflage_geoip_ip（让浏览器跟随代理）
```

### 3.3 浏览器启动时（`_launch_camoufox`）

```
1. 检查 lajiao_proxy_mode 是否为 credentials
2. 检查代理 URL 是否含用户名密码
3. 如果含 → 启动本地 HTTP 桥：
   - 创建 _LocalHttpToSocksBridge(上游host, 上游port, 用户名, 密码)
   - 启动 daemon 线程监听 127.0.0.1:随机端口
   - 返回 http://127.0.0.1:54321（本地无认证地址）
4. 将本地桥地址传给 Camoufox 作为代理配置
5. Camoufox 用本地地址（无认证）→ 桥自动用用户名密码连接上游
```

### 3.4 清理时（`_cleanup`）

```
1. 关闭所有本地桥 → 释放端口
2. 关闭浏览器
```

---

## 四、代理验证详解

凭证模式用**两层验证**（比 API 模式少，因为不需要测 ChatGPT CSRF）：

```
第1层 → https://api.ipify.org?format=json
         用 requests 库，通过代理发请求
         拿到 exit_ip（实际出口 IP）
         → 不通 → 跳过此代理

第2层 → https://ipinfo.io/{exit_ip}/json
         验证出口 IP 的国家
         如 lajiao_proxy_expected_country = "JP"
         但 ipinfo 返回 country = "US"
         → 跳过此代理，日志: "辣椒 HTTP 代理国家不匹配，跳过"
         
第3层 → 查 self._used_proxy_ips
         出口 IP 已在本 run 用过？
         → 跳过，日志: "代理出口 IP 已用过，跳过"
```

---

## 五、常见错误

| 报错 | 原因 | 解决 |
|------|------|------|
| `辣椒账号密码代理文件不存在: xxx` | `lajiao_proxy_credentials_file` 路径写错 | 检查文件路径 |
| `lajiao_proxy_mode=credentials 但未配置...` | 既没写 YAML 内联也没配置文件 | 至少配一个 |
| `辣椒 HTTP 代理池耗尽或超时` | 所有候选代理都验证不通过 | 检查账号是否过期 / 国家配置是否正确 / 增加候选数 |
| `辣椒 HTTP 代理国家不匹配` | 代理出口 IP 和 `lajiao_proxy_expected_country` 不一致 | 在辣椒后台确认该账号绑定的 IP 是哪个国家 |
| `upstream socks authentication failed` | 上游 SOCKS5 认证失败（用户名密码错误） | 检查账号密码是否正确 |
| `upstream socks connect failed` | 上游 SOCKS5 连不上目标（网关拒绝了 CONNECT） | 辣椒网关可能限制了对特定端口的访问 |

---

## 六、两种模式选择建议

| 场景 | 推荐模式 |
|------|---------|
| 新用户、快速上手 | `api` — 一行 URL key 搞定 |
| 需要固定出口 IP、白名单场景 | `credentials` — 账号密码，IP 不变 |
| 大量并发注册（多台机器） | `api` — 每次提取新 IP，天然分散 |
| 对 IP 质量要求极高（ChatGPT Plus 订阅） | `credentials` — 可买高质量日本静态 IP |
| 网络环境受限（API 被封） | `credentials` — 走 HTTP CONNECT 隧道 |

---

## 七、流程图

```
┌─ config.yaml ─────────────────────────────────────────────────┐
│ lajiao_proxy_mode: credentials                                 │
│ lajiao_proxy_credentials: "user:pass@gate:port"                │
│ lajiao_proxy_expected_country: "JP"                            │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ RegisterPipeline.__init__ ────────────────────────────────────┐
│ 解析 user:pass@gate:port → 候选列表 ["http://user:pass@gate:port"]
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ _select_fresh_proxy_for_attempt() ────────────────────────────┐
│ 对每个候选:                                                     │
│   1. requests.get(api.ipify.org, proxy=候选) → exit_ip         │
│   2. requests.get(ipinfo.io/{exit_ip}, proxy=候选) → country   │
│   3. country != "JP" → 跳过                                     │
│   4. exit_ip in used_ips → 跳过                                 │
│   5. 通过 → config["proxy"] = 候选                             │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ _launch_camoufox() ──────────────────────────────────────────┐
│ 1. parse_url(config["proxy"]) → host, port, user, pass         │
│ 2. bridge = _LocalHttpToSocksBridge(host, port, user, pass)    │
│ 3. bridge.start()  →  监听 127.0.0.1:54321                    │
│ 4. Camoufox 代理配置 = "http://127.0.0.1:54321" (无认证)       │
│ 5. Camoufox 所有请求 → 本地桥 → 辣椒网关                        │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ 注册完成 / 失败 ──────────────────────────────────────────────┐
│ _cleanup(): bridge.close() → 释放端口                           │
└────────────────────────────────────────────────────────────────┘
```
