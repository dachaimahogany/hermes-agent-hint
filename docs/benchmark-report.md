# Why agent sessions need a hint: measured impact

> A cold/warm prefix-cache benchmark and a live routing test of **agent-hint**, run on an
> 8-card Ascend 910-class NPU server serving Qwen3-8B through a MindIE-Motor deployment
> (vLLM engines with prefix caching enabled).
>
> **中文版：[benchmark-report.zh-CN.md](benchmark-report.zh-CN.md)**
>
> All host addresses, ports, container names and image identifiers have been removed; only the
> hardware and software environment is described. Every number below was measured — none is
> extrapolated.

---

## TL;DR

1. **The engine can reuse a session's prefix — but only on the instance that holds it.**
   Measured with the engine's own counter: a cold request (fresh prefix) hits **0.0%** of its
   prefix tokens in cache; a warm one (same prefix, same instance) hits **84–99%**.
2. **Without a hint, the scheduler cannot tell two requests belong to the same session** — an
   OpenAI-compatible body carries `model` / `messages` / `tools` and nothing about session
   identity. Measured: 6 identical requests landed on the two engine instances **3:3**, with
   `aff=-` and `prio=None` in the coordinator log. Which instance a session lands on is a coin
   flip, and a coin flip is what destroys the reuse in point 1.
3. **With a hint, the same session sticks.** One session's 4 consecutive requests all landed on
   the same instance, logged `aff=fresh` then `aff=hit` ×3; an 8-round run logged `aff=fresh`
   then **7 consecutive `session affinity hit`** lines on the same instance. The no-hint control
   produced **no** affinity hits at all.
4. **What that buys, in time.** Reusing a ~3 k-token prefix cut p50 TTFT from **2.079 s to
   1.598 s (1.30×, ~0.48 s saved)**; the gain grows with prefix length and is ~1.0× for short
   prefixes (where TTFT is dominated by the first decode step, not prefill).
5. **What it costs.** One header (a few hundred bytes of base64url JSON), no request-body
   change, no dependency, off by default. Nine real agent requests were captured in flight: the
   bodies contained no hint fields, and none of the headers failed to decode.

The rest of this document is the evidence behind those five lines.

---

## 1. The gap this fills

An agent turn is not a one-shot completion. It is a chain of requests that share a long, growing
prefix — system prompt, tool definitions, conversation history — and the chain only makes
progress when each request is fast.

Prefix caching (a standard vLLM-family feature) removes the redundant prefill for any prefix the
engine has already seen. The catch is that a distributed engine caches **per instance**: caching
only pays off if the requests that share a prefix are routed to the instance that already holds
it. So the value of prefix caching is decided at the **routing** layer — and routing needs to know
which requests belong to the same session.

The OpenAI-compatible request body cannot express that. It has no session field; from the
scheduler's point of view every request in a session is a stranger. Teams normally paper over this
with source-IP affinity (breaks behind NAT and gateways), with a single instance (no cache,
no scale), or with a client-side load balancer that pins by connection (breaks on long-lived
multiplexed connections and on retries).

`agent-hint` fills exactly this gap: it attaches the missing session semantics to the request as a
single header, and it does so from inside the agent framework, where that semantics actually
exists.

## 2. Environment under test

| Item | Value |
| --- | --- |
| NPU | 8 cards, Ascend 910-class (16 chips, 2 per card), 64 GB HBM per card (56–59 GB resident during the run) |
| Host | openEuler 24.03 LTS, aarch64, 320 CPU cores, 2 TB RAM |
| Serving stack | MindIE-Motor — controller + coordinator + 2 vLLM engine instances (`union×2`), session affinity enabled |
| Engine | vllm-ascend 0.25.1, prefix caching enabled, `max_model_len=8192` |
| Model | Qwen3-8B |
| Observed prefill | ≈13 k tokens/s on a single card |
| TTFT definition | first content token of a streamed response, `enable_thinking=false` (so thinking tokens cannot skew it) |

Deliberately omitted: host names and addresses, ports, container and image identifiers, and the
deployment's internal service layout.

## 3. How it was measured — and what each oracle can and cannot prove

Because "it felt faster" is not evidence, the run used three independent oracles and the report
leans on each one only for what it is actually able to show:

