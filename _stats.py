"""运行时观测采集（纯进程内内存，不发敏感 trace）。

``post_api_request`` 把 usage 与时长收进来，``snapshot`` 收敛成一个间隔值——它唯一的
消费者是 TTL 推导：``ttl_s = clamp(round(间隔秒 × 2), 60, 3600)``。

``prediction`` 那套字段（``next_action`` / ``remaining_calls`` / ``prob``）已从协议移除：
它们是启发式猜测，被消费侧误读成高置信信号的代价远大于收益，而它们的输入
（``next_action_map``）本质上是一份硬编码的业务 SOP。这里只留可观测、可复现的量。
"""
import threading
import time
from collections import defaultdict, deque

_MAX_SAMPLES = 16

_lock = threading.Lock()  # collect 跑 worker 线程、snapshot 跑 agent-loop，需互斥

_samples = defaultdict(deque)  # session_id -> deque(sample)
_last_ended_at = {}  # session_id -> ended_at（interval 近似用）


def _reset():
    """测试/回收辅助：清空全部统计。"""
    with _lock:
        _samples.clear()
        _last_ended_at.clear()


def collect(session_id=None, usage=None, api_duration=None, first_chunk_at=None,
            started_at=None, ended_at=None, **_kw):
    """``post_api_request`` 观测采集；``usage`` 非 dict（含 None）时跳过，不污染统计。"""
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
        q.append({"output_tokens": output_tokens, "latency_ms": latency_ms})
        while len(q) > _MAX_SAMPLES:
            q.popleft()
        if ended_at is not None:
            _last_ended_at[sid] = ended_at


def _mean(values):
    vals = [v for v in values if v is not None]
    return int(round(sum(vals) / len(vals))) if vals else None


def snapshot(sid):
    """返回 ``{latency_ms, output_tokens, interval_ms}``；无样本 -> ``None``。

    ``interval_ms`` 是“上一次请求结束到现在”的毫秒数，也就是会话这次空闲了多久。
    """
    sid = str(sid or "")
    with _lock:
        q = list(_samples.get(sid) or [])  # 快照：避免与 collect 并发迭代 deque
        last_ended = _last_ended_at.get(sid)
    if not q and last_ended is None:
        return None
    interval = None
    if last_ended is not None:
        interval = int((time.time() - last_ended) * 1000)
        if interval < 0:
            interval = None
    if not q:
        return {"interval_ms": interval} if interval is not None else None
    out = {}
    latency = _mean([s["latency_ms"] for s in q])
    if latency is not None:
        out["latency_ms"] = latency
    out_tokens = _mean([s["output_tokens"] for s in q])
    if out_tokens is not None:
        out["output_tokens"] = out_tokens
    if interval is not None:
        out["interval_ms"] = interval
    return out or None


def interval_ms(sid):
    """只取空闲间隔（TTL 推导的输入）。"""
    snap = snapshot(sid)
    return (snap or {}).get("interval_ms")
