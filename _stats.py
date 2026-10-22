"""prediction 采集/聚合（纯进程内内存，不发敏感 trace）。

数据源：post_api_request 的 usage/时长；按 session 保留最近 N 次，snapshot() 收敛为
单值发送。next_action/remaining_calls 由 `next_action_map` 配置的通用映射给出（未配置则省略），
不属于埋点统计量。
"""
import threading
import time
from collections import defaultdict, deque

_MAX_SAMPLES = 16
_MIN_TOOL_SAMPLES = 3

_lock = threading.Lock()  # collect/note_tool 跑 worker 线程、snapshot 跑 agent-loop，需互斥（review A1）

_samples = defaultdict(deque)  # session_id -> deque(sample)
_last_ended_at = {}  # session_id -> ended_at（interval 近似用）
_last_tool = {}  # session_id -> 最近工具名
_tool_started = {}  # session_id -> 工具开始时刻（monotonic）
_tool_durations = defaultdict(deque)  # 工具名 -> deque(耗时秒)，跨会话共享


def _p90(values):
    """最近样本的 p90；样本不足时返回 None。"""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * 0.9
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _reset():
    """测试/回收辅助：清空全部统计。"""
    with _lock:
        _samples.clear()
        _last_ended_at.clear()
        _last_tool.clear()
        _tool_started.clear()
        _tool_durations.clear()


def note_tool(sid, name):
    """记录最近工具名与开始时刻（pre_tool_call 调用）。"""
    if sid and name:
        with _lock:
            _last_tool[str(sid)] = str(name)
            _tool_started[str(sid)] = time.monotonic()


def note_tool_done(sid):
    """工具结束时结算耗时，按工具名累积（post_tool_call 调用）。

    没有这一步就只能给 ``expected_duration_s`` 填一个常数，而常数会让 TTL 永远
    落在协议下限，工具超过一分钟的等待必然失效。
    """
    key = str(sid or "")
    if not key:
        return
    with _lock:
        started = _tool_started.pop(key, None)
        name = _last_tool.get(key)
        if started is None or not name:
            return
        elapsed = max(0.0, time.monotonic() - started)
        _tool_durations[name].append(elapsed)
        while len(_tool_durations[name]) > _MAX_SAMPLES:
            _tool_durations[name].popleft()


def collect(session_id=None, usage=None, api_duration=None, first_chunk_at=None,
            started_at=None, ended_at=None, **_kw):
    """post_api_request 观测采集；usage 非 dict（含 None）时跳过，不污染统计（M5）。"""
    sid = str(session_id or "")
    if not sid:
        return
    if not isinstance(usage, dict):
        return
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    try:
        output_tokens = int(output_tokens) if output_tokens is not None else None
    except (TypeError, ValueError):
        output_tokens = None
    latency_ms = None
    if first_chunk_at is not None and started_at is not None:
        try:
            latency_ms = int((first_chunk_at - started_at) * 1000)
        except (TypeError, ValueError):
            latency_ms = None
    if latency_ms is None and api_duration is not None:
        try:
            latency_ms = int(float(api_duration) * 1000)
        except (TypeError, ValueError):
            latency_ms = None
    with _lock:
        q = _samples[sid]
        q.append({"output_tokens": output_tokens, "latency_ms": latency_ms, "ended_at": ended_at})
        while len(q) > _MAX_SAMPLES:
            q.popleft()
        if ended_at is not None:
            _last_ended_at[sid] = ended_at


def _mean(values):
    vals = [v for v in values if v is not None]
    return int(round(sum(vals) / len(vals))) if vals else None


def _map_next(last_tool, mapping):
    """由配置映射推下一步与剩余调用数（通用：不硬编码任何业务 SOP）。

    ``mapping`` 形如 ``{"<工具名前缀>": ["<下一步工具名>", <剩余调用数>, "<state>"], ...}``，
    值也接受 dict（``{"tool": ..., "remaining": ..., "state": ...}``）。键按长度降序匹配。
    未配置 / 无匹配且无 ``__default__`` -> ``(None, None, None)``（调用方整段省略）。
    """
    if not mapping:
        return (None, None, None)
    key = None
    if last_tool:
        for prefix in sorted(mapping, key=len, reverse=True):
            if prefix != "__default__" and str(last_tool).startswith(prefix):
                key = prefix
                break
    if key is None and "__default__" in mapping:
        key = "__default__"
    if key is None:
        return (None, None, None)
    entry = mapping.get(key)
    if isinstance(entry, dict):
        return (entry.get("tool"), entry.get("remaining"), entry.get("state"))
    if isinstance(entry, (list, tuple)):
        padded = list(entry) + [None, None, None]
        return (padded[0], padded[1], padded[2] if padded[2] is not None else "tool")
    return (None, None, None)


def snapshot(sid, next_action_map=None):
    """返回 prediction 相关字段；无样本 -> None（调用方整段省略 prediction）。

    ``next_action_map`` 由调用方（agent-loop）用当前配置传入；未配置时
    next_action/remaining_calls 整段省略（启发式必须显式配置，绝不硬编码业务 SOP）。
    """
    sid = str(sid or "")
    with _lock:
        q = list(_samples.get(sid) or [])  # 快照：避免与 collect 并发迭代 deque（review A1）
        last_ended = _last_ended_at.get(sid)
        last_tool = _last_tool.get(sid)
    if not q:
        return None
    latency = _mean([s["latency_ms"] for s in q])
    out_tokens = _mean([s["output_tokens"] for s in q])
    if out_tokens is None:
        out_tokens = q[-1]["output_tokens"]
    interval = None
    if last_ended is not None:
        interval = int((time.time() - last_ended) * 1000)
        if interval < 0:
            interval = None
    pred = {}
    if latency is not None:
        pred["latency_ms"] = latency
    if out_tokens is not None:
        pred["output_tokens"] = out_tokens
    if interval is not None:
        pred["interval_ms"] = interval
    # next_action / remaining_calls 为启发式估计，**由 next_action_map 显式配置**（未配置则整段省略）；
    # 它不是字段定义里的埋点统计量；prob 固定保守 0.5（不随启发式强度变化），
    # 以防网关将启发式猜测误读为高置信信号（调度预取/配额误用）。
    next_name, remaining, next_state = _map_next(last_tool, next_action_map)
    if next_state is not None:
        na = {"state": next_state, "prob": 0.5}
        if next_name:
            with _lock:
                observed = list(_tool_durations.get(next_name) or ())
            p90 = _p90(observed)
            # 有实测就发实测的 p90，样本不足才退回常数 5 秒（保守：宁可短，不虚构长保留）。
            expected = max(1, int(round(p90))) if p90 is not None and len(observed) >= _MIN_TOOL_SAMPLES else 5
            na["tools"] = [{"name": next_name, "expected_duration_s": expected}]
        pred["next_action"] = na
        if remaining is not None:
            pred["remaining_calls"] = int(remaining)
    return pred
