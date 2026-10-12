# agent-hint — a Hermes plugin for inference-engine–aware agent sessions

Give your inference engine (scheduler / gateway / KV-cache manager) a **standard, machine-readable
view of the agent session** driving each request: who the session is, where it is in its lifecycle,
how urgent it is, and what it is likely to do next.

The plugin attaches a single HTTP header — `X-Agent-Hint` — to **main LLM requests only**, and
optionally emits session lifecycle events out-of-band. It never touches the request body, never
modifies Hermes core, and is **off by default**.

```
LLM request ──► llm_request middleware ──► + X-Agent-Hint header ──► inference engine
                                          ▲
   hooks (session / tool / approval / subagent / api) ──► status events (ndjson | http | none)
```

## Why

An OpenAI-compatible HTTP API carries no notion of "session". An agent, however, has one: turn
boundaries, tool calls, approval waits, sub-agents, and a strong affinity to whatever instance holds
its KV cache. Engines that can see that structure can schedule better — pin a session to the
instance that already holds its cache, let a user-blocked session outrank a background one, or hold
capacity for a session that is about to resume.

## Install

```bash
hermes plugins install dachaimahogany/hermes-agent-hint   # or: hermes plugins install <repo-url>
```

Or drop the directory into `$HERMES_HOME/plugins/agent-hint/` (per-profile) and enable it:

```bash
hermes plugins enable agent-hint
```

## Configure

Settings live under `plugins.entries.agent-hint.settings` in `config.yaml`; every key can also be set
via an `AGENT_HINT_<KEY>` environment variable. Precedence: **settings > env > built-in default**.
Every key is optional — the defaults are the safe defaults.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. Off = no header, no events. |
| `agent_type` | `hermes` | Value written to `session.agent_type`. Set per deployment/role. |
| `priority_level` | `3` | `priority.level`; `<= 0` omits the whole `priority` block. |
| `priority_reason` | `user_waiting` | `priority.reason` text. |
| `status_sink` | `none` | Event transport: `ndjson`, `http`, or `none`. |
| `status_url` | *(empty)* | Target URL for the `http` sink. Empty = no network traffic at all. |
| `finalize_on` | `session_end` | When to emit `finalize`: `session_end` or `session_finalize`. |
| `suspend_after_s` | `0` | Emit `suspend` if an approval stays pending this long. `0` disables. |
| `cache_pin_end` | `1` | `cache_control.pin.range.end`; `0` omits the range. |
| `next_action_map` | *(empty)* | Generic `prediction.next_action` heuristic (JSON). Empty = omit. |

Example:

```yaml
plugins:
  entries:
    agent-hint:
      settings:
        enabled: true
        agent_type: my-agent
        status_sink: http
        status_url: http://127.0.0.1:8080/agent-status
        next_action_map: '{"search_": ["summarize_run", 6, "tool"], "__default__": [null, 0, "tool"]}'
```

## The `X-Agent-Hint` header

`base64url(UTF-8 JSON)`, padding stripped — header values must stay ASCII and single-line. Field
classes carried in the JSON:

| Class | Fields | Notes |
|---|---|---|
| `session` | `session_id`, `agent_type`, `call_index`, `epoch` | Identity + call counter; `epoch` increments when the context is rewritten. |
| `agent_status` | `state` (`running` / `tool` / `pause` / `finalize`), `state_since_ms`, `reason` | Lifecycle, out-of-band from the request path. |
| `priority` | `level`, `reason` | Soft priority hint; the engine decides. |
| `cache_control` | `pin`, `range` | Cache-pinning hint for the session's prefix. |
| `prediction` | `latency_ms`, `output_tokens`, `interval_ms`, `next_action`, `remaining_calls` | Statistics are measured; `next_action`/`remaining_calls` are **heuristics** and are omitted unless `next_action_map` is configured. |

`strict_priority` is deliberately **not** emitted: a client-side plugin must not be able to claim a
hard priority. Trust decisions belong to the control plane.

Status events (same shapes, plus a monotonic `X-Agent-Hint-Seq` on the `http` sink) are emitted for
session start/end, tool start/finish, approval wait/resume, sub-agent start/stop, and finalize.

## Safety properties

- **Off by default.** Nothing is injected and nothing is sent until you enable it.
- **No network unless you point it at a URL.** `status_sink` defaults to `none`; `ndjson` writes
  locally, `http` sends only to `status_url`.
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
python3 -m pytest tests/     # 36 tests, no network, no Hermes install required
```

## Verification

Beyond the unit tests, two independent end-to-end results back this plugin (both on an **unmodified**
Hermes v0.21.6 checkout):

1. **Upstream gate** — `hermes plugins validate .` passes in full: manifest fields, `requires_hermes`,
   `config_schema`, the capability probe (`register()` run in isolation, declared hooks/middleware
   matching registrations), security scan, and "no core override".
2. **Standalone turn + real engine** — installed as the *only* plugin into a clean `HERMES_HOME`
   (no bundle, no companion plugins), a real `hermes -z` turn produced two `X-Agent-Hint` headers
   (`call_index` 1→2) and the event sequence `running → tool → running → finalize`; with the plugin
   disabled the same run was completely silent. The header bytes emitted by this plugin's own
   `_builder`/`_codec` were then sent to a MindIE-Motor scheduler, whose log shows the session being
   pinned — `session affinity hit session_id=… instance_id=4` after a first request with
   `prio=3 aff=fresh` and a second with `prio=3 aff=hit`, while an identical request without the
   header logged `prio=None aff=-`.

## License

MIT — see `LICENSE`.

---

## 中文摘要

这个插件把「agent 会话的结构」用**一个标准 HTTP 头 `X-Agent-Hint`**（base64url JSON）告诉下游推理
引擎：会话身份、生命周期状态（running / tool / pause / finalize）、软优先级、缓存亲和提示，以及
可选的统计与启发式预测；同时可按需把生命周期事件通过 `ndjson` 或 `http` 旁路上报到引擎。

设计要点：**默认关闭**、**不配 `status_url` 就一个字节都不外发**、**任何异常都自吞绝不影响对话**、
**纯标准库且不碰 Hermes 内核**（只用 `ctx.register_hook` / `register_middleware` / `get_config`）。
`strict_priority` 有意不发射 —— 客户端插件不应能声明硬优先级。
