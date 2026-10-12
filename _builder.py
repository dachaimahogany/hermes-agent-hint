"""AgentHint 对象构造（纯函数，无副作用）。

字段语义见 README 的 “The `X-Agent-Hint` header” 小节：
- 根会话不写 parent_session_id / derivation（缺省）。
- 缺省项整段省略（priority / cache_control / prediction / reason / tools）。
- strict_priority 明确不写：由可信控制面注入，普通 Agent 永不主动设置（N1）。
"""


def request_hint(session_id, call_index, epoch, agent_type, state, state_since_ms,
                 parent_session_id=None, derivation=None,
                 priority=None, cache_control=None, prediction=None):
    """构造随 LLM 请求下发的完整 AgentHint（缺省项省略）。"""
    session = {
        "session_id": session_id,
        "agent_type": agent_type,
        "call_index": call_index,
        "epoch": epoch,
    }
    if parent_session_id:
        session["parent_session_id"] = parent_session_id
    if derivation:
        session["derivation"] = derivation
    hint = {
        "session": session,
        "agent_status": {"state": state, "state_since_ms": state_since_ms},
    }
    if priority:
        hint["priority"] = priority
    if cache_control:
        hint["cache_control"] = cache_control
    if prediction:
        hint["prediction"] = prediction
    return hint


def status_event(session_id, state, since_ms, reason=None, tools=None):
    """构造状态事件（异步上报）。session 只带 session_id（对齐字段文档 §4 示例）。"""
    event = {
        "session": {"session_id": session_id},
        "agent_status": {"state": state, "state_since_ms": since_ms},
    }
    if reason:
        event["agent_status"]["reason"] = reason
    if tools:
        event["agent_status"]["tools"] = tools
    return event


def cache_control(request, end=1):
    """最小形态：pin 稳定前缀（按消息条数）。request 含合法 messages 才产出，否则 None。

    D2：不发 range.unit；ephemeral/ttl_s 留 T2。
    """
    messages = request.get("messages") if isinstance(request, dict) else None
    if not isinstance(messages, list) or not messages or end <= 0:
        return None
    return {"type": "pin", "range": {"end": end}}