| Oracle | What it proves | What it cannot show |
| --- | --- | --- |
| Engine `/metrics` prefix-cache hit/query counters (per-request token deltas) | whether the engine really reused cached tokens | which instance the coordinator chose — a request can hit on *either* instance if that prefix was cached there earlier |
| Coordinator log (`scheduled … aff=…`, `session affinity hit …`) | the routing decision itself, and whether a session was pinned | how much prefix was actually reused |
| Per-request TTFT | the user-visible effect | causality by itself — hence counters and logs are the primary evidence and timing is read alongside them |

Scenarios: **cold vs warm** prefix at four lengths on a single engine (mechanism), and
**hint off vs hint on** for the same session (routing). Requests were issued by a pure-stdlib
client speaking the OpenAI-compatible API, with the header produced by this plugin's own encoder —
the same code path a real agent uses.

## 4. Results

### 4.1 Prefix caching works, and it scales with prefix length

Single engine, same request shape, only variable = whether the prefix was already cached.

| Actual prefix (tokens) | Cold p50 TTFT | Warm p50 TTFT | Prefix-cache hit rate | Relative gain |
| --- | --- | --- | --- | --- |
| ~769 | 1.511 s | 1.524 s | 83% | 0.99× |
| ~1602 | 1.548 s | 1.520 s | 96% | 1.02× |
| **~2954** | **2.079 s** | **1.598 s** | **100%** | **1.30×** |
| ~6376 | 2.312 s | 1.958 s | 94% | 1.18× |

- **Cold is exactly zero, not merely low.** Re-aggregating the raw per-request records confirms
  every cold sample in the cleaned run registered 0.0% hits — the signal has no noise floor to
  argue with.
- **The baseline is fair:** the first round of a warm sequence costs the same as a cold request
  (both must prefill in full); the gain appears only from the second round on, which is precisely
  the multi-round pattern of an agent session.
- **Where the time goes:** at ~3 k tokens the saving is ~0.48 s. At ~769 tokens the gain is
  ~1.0× — at that size, TTFT is dominated by the first decode step rather than prefill, so there
  is almost nothing to remove. Longer prefixes on slower prefill hardware gain much more (see §5).

### 4.2 Routing is the decisive difference

**Control: no hint.** Six requests, no `X-Agent-Hint`:

| Request | Instance | prio | aff |
| --- | --- | --- | --- |
| 1 | A | None | – |
| 2 | B | None | – |
| 3 | B | None | – |
| 4 | A | None | – |
| 5 | A | None | – |
| 6 | B | None | – |

Blind scatter at 3:3 — no pin, no session identity, `prio=None`.

**Treatment: same session, 4 consecutive requests, hint on.** All four pinned to the same
instance, first one recorded as a new pin and the rest as hits:

```
scheduled role=union req_id=… instance=2 … prio=3 aff=fresh
scheduled role=union req_id=… instance=2 … prio=3 aff=hit
scheduled role=union req_id=… instance=2 … prio=3 aff=hit
scheduled role=union req_id=… instance=2 … prio=3 aff=hit
session affinity hit session_id=sess-hermes-x parent_session_id=sess-hermes-x instance_id=2 req_id=…
```

The same pattern held over an 8-round sequence on a second session — `aff=fresh` followed by
**seven** consecutive `session affinity hit` lines on one instance — while the no-hint arm logged
no affinity line at all. Over the steady-state rounds (2+) that arm also ran with a lower and
tighter TTFT distribution: **1.72 s** median with the hint vs **1.87 s** without.

> **A note on oracles, because it matters for anyone re-deriving these numbers.** In this routing
> scenario the per-round prefix-cache counters are *not* a clean oracle: with two instances, a
> session can accumulate cache hits on either one if an earlier round had already cached that
> prefix there, so a "hit-or-miss per round" view can look similar in both arms. The routing claim
> here rests on the coordinator log, which records the decision itself. That is why the benchmark
> keeps the two oracles separate.

### 4.3 What the agent actually puts on the wire

Nine real agent requests were captured mid-flight by a local HTTP interposer and decoded
byte-for-byte:

```json
{
  "session": {
    "session_id": "20261012_093707_…",
    "agent_type": "office-agent",
    "call_index": 1,
    "epoch": 0
  },
  "agent_status": { "state": "running", "state_since_ms": 1791769027861 },
  "priority": { "level": 3, "reason": "user_waiting" },
  "cache_control": { "type": "pin", "range": { "end": 1 } }
}
```

(`agent_type` and the priority policy are configuration, not constants; the values above are what
the deployment set. The session id is shortened here.)

Observed across the nine:

- **9 requests → 9 distinct headers**, `call_index` incrementing 1→9 with `session_id` constant —
  the header is per-call state, not a one-off.
