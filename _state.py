"""每会话进程内状态（唯一可变状态，全程加锁）。

结构：session_id -> {call_index, last_api_request_id, epoch, state, state_since_ms,
inflight:set, parent_session_id, child_role}，OrderedDict + 上限 256（LRU 淘汰）。
子 Agent 父映射另存 child_session_id -> (parent_session_id, child_role)。
"""
import threading
import time
from collections import OrderedDict

_MAX_SESSIONS = 256

_lock = threading.Lock()
_sessions = OrderedDict()
_subagent = {}  # child_session_id -> (parent_session_id, child_role)
_watchdogs = {}  # session_id -> threading.Timer
_epoch_provider = None  # 可插接缝：callable(session_id) -> epoch int；None = 内部计数器（T4）


def _now_ms():
    return int(time.time() * 1000)


def _new():
    return {
        "call_index": 0,
        "last_api_request_id": "",
        "epoch": 0,
        "state": "running",
        "state_since_ms": _now_ms(),
        "inflight": set(),
        "parent_session_id": None,
        "child_role": None,
        # 放行边：下一条请求头要带一次 resume（STATE_DESIGN §4.3「只描述一条请求」）
        "pending_resume": False,
        "pending_resume_since": None,
        # 终态是否已经随某条请求头发出去过；false = 下一条请求头带 finalize
        "finalize_served": False,
        # 上一次 note_call 的返回：同一次逻辑调用重试时原样复用，不二次消耗一次性边
        "last_reply": None,
    }


def _reset():
    """测试/回收辅助：清空全部进程内状态。"""
    global _epoch_provider
    with _lock:
        _sessions.clear()
        _subagent.clear()
        _epoch_provider = None
        for t in list(_watchdogs.values()):
            t.cancel()
        _watchdogs.clear()


def _purge_subagent_refs(sid):
    """删除 sid 作为 child 的父映射 + sid 作为 parent 的 children 映射（调用处持锁，review A2）。"""
    _subagent.pop(sid, None)
    for child in [c for c, p in _subagent.items() if p[0] == sid]:
        _subagent.pop(child, None)


def _current_epoch(sid, s):
    """epoch 接缝：provider 提供则用之，否则内部计数器（默认恒 0，T4）。
    provider 应为纯函数、不得重入 _state（调用处持锁）。"""
    if _epoch_provider is not None:
        try:
            return int(_epoch_provider(sid))
        except Exception:
            pass  # provider 异常回退内部计数器，不拖垮请求
    return s["epoch"]


def set_epoch_provider(fn):
    """注入 epoch 推导函数（T4 接缝）；传 None 恢复内部计数器。"""
    global _epoch_provider
    with _lock:
        _epoch_provider = fn


def bump_epoch(sid):
    """上下文重写/压缩-fork 时递增会话 epoch（零侵入触发暂缺，留接缝，T4）。返回新 epoch。"""
    with _lock:
        s = _sessions.get(sid)
        if s is None:
            return 0
        s["epoch"] += 1
        return s["epoch"]


def _touch(sid):
    s = _sessions.get(sid)
    if s is None:
        if len(_sessions) >= _MAX_SESSIONS:
            evicted_sid, _ = _sessions.popitem(last=False)  # LRU 淘汰最旧
            _purge_subagent_refs(evicted_sid)  # 随 LRU 一并清 _subagent（review A2）
        s = _new()
        _sessions[sid] = s
    else:
        _sessions.move_to_end(sid)
    return s


def note_call(sid, api_request_id=""):
    """登记一次（可能重试的）LLM 请求。返回 (call_index, state, since_ms, epoch, resumed)。

    两条一次性边（STATE_DESIGN §4.3 / §4.4），都靠「挂在下一条请求上」实现，因为
    ``resume`` / ``finalize`` 描述的是**一次边**而不是一段持续状态：

    - 放行（``mark_resume``）之后的第一条请求头带 ``resume``，其后回到 ``running``；
    - 停在 ``finalize``（上一 turn 已结束）的第一条请求头带 ``finalize``，其后的请求
      才算新一轮：回 ``running`` 且 ``resumed=True``（中间件据此补发 running 事件，M4）。

    同 api_request_id 重试原样返回上一次的结论：call_index 不虚增（R2），也不把
    一次性边二次消耗掉（否则重试会先吃掉 resume，真正的下一条请求看不到它）。
    """
    with _lock:
        s = _touch(sid)
        if (api_request_id and api_request_id == s["last_api_request_id"]
                and s["last_reply"] is not None):
            return s["last_reply"]  # 同一逻辑调用的重试/回退：结论不变
        resumed = False
        state, since = s["state"], s["state_since_ms"]
        if s["pending_resume"]:
            # 放行边只描述紧跟着的那一条请求
            s["pending_resume"] = False
            state = "resume"
            since = s["pending_resume_since"] or since
            s["pending_resume_since"] = None
        elif state == "finalize":
            if not s["finalize_served"]:
                s["finalize_served"] = True  # 本次请求携带 finalize
            else:  # 终态已发出过一次：这条是新的一轮
                s["state"] = "running"
                s["state_since_ms"] = _now_ms()
                state, since = s["state"], s["state_since_ms"]
                resumed = True
        if api_request_id and api_request_id == s["last_api_request_id"]:
            pass  # 同一次逻辑调用重试，call_index 不涨
        else:
            s["call_index"] += 1
            s["last_api_request_id"] = api_request_id or ""
        reply = (s["call_index"], state, since, _current_epoch(sid, s), resumed)
        s["last_reply"] = reply
        return reply


