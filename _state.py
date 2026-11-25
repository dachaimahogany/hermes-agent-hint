"""每会话进程内状态（唯一可变状态，全程加锁）。

结构：``session_id -> {call_index, last_api_request_id, epoch, state, state_since_ms,
inflight:set, signature, rewrite_pending}``，OrderedDict + 上限 256（LRU 淘汰）。
子 Agent 只记 ``child_session_id -> parent_session_id``：``relation`` / ``join`` 这些
描述分叉方式的字段已从线格式移除，子会话需要的只有“我爹是谁”。

这里的 ``state`` 是**进程内私有词汇**（``running`` / ``tool`` / ``pause`` / ``finalize``），
不上线。线格式只认 ``start`` / ``append`` 与四个 manage 动词，二者的映射在
``_builder`` 里完成——本地状态机不需要为了迁就线上词汇而改名。
"""
import threading
import time
from collections import OrderedDict

_MAX_SESSIONS = 256

_lock = threading.Lock()
_sessions = OrderedDict()
_subagent = {}  # child_session_id -> parent_session_id
_epoch_provider = None  # 可插接缝：callable(session_id) -> epoch int；None = 内部计数器


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
        "signature": None,
        "rewrite_pending": False,
    }


def _reset():
    """测试/回收辅助：清空全部进程内状态。"""
    global _epoch_provider
    with _lock:
        _sessions.clear()
        _subagent.clear()
        _epoch_provider = None


def _purge_subagent_refs(sid):
    """删除 sid 作为 child 的父映射 + sid 作为 parent 的 children 映射（调用处持锁）。"""
    _subagent.pop(sid, None)
    for child in [c for c, p in _subagent.items() if p == sid]:
        _subagent.pop(child, None)


def _current_epoch(sid, s):
    """epoch 接缝：provider 提供则用之，否则内部计数器（默认恒 0）。

    provider 应为纯函数、不得重入 ``_state``（调用处持锁）。
    """
    if _epoch_provider is not None:
        try:
            return int(_epoch_provider(sid))
        except Exception:
            pass  # provider 异常回退内部计数器，不拖垮请求
    return s["epoch"]


def set_epoch_provider(fn):
    """注入 epoch 推导函数；传 None 恢复内部计数器。"""
    global _epoch_provider
    with _lock:
        _epoch_provider = fn


def bump_epoch(sid):
    """上下文被重写（压缩 / 分支）时递增 epoch。返回新 epoch。

    同时置 ``rewrite_pending``：紧随其后的那一轮请求跑在重写后的前缀上，此刻不能
    声明缓存——前缀刚刚换了，声明保护的是一个还不存在于引擎里的东西。

    未见过的会话也要能抬代：压缩可能发生在第一条请求之前（宿主在会话开头就先做一次
    摘要），若这里只查不建，那次重写会在 ``note_call`` 建状态时丢掉。
    """
    with _lock:
        s = _touch(sid)
        s["epoch"] += 1
        s["rewrite_pending"] = True
        return s["epoch"]


def consume_rewrite(sid):
    """读取并清除“本轮是重写轮”标记。"""
    with _lock:
        s = _sessions.get(sid)
        if s is None or not s.get("rewrite_pending"):
            return False
        s["rewrite_pending"] = False
        return True


def note_signature(sid, signature):
    """登记本轮 ``model + tools`` 签名；返回它是否与上一轮不同。

    工具集变了就等于前缀变了：引擎的缓存键包含工具定义，签名一换，旧前缀全部作废。
    """
    with _lock:
        s = _touch(sid)
        previous = s["signature"]
        s["signature"] = signature
        return previous is not None and previous != signature


def _touch(sid):
    s = _sessions.get(sid)
    if s is None:
        if len(_sessions) >= _MAX_SESSIONS:
            evicted_sid, _ = _sessions.popitem(last=False)  # LRU 淘汰最旧
            _purge_subagent_refs(evicted_sid)
        s = _new()
        _sessions[sid] = s
    else:
        _sessions.move_to_end(sid)
    return s


def note_call(sid, api_request_id=""):
    """登记一次（可能重试的）LLM 请求。

    返回 ``(call_index, state, since_ms, epoch, resumed)``：

    - 同 ``api_request_id`` 重复 -> ``call_index`` 不涨（重试/回退不虚增）。
    - 若当前停在 ``finalize``（上一 turn 已结束）-> 置 ``running`` 且 ``resumed=True``。
    """
    with _lock:
        s = _touch(sid)
        resumed = False
        if s["state"] == "finalize":
            s["state"] = "running"
            s["state_since_ms"] = _now_ms()
            resumed = True
        if api_request_id and api_request_id == s["last_api_request_id"]:
            pass  # 同一次逻辑调用重试，call_index 不涨
        else:
            s["call_index"] += 1
            s["last_api_request_id"] = api_request_id or ""
        return (s["call_index"], s["state"], s["state_since_ms"], _current_epoch(sid, s), resumed)


def set_state(sid, state):
    """状态切换；仅变化时更新 ``state_since_ms``。返回当前 ``state_since_ms``。"""
    with _lock:
        s = _touch(sid)
        if s["state"] != state:
            s["state"] = state
            s["state_since_ms"] = _now_ms()
        return s["state_since_ms"]


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
    """工具在飞 -1；集合清空才回 running（并行工具批安全）。返回新 since_ms 或 None。"""
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
    """会话真关闭回收（``on_session_finalize``）。"""
    with _lock:
        _sessions.pop(sid, None)
        _purge_subagent_refs(sid)


def lineage(sid):
    """返回 ``parent_session_id``；根会话 -> ``None``。"""
    with _lock:
        return _subagent.get(sid)


def peek(sid):
    """返回 ``(call_index, epoch, parent_session_id)``，不改变任何状态。

    manage 请求要带着“当前处在第几次调用、哪一代上下文”，但它自己不是一次 LLM 调用，
    因此只能读，不能 ``note_call``——否则每发一次 ``pause`` 都会把 ``call_index`` 推高。
    """
    with _lock:
        s = _sessions.get(sid)
        if s is None:
            return (0, 0, _subagent.get(sid))
        return (s["call_index"], _current_epoch(sid, s), _subagent.get(sid))


def subagent_start(parent_session_id, child_session_id):
    """记录子 Agent 父映射；``child_session_id`` 为空 -> 防御跳过。"""
    if not child_session_id:
        return
    with _lock:
        _subagent[child_session_id] = parent_session_id or None
        _touch(child_session_id)


def subagent_stop(child_session_id):
    """清理子 Agent 父映射。"""
    if not child_session_id:
        return
    with _lock:
        _subagent.pop(child_session_id, None)
