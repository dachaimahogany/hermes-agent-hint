# agent-hint

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Hermes >= 0.21.6](https://img.shields.io/badge/hermes-%E2%89%A50.21.6-8A2BE2.svg)](https://github.com/NousResearch/hermes-agent)
[![stdlib only](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#safety-properties)
[![tests](https://img.shields.io/badge/tests-72%20passing-brightgreen.svg)](#tests)
[![measured impact](https://img.shields.io/badge/report-benchmark%20%2B%20field%20test-orange.svg)](docs/benchmark-report.md)

**English** · **[中文](#中文)**

> Give your inference engine — scheduler, gateway, KV-cache manager — a **standard,
> machine-readable view of the agent session** driving each request: which session it is, where in
> the session's call sequence it sits, whether a person is waiting on it, and how long to hold the
> prefix it is about to reuse.

The plugin attaches a single HTTP header — `X-Agent-Hint` — to **main LLM requests only**, and sends
**manage requests** (empty-body calls carrying exactly one lifecycle verb) when a session pauses,
resumes, stops, or is compacted. It never touches the request body, never modifies Hermes core, and
is **off by default**.

```
LLM request ──► llm_request middleware ──► + X-Agent-Hint header ──► inference engine
                                          ▲
  hooks (session / tool / approval / subagent / compaction) ──► manage request (pause | resume | stop | compact)
```

## Why

An OpenAI-compatible HTTP API carries no notion of "session". An agent, however, has one: turn
boundaries, tool calls, approval waits, sub-agents, and a strong affinity to whatever instance
holds its KV cache. Engines that can see that structure can schedule better — pin a session to the
instance that already holds its cache, let a user-blocked session outrank a background one, or hold
capacity for a session that is about to resume.

### Measured impact

Not a hypothesis — measured against a MindIE-Motor deployment serving Qwen3-8B on an 8-card
Ascend 910B4 NPU server (vLLM engines, prefix caching on):

| What was measured | No hint | With hint |
|---|---|---|
| Which instance a session lands on | 6 requests scattered **3:3** across two instances, `aff=-`, `prio=None` | same session pinned: `aff=fresh` then **`aff=hit` ×3** (4 requests); **7 consecutive affinity hits** in an 8-round run; the no-hint control logged none |
| Prefix-cache hit rate | cold prefix: **0.0%** | warm prefix on the same instance: **84–99%** |
| p50 TTFT, ~3 k-token prefix | 2.079 s | **1.598 s → 1.30× (≈0.48 s saved)** |

Cost of the above: **one header**, no request-body change, no dependency, off by default. Nine real
agent requests were captured in flight — zero header decode failures, and not one request body
carried a hint field.

**Full evidence, method, and honest limitations →**
**[`docs/benchmark-report.md`](docs/benchmark-report.md)** ·
**[`docs/benchmark-report.zh-CN.md`](docs/benchmark-report.zh-CN.md)**

## Install

```bash
hermes plugins install dachaimahogany/hermes-agent-hint   # or: hermes plugins install <repo-url>
```

Or drop the directory into `$HERMES_HOME/plugins/agent-hint/` (per-profile) and enable it:

```bash
hermes plugins enable agent-hint
# if the enable command is unavailable in your setup, set the list directly in config.yaml:
#   plugins:
#     enabled: ["agent-hint"]
```

> **Installing does not turn it on.** Two switches must both be on, or the middleware returns
> immediately and no header is sent:
>
> 1. the plugin is enabled (`plugins.enabled` / `hermes plugins enable agent-hint`),
> 2. the plugin's own master switch is on: `settings.enabled: true`, or `AGENT_HINT_ENABLED=1`.

## Configure

Settings live under `plugins.entries.agent-hint.settings` in `config.yaml`; every key can also be set
via an `AGENT_HINT_<KEY>` environment variable. Precedence: **settings > env > built-in default**.
Every key is optional — the defaults are the safe defaults.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. Off = no header, no manage request. |
| `priority_level` | `3` | `priority_control.priority.level`, folded to the two-level contract: `>= 3` becomes `3` (a person is waiting), anything else becomes `1`. `<= 0` omits the whole block. |
| `cache_pin_end` | `auto` | `cache_control.msg_offset`. `auto` (default) = this request's wire message count, system prompt included; `0` emits no declaration; an integer pins an explicit position (out of range = no declaration). |
| `cache_ttl_s` | `auto` | `cache_control.ttl_s`. `auto` derives it from the observed gap (else 300 s), doubled and clamped to 60–3600; `0` sends no declaration at all; a number fixes it. |
| `epoch_on` | `summarize,micro_summarize,provider_native` | Which `post_compaction` kinds count as a context rewrite and bump `session.epoch`: a comma list over the host's closed set (`summarize`, `micro_summarize`, `prune_tool_results`, `provider_native`), or `all` / `none`. `prune_tool_results` only trims tool results and leaves the message count unchanged, so it does not count by default. |
| `manage_sink` | `none` | Manage-request transport: `ndjson`, `http`, or `none`. |
| `manage_url` | *(empty)* | Target URL for the `http` sink. Empty = no network traffic at all. |
| `manage_model` | *(empty)* | `model` of the placeholder manage body. Empty omits the key. |
| `manage_token_env` | `AGENT_HINT_TOKEN` | Name of the env var holding the bearer token for the `http` sink. The token travels in a header, never inside the hint. |
| `manage_timeout_s` | `5` | Per-request timeout, clamped to 1–30. |
| `manage_max_retries` | `1` | Retries per manage request, clamped to 0–3. |
| `manage_queue_limit` | `64` | Bounded queue depth for the `http` sink; overflow drops the newest, clamped to 1–1024. |

Example:

```yaml
plugins:
  entries:
    agent-hint:
      settings:
        enabled: true
        priority_level: 3
        cache_ttl_s: auto
        epoch_on: summarize,micro_summarize,provider_native
        manage_sink: http
        manage_url: http://127.0.0.1:8080/v1/chat/completions
        manage_model: Qwen3-8B
```

## The `X-Agent-Hint` header

`base64url(UTF-8 JSON)`, padding stripped — header values must stay ASCII and single-line. Four
blocks, and only four:

| Block | Fields | Notes |
|---|---|---|
| `session` | `session_id`, `state`, `call_index`, `epoch`, optional `parent_session_id` | `state` is one of `start` / `append` / `pause` / `resume` / `stop` / `compact`. `start` appears exactly once per fresh session; every other ordinary request is `append`. `epoch` increments when the context is rewritten — driven by the host's `post_compaction` hook, gated by `epoch_on`. |
| `context_management` | `manage_request`, `edits[]` | Present on **manage requests only**; an edit carries only `type` and `target`, and `target` is always `session` (the version lives in `session.epoch`, not in the edit). |
| `priority_control` | `priority.level`, `priority.reason` | `level` is `3` (a person is waiting) or `1` (background). `reason` is **derived** from `level`, never configured separately — the two cannot contradict each other. |
| `cache_control` | `type`, `ttl_s`, `msg_offset` | One object, `type` always `ephemeral`. `msg_offset` is this request's wire message count (the system prompt counts as message 1). |

`ttl_s = clamp(round(last observed interval × 2), 60, 3600)` — declare the hold you are about to
reuse, not a fixed guess. The declaration is suppressed on a round that rewrites the context, on a
round whose model or tool set changed, on a prefix shorter than one cache block, and on any round
that carries an empty assistant message.

Deliberately **not** emitted: `agent_status`, `prediction`, `derivation`, `agent_type`,
`strict_priority`, `cache_control[].range`/`unit`, `latency_control`. The plugin's internal state
(`running` / `tool` / `pause` / `finalize`) never reaches the wire — an engine does not need to know
what the agent is doing, only whether to keep or drop the context after this turn. A client-side
plugin must not be able to claim a hard priority either; trust decisions belong to the control plane.

Auxiliary calls — title generation, summaries requested for some other purpose, anything carrying a
non-empty `purpose` — carry **no hint at all**.

### Manage requests

`pause` / `resume` / `stop` / `compact` each own **one** dedicated request: `POST <manage_url>`, an
empty placeholder body (`{"model": …, "messages": [], "stream": false}`) and the verb in the header.
A consumer must accept an empty `messages` list when `context_management.manage_request` is true.

| Verb | `edits[0].type` | Sent when |
|---|---|---|
| `pause` | `offload` | an approval is raised and the turn is blocked on a person |
| `resume` | `prefetch` | that approval is answered |
| `stop` | `evict` | the session finalizes |
| `compact` | `evict` | the context is compacted; the hint carries `epoch - 1`, the generation that was dropped |

Manage requests are deduped per session (two consecutive identical verbs carry no new information),
never counted in `call_index`, and go through the same fail-safe sink as everything else.

## Safety properties

- **Off by default.** Nothing is injected and nothing is sent until you enable it.
- **No network unless you point it at a URL.** `manage_sink` defaults to `none`; `ndjson` writes
  locally, `http` sends only to `manage_url`.
- **The token stays out of the hint.** A bearer token goes in an `Authorization` header on the
  manage request; the hint itself is treated as engine-visible log material.
- **Fail-safe.** Every hook and every sink swallows its own errors (warn-once to stderr) — a broken
  hint can never break a turn. The middleware returns the request untouched on any failure.
- **Pure stdlib, no core access.** Only `ctx.register_hook` / `ctx.register_middleware` /
  `ctx.get_config` are used; no Hermes internals are imported and no core object is patched.
- **Observers stay observers.** `pre_tool_call` never returns a block/approve/modify directive.
- **Unsupported by design:** `plugins.isolation: host` (a per-profile host process) is not covered —
  the middleware would have to serialize the whole request across the process boundary on every LLM
  call. This plugin is intended for the default `in_process` isolation.

## Compatibility

`requires_hermes: ">=0.21.6"`. Verified against an **unmodified** Hermes v0.21.6 checkout — the
plugin uses only public, documented surfaces (`VALID_HOOKS` ∩ the `llm_request` middleware), so no
core patch is needed.

Hook payloads are read defensively and by keyword; a hook whose payload changes shape degrades to
"no event" rather than an error.

## Tests

```bash
python3 -m pytest tests/     # 85 tests, no network, no Hermes install required
```

## Verification

Beyond the unit tests, three independent results back this plugin (all on an **unmodified** Hermes
v0.21.6 checkout):

1. **Upstream gate** — `hermes plugins validate .` passes in full: manifest fields, `requires_hermes`,
   `config_schema`, the capability probe (`register()` run in isolation, declared hooks/middleware
   matching registrations), security scan, and "no core override".
2. **Standalone turn + real engine** — installed as the *only* plugin into a clean `HERMES_HOME`
   (no bundle, no companion plugins), a real `hermes -z` turn produced two `X-Agent-Hint` headers
   (`state=start` then `append`, `call_index` 1→2, `cache_control.msg_offset` tracking the wire
   message count) and a `stop` manage request at finalize; with the plugin disabled the same run was
   completely silent. The header bytes emitted by this plugin's own `_builder`/`_codec` were then sent
   to a MindIE-Motor scheduler, whose log shows the session being pinned — `session affinity hit
   session_id=… instance_id=4` after a first request with `prio=3 aff=fresh` and a second with
   `prio=3 aff=hit`, while an identical request without the header logged `prio=None aff=-`.
3. **Benchmark + field test** — cold/warm prefix-cache measurement, hint-off/on routing comparison,
   and 9 captured real-agent requests, with the numbers in the [report](docs/benchmark-report.md).

## License

MIT — see [`LICENSE`](LICENSE).

---

# 中文

**中文** · **[English](#agent-hint)**

> 给下游推理引擎（调度器 / 网关 / KV 缓存管理器）一个**标准化、机器可读的「agent 会话视图」**：
> 这个请求属于哪个会话、在会话的调用序列里排第几、是否有人正在等它、前缀该留多久。

插件把**单个 HTTP 头 `X-Agent-Hint`** 附加到**主 LLM 请求**上；当会话暂停 / 恢复 / 结束 / 被压缩时，
另发一条 **manage 请求**（空占位体，只带一个生命周期动词）。它**绝不修改请求体**、**绝不改动 Hermes
内核**，且**默认关闭**。

```
LLM 请求 ──► llm_request 中间件 ──► 附加 X-Agent-Hint 头 ──► 推理引擎
                                    ▲
   hooks（会话 / 工具 / 审批 / 子代理 / 压缩）──► manage 请求（pause | resume | stop | compact）
```

## 为什么需要它

OpenAI 兼容的 HTTP API 里没有「会话」这个概念。但 agent 有：回合边界、工具调用、审批等待、子代理，
以及「强烈倾向于回到持有自己 KV 缓存的实例」这一事实。能看见这层结构的引擎可以调度得更好 —— 把会话
钉在已持有其缓存的实例上、让被用户阻塞的会话压过后台任务、或为即将恢复的会话预留容量。

### 实测影响

这不是推测 —— 在一套 MindIE-Motor 部署上实测（8 卡 Ascend 910B4 NPU 服务器，服务 Qwen3-8B，
vLLM 引擎开启前缀缓存）：

| 测什么 | 无 hint | 带 hint |
|---|---|---|
| 会话落到哪个实例 | 6 个请求在两个实例间**3:3 散列**，`aff=-`、`prio=None` | 同一会话被钉住：`aff=fresh` 后 **`aff=hit` ×3**（连续 4 个请求）；8 轮序列里**连续 7 次亲和命中**；无 hint 对照组一条亲和日志都没有 |
| 前缀缓存命中率 | 冷前缀：**0.0%** | 同实例暖前缀：**84–99%** |
| 约 3k token 前缀的 p50 TTFT | 2.079s | **1.598s → 1.30×（省约 0.48s）** |

以上收益的代价：**一个 header**，不改请求体，无依赖，默认关闭。真机抓取 9 条 agent 真实请求 ——
header 解码 0 失败，且没有任何一个请求体携带 hint 字段。

**完整证据、测量方法与如实列出的局限 →**
**[`docs/benchmark-report.zh-CN.md`](docs/benchmark-report.zh-CN.md)** ·
**[`docs/benchmark-report.md`](docs/benchmark-report.md)**

## 安装

```bash
hermes plugins install dachaimahogany/hermes-agent-hint   # 或 hermes plugins install <仓库地址>
```

也可以直接把目录放进 `$HERMES_HOME/plugins/agent-hint/`（按 profile 生效）再启用：

```bash
hermes plugins enable agent-hint
# 若你的环境里 enable 命令不可用，可直接在 config.yaml 里写清单：
#   plugins:
#     enabled: ["agent-hint"]
```

> **装上不等于打开。** 两个开关必须同时为开，否则中间件直接返回、一个 header 都不会发：
>
> 1. 插件已启用（`plugins.enabled` / `hermes plugins enable agent-hint`）；
> 2. 插件自己的总开关：`settings.enabled: true`，或环境变量 `AGENT_HINT_ENABLED=1`。

## 配置

配置位于 `config.yaml` 的 `plugins.entries.agent-hint.settings`；每个键也可用 `AGENT_HINT_<KEY>`
环境变量设置。优先级：**settings > 环境变量 > 内置默认值**。所有键都是可选的 —— 默认值即安全值。

| 键 | 默认 | 含义 |
|---|---|---|
| `enabled` | `false` | 总开关。关闭 = 不发 header、不发 manage 请求。 |
| `priority_level` | `3` | `priority_control.priority.level`，折叠进二值域：`>= 3` 记为 `3`（有人正在等），其余记为 `1`。`<= 0` 则整段不发射。 |
| `cache_pin_end` | `auto` | `cache_control.msg_offset`。`auto`（默认）= 本请求的线格式消息条数（含 system）；`0` 不发声明；填整数则显式指定位置（越界即不发）。 |
| `cache_ttl_s` | `auto` | `cache_control.ttl_s`。`auto` 按观测到的间隔推导（无观测则 300s），乘 2 后钳到 60–3600；`0` 不再发送任何声明；填数字则固定。 |
| `epoch_on` | `summarize,micro_summarize,provider_native` | 哪些 `post_compaction` 的 kind 算「上下文重写」、要抬 `session.epoch`：宿主的闭合集（`summarize`、`micro_summarize`、`prune_tool_results`、`provider_native`）里挑，或写 `all` / `none`。`prune_tool_results` 只裁短工具结果、消息条数不变，默认不算。 |
| `manage_sink` | `none` | manage 请求的通道：`ndjson`、`http`、`none`。 |
| `manage_url` | *(空)* | `http` 通道的目标地址。留空 = 完全不产生网络流量。 |
| `manage_model` | *(空)* | 占位体里的 `model`。留空则整个键省略。 |
| `manage_token_env` | `AGENT_HINT_TOKEN` | 存放 `http` 通道 bearer token 的环境变量名。token 只进 header，绝不进 hint。 |
| `manage_timeout_s` | `5` | 单次请求超时，钳在 1–30。 |
| `manage_max_retries` | `1` | 单次 manage 请求的重试次数，钳在 0–3。 |
| `manage_queue_limit` | `64` | `http` 通道的有界队列深度，满则丢最新的，钳在 1–1024。 |

示例：

```yaml
plugins:
  entries:
    agent-hint:
      settings:
        enabled: true
        priority_level: 3
        cache_ttl_s: auto
        epoch_on: summarize,micro_summarize,provider_native
        manage_sink: http
        manage_url: http://127.0.0.1:8080/v1/chat/completions
        manage_model: Qwen3-8B
```

## `X-Agent-Hint` 头

`base64url(UTF-8 JSON)`，去掉 padding —— header 值必须保持 ASCII 与单行。**只有四块**：

| 块 | 字段 | 说明 |
|---|---|---|
| `session` | `session_id`、`state`、`call_index`、`epoch`，可选 `parent_session_id` | `state` 取 `start` / `append` / `pause` / `resume` / `stop` / `compact`。`start` 在每个新会话里只出现一次；其余普通请求一律 `append`。上下文被重写时 `epoch` 递增 —— 由宿主的 `post_compaction` 钩子驱动，口径见 `epoch_on`。 |
| `context_management` | `manage_request`、`edits[]` | **只有 manage 请求**带这一块；每条 edit 只写 `type` 与 `target`，`target` 恒为 `session`（版本号在 `session.epoch` 里，不在 edit 里）。 |
| `priority_control` | `priority.level`、`priority.reason` | `level` 取 `3`（有人正在等）或 `1`（后台）。`reason` 由 `level` **推导**，不单独配置 —— 二者不可能互相矛盾。 |
| `cache_control` | `type`、`ttl_s`、`msg_offset` | 单一对象，`type` 恒为 `ephemeral`。`msg_offset` 是本请求的线格式消息条数（system 提示词算第 1 条）。 |

`ttl_s = clamp(round(上次观测间隔 × 2), 60, 3600)` —— 声明你即将复用的那段保留，而不是一个拍死的猜值。
以下四种轮次**不发**这条声明：上下文被重写的那一轮、模型或工具集发生变化的那一轮、前缀不足一个缓存块的
请求、以及带空 assistant 消息的请求。

**有意不发射**：`agent_status`、`prediction`、`derivation`、`agent_type`、`strict_priority`、
`cache_control[].range`/`unit`、`latency_control`。插件内部状态（`running` / `tool` / `pause` /
`finalize`）永不上线 —— 引擎不需要知道 agent 此刻在干什么，只需要知道这一轮之后要不要卸载上下文。
客户端插件也不应有能力声明硬优先级，信任决策属于控制面。

辅助调用（标题生成、为别的目的要的摘要、任何带非空 `purpose` 的调用）**完全不带 hint**。

### manage 请求

`pause` / `resume` / `stop` / `compact` 各由**一次**专用请求承载：`POST <manage_url>`，空占位体
（`{"model": …, "messages": [], "stream": false}`），动词放在 header 里。消费侧在
`context_management.manage_request` 为真时**必须接受空的 `messages` 列表**。

| 动词 | `edits[0].type` | 何时发 |
|---|---|---|
| `pause` | `offload` | 弹出审批、这一轮被人阻塞 |
| `resume` | `prefetch` | 审批得到答复 |
| `stop` | `evict` | 会话 finalize |
| `compact` | `evict` | 上下文被压缩；hint 携带 `epoch - 1`，即被丢掉的那一代 |

manage 请求按会话去重（连续同一个动词不带来新信息）、不计入 `call_index`，并与其他一切共用同一条
故障安全的出口。

## 安全属性

- **默认关闭。** 未启用前不注入任何内容、不外发任何数据。
- **不配 URL 就无网络。** `manage_sink` 默认 `none`；`ndjson` 只写本地，`http` 只发往 `manage_url`。
- **token 不进 hint。** bearer token 放在 manage 请求的 `Authorization` 头里；hint 本身按「引擎可见的
  日志材料」对待。
- **故障安全。** 每个 hook 与每个 sink 都自吞异常（只向 stderr 警告一次）—— 坏掉的 hint 永远无法
  破坏一次对话；中间件在任何失败情况下原样返回请求。
- **纯标准库，不碰内核。** 仅使用 `ctx.register_hook` / `ctx.register_middleware` / `ctx.get_config`；
  不导入 Hermes 内部模块，不 patch 任何核心对象。
- **旁观者只做旁观。** `pre_tool_call` 永不返回 block/approve/modify 指令。
- **有意不支持：** `plugins.isolation: host`（每 profile 独立宿主进程）不在覆盖范围 —— 那样中间件就得
  在每次 LLM 调用时把整个请求序列化跨进程传递。本插件面向默认的 `in_process` 隔离模式。

## 兼容性

`requires_hermes: ">=0.21.6"`。已在**未改动**的 Hermes v0.21.6 检出上验证 —— 插件只使用公开且
有文档的接口（`VALID_HOOKS` ∩ `llm_request` 中间件），无需任何内核补丁。

hook 载荷按关键字防御式读取；若某 hook 的载荷结构变化，会退化为「不发事件」而不是报错。

## 测试

```bash
python3 -m pytest tests/     # 85 个用例，无网络，无需安装 Hermes
```

## 验证

除单元测试外，有三项独立结果支撑本插件（均在**未改动**的 Hermes v0.21.6 检出上完成）：

1. **上游校验闸门** —— `hermes plugins validate .` 全项通过：manifest 字段、`requires_hermes`、
   `config_schema`、能力探针（隔离运行 `register()`，声明的 hooks/中间件与实际注册一致）、安全扫描、
   以及「未覆盖内核」。
2. **独立插件跑真实对话 + 真实引擎** —— 作为**唯一**插件装入干净的 `HERMES_HOME`（无 bundle、无配套
   插件），一次真实的 `hermes -z` 对话产生了两个 `X-Agent-Hint` 头（`state=start` 后 `append`，
   `call_index` 1→2，`cache_control.msg_offset` 跟随线格式消息条数），并在 finalize 时发出一次 `stop`
   manage 请求；关闭插件后同一次运行完全静默。随后把本插件自己的 `_builder`/`_codec` 产出的 header
   字节发给 MindIE-Motor 调度器，其日志显示会话被钉住 —— 首个请求 `prio=3 aff=fresh`、第二个
   `prio=3 aff=hit` 并打出 `session affinity hit session_id=… instance_id=4`，而同样内容不带 header
   的请求记为 `prio=None aff=-`。
3. **benchmark + 真机验证** —— 前缀缓存冷暖对照、hint 关/开的路由对照、以及 9 条真实 agent 请求抓取，
   数字见[报告](docs/benchmark-report.zh-CN.md)。

## 许可

MIT —— 见 [`LICENSE`](LICENSE)。
