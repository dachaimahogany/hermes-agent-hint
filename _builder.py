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


TTL_MIN_S = 60
TTL_MAX_S = 3600
TTL_SAFETY_FACTOR = 2
TTL_FALLBACK_GAP_S = 300


def estimate_ttl_s(tool_expected_duration_s=None, interval_ms=None):
    """按运行时信号估算保留时长（交接文档 §3.3）。

    优先级：下一次工具调用的预计时长 > 最近一次请求间隔 > 协议默认 300s。

    乘以安全系数后落在协议窗口 ``[TTL_MIN_S, TTL_MAX_S]`` 内：低于下限抬到 60s，
    高于上限**截断到 3600s**（实测结论，P3 2026-10-21：保留生效的充要条件是
    ``ttl_s > 空闲间隔``；不声明时 600s 间隔命中 0%，声明 3600s 命中 99.8%。
    因此截断严格优于丢弃——丢弃等于完全放弃保护）。
    """
    gap = None
    if isinstance(tool_expected_duration_s, (int, float)) and tool_expected_duration_s > 0:
        gap = float(tool_expected_duration_s)
    elif isinstance(interval_ms, (int, float)) and interval_ms > 0:
        gap = interval_ms / 1000.0
    else:
        gap = TTL_FALLBACK_GAP_S

    raw = gap * TTL_SAFETY_FACTOR
    return max(TTL_MIN_S, min(TTL_MAX_S, int(round(raw))))


def cache_control(request, end=1, ttl_s=0, task_end=None):
    """会话前缀的保留声明（pin 稳定前缀 + 可选 ephemeral 保活）。

    断点规则（交接文档 §3.2）：断点 1 = 稳定前缀末尾（system + 工具定义），
    断点 2 = 任务说明末尾（``task_end``，由调用方按自己的消息结构定位）。
    两条断点共享同一 ``range.unit="message"`` 坐标；当前轮对话永远不打断点。

    - ``ttl_s <= 0`` 时不产出 ephemeral 条目：只有显式 TTL 才进入消费侧生命周期
      （Motor 契约：无 ttl 的声明不入表），因此 pin 保持最小形态。
    - ``task_end`` 为空/不大于 ``end``/越界时不产出第二条断点——不猜位置。

    ``request`` 含合法 messages 才产出，否则 None（畸形输入绝不抛错）。
    """
    messages = request.get("messages") if isinstance(request, dict) else None
    if not isinstance(messages, list) or not messages or end <= 0:
        return None
    if end > len(messages):
        # 断点越界：消费侧会整条丢弃 range，这里宁可不发也不发一个错位的声明。
        return None

    controls = [{"type": "pin", "range": {"end": end, "unit": "message"}}]
    if ttl_s > 0:
        controls.append({
            "type": "ephemeral",
            "ttl_s": ttl_s,
            "range": {"end": end, "unit": "message"},
        })
    # 第二条断点保护到任务上下文为止。它必须比第一条更宽才是一个真正的嵌套前缀；
    # 等于第一条只是重复声明，因此要求严格大于。
    if isinstance(task_end, int) and end < task_end <= len(messages):
        controls[-1]["range"]["end"] = task_end
    return controls