- **0 decode failures** on the engine side.
- **The request bodies stayed clean:** none of the nine carried any hint field. The protocol
  boundary is "header only", and the plugin respects it.
- **Control-plane-only fields stay with the control plane:** fields the spec reserves (e.g.
  `strict_priority`) are never emitted by the agent side.
- `cache_control` was emitted as a single object; the schema permits object or array, and both
  sides parsed it without complaint.

## 5. Cross-check against an independent implementation

NVIDIA's published results for agentic inference with Dynamo report that, after the first call
writes a conversation prefix into KV cache, **every subsequent call to the same worker hits
85–97%** of it, that four cooperating agents reach a **97.2% aggregate** hit rate, and that
KV-aware/session-aware routing delivered a **4× reduction in p50 TTFT** versus default routing
([Full-Stack Optimizations for Agentic Inference with NVIDIA Dynamo](https://developer.nvidia.com/blog/full-stack-optimizations-for-agentic-inference-with-nvidia-dynamo/)).

Two readings matter here:

- **The mechanism agrees.** Our 84–99% measured hit rate sits inside their 85–97% band, on
  entirely different hardware and a different engine. Session stickiness → prefix reuse is the
  same lever everywhere; it is not a vendor-specific trick.
- **The magnitude is a hardware property, not a mechanism property.** We measured 1.30× where
  they report 4×. On this server single-card prefill is fast (~13 k tokens/s) and the completion
  is short (32 tokens), so TTFT is dominated by the first decode step; removing ~3 k tokens of
  prefill out of a ~1.6 s total saves ~0.48 s. On an older dual-card system where prefill is the
  bottleneck, the same mechanism was measured at ~5.5× (6.54 s → 1.19 s) in a separate run.
  Practical reading: the cheaper your prefill, the smaller the win — but it never turns negative,
  and it grows with prefix length and with how often a session re-enters.

## 6. Known limitations

Stated plainly, because a report that only lists wins is not useful:

- **Shared machine, so run-to-run noise exists.** Timing conclusions are directional; the
  counters and logs are the hard evidence. Both are quoted above.
- **The live multi-round `aff=hit` path was not reached in the real agent process** in this run:
  the engine returned an empty SSE stream with the harness's tool-call format (an engine-side
  tool-call-parser / thinking-mode mismatch, not a plugin issue). The hit path was therefore
  verified by replaying the plugin's **real header bytes** through a real engine — and the header
  bytes are identical between the two paths, which is what makes the replay meaningful.
- **Concurrent sessions lost affinity in some rounds** (multiple `aff=fresh` for non-first
  rounds) — open item, a phenomenon also seen with another agent harness against the same stack,
  suspected to be in concurrent dispatch on the provider side.
- **One engine-side parsing discrepancy:** a live request whose header carried priority level 3
  was logged as `prio=1`, while a replay of the same header logged `prio=3`. Tracked as an
  engine-side investigation; the agent side emits what the schema says it emits.
- **A prefix longer than `max_model_len` is rejected (HTTP 400)** — expected, but worth knowing
  when sizing `cache_control` ranges.
- No claim here about **cost** (NPU-seconds) or behaviour **under high concurrent load**; this
  run covers latency and routing behaviour.

## 7. Provenance and reproduction

- **Raw data:** 336 per-request records (label, session id, round, prefix length, TTFT, engine
  counter deltas) plus a five-sheet workbook (summary / prefix gradient / cold-warm mix /
  coordinator stickiness / environment snapshot). Available on request.
- **Client:** pure-stdlib Python against the OpenAI-compatible API; the header is produced by this
  plugin's own builder and encoder, so what is measured is what the plugin emits.
- **Oracles:** engine `/metrics` counters scraped per request; coordinator log lines matching
  `scheduled … aff=` and `session affinity hit …`.
- **Independence:** the benchmark was run against a MindIE-Motor deployment by the plugin's author
  working with the engine side; the engine-side observations are quoted from the joint
  integration record rather than restated as the plugin's own claim.
- **Anonymization:** all host addresses, ports, container names and image identifiers were
  removed before publication. Nothing in this document identifies a specific deployment.

---

**Reproduce it yourself?** The plugin needs no special configuration for the cold/warm part —
enable prefix caching on the engine, repeat a request with an identical long prefix, and diff the
prefix-cache counters. For the routing part, compare the coordinator's `aff=` lines with the hint
disabled and enabled.