def set_state(sid, state):
    """状态切换；仅变化时更新 state_since_ms。返回当前 state_since_ms。

    进入 ``pause`` / ``finalize`` 会作废尚未被请求消费的放行边——新一轮挂起或结束
    覆盖上一轮的「刚放行」。任何一次切换都重置 ``finalize_served``：终态可以随下一条
    请求头再发一次。
    """
    with _lock:
        s = _touch(sid)
        if s["state"] != state:
            s["state"] = state
            s["state_since_ms"] = _now_ms()
        if state in ("pause", "finalize"):
            s["pending_resume"] = False
            s["pending_resume_since"] = None
        s["finalize_served"] = False
        return s["state_since_ms"]


def mark_resume(sid, since_ms=None):
    """审批/挂起放行：标记**下一条**请求头带一次 ``resume``，其后回 ``running``。

    ``resume`` 携带的是"刚刚被放行"这个一次性事件，不能作为持续状态挂在会话上，
    否则后面每条请求都会报 resume。返回该边的进入时刻（放行时刻），它同时就是
    那条请求头里的 ``state_since_ms``——消费侧据此知道上下文从哪一刻起值得保留。
    """
    with _lock:
        s = _touch(sid)
        s["pending_resume"] = True
        s["pending_resume_since"] = int(since_ms) if since_ms else _now_ms()
        return s["pending_resume_since"]


def tool_started(sid, call_id):
    """工具在飞 +1；从非 tool 切入时更新 since。返回 since_ms。"""
    with _lock:
        s = _touch(sid)
        if call_id:
            s["inflight"].add(call_id)
        if s["state"] != "tool":
            s["state"] = "tool"
            s["state_since_ms"] = _now_ms()
        return s["state_since_ms"]


def tool_finished(sid, call_id):
    """工具在飞 -1；集合清空才回 running（R3，并行工具批安全）。返回新 since_ms 或 None。"""
    with _lock:
        s = _touch(sid)
        if call_id:
            s["inflight"].discard(call_id)
        if s["inflight"]:
            return None
        if s["state"] != "running":
            s["state"] = "running"
            s["state_since_ms"] = _now_ms()
        return s["state_since_ms"]


def clear_inflight(sid):
    """turn 结束兜底：清在飞集合（防丢 post 导致卡 tool）。"""
    with _lock:
        s = _sessions.get(sid)
        if s is not None:
            s["inflight"].clear()


def drop(sid):
    """会话真关闭回收（on_session_finalize）。"""
    with _lock:
        _sessions.pop(sid, None)
        _purge_subagent_refs(sid)  # 清 sid 自身父映射 + 其 children 映射（review A2）
        t = _watchdogs.pop(sid, None)
        if t is not None:
            t.cancel()


def agent_type(sid, default):
    """子 Agent 用 child_role 覆盖 agent_type；否则用配置默认。"""
    with _lock:
        entry = _subagent.get(sid)
        return entry[1] if entry and entry[1] else default


def lineage(sid):
    """返回 (parent_session_id, derivation)。

    优先级：原生 ``subagent_start`` 记录（spawn）> 部署方声明（fork 或 spawn）> 根会话 (None, None)。
    Hermes 没有 fork 事件，所以一条 fork 血缘只能由创建该会话的一方声明出来。
    """
    with _lock:
        entry = _subagent.get(sid)
        if entry and entry[0]:
            return (entry[0], {"relation": "spawn", "join": "sync"})
    from . import _settings  # 局部导入：_settings 不依赖本模块，避免加载期循环
    declared = _settings.lineage()
    if not declared:
        return (None, None)
    derivation = {"relation": declared["relation"]}
    if declared["relation"] == "spawn":
        derivation["join"] = "sync"
    elif declared["relation"] == "fork":
        if declared.get("fork_point") is not None:
            derivation["fork_point"] = declared["fork_point"]
    return (declared.get("parent_session_id"), derivation)


def subagent_start(parent_session_id, child_session_id, child_role):
    """记录子 Agent 父映射；child_session_id 为空 -> 防御跳过（M6）。"""
    if not child_session_id:
        return
    with _lock:
        _subagent[child_session_id] = (parent_session_id, child_role)
        _touch(child_session_id)


def subagent_stop(child_session_id):
    """清理子 Agent 父映射。"""
    if not child_session_id:
        return
    with _lock:
        _subagent.pop(child_session_id, None)


def start_suspend_watchdog(sid, seconds):
    """审批等待超阈 -> suspend（默认关；seconds<=0 不启用，R16）。"""
    if seconds <= 0:
        return

    def _fire():
        with _lock:
            s = _sessions.get(sid)
            if s is None or s["state"] != "pause":
                return
            s["state"] = "suspend"
            s["state_since_ms"] = _now_ms()
            since = s["state_since_ms"]
        # 锁外发事件，避免死锁（延迟导入避免模块加载期循环依赖）
        try:
            from . import _builder, _sink
            _sink.emit(_builder.status_event(sid, "suspend", since_ms=since, reason="checkpoint"))
        except Exception:
            pass

    with _lock:
        t = _watchdogs.pop(sid, None)
        if t is not None:
            t.cancel()
        t = threading.Timer(seconds, _fire)
        t.daemon = True
        _watchdogs[sid] = t
    t.start()


def cancel_suspend_watchdog(sid):
    with _lock:
        t = _watchdogs.pop(sid, None)
    if t is not None:
        t.cancel()
