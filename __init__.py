"""agent-hint 插件入口：注册 llm_request 中间件 + 11 个 hooks。

零侵入：只依赖 ctx（register_hook / register_middleware / get_config）与标准库，
不 import 任何 hermes 内部模块。关闭态（enabled=0，默认）中间件与 hooks 均在入口早退（us 级）。

线格式的权威定义是 dsh 仓库的
``docs/design/20261125-01-merged-header-protocol.md``：只有四块，``session.state`` 六值，
``pause`` / ``resume`` / ``stop`` / ``compact`` 各由一次**专用 manage 请求**承载。
运行中状态（``running`` / ``tool`` / ``pause`` / ``finalize``）留在 ``_state`` 里，
不上线——引擎不需要知道 Agent 此刻在干什么，只需要知道这一轮之后要不要卸载上下文。
"""
import hashlib
import json

from . import _builder, _codec, _settings, _sink, _state, _stats


def register(ctx):
    _settings.bind(ctx)  # load 时快照全部 settings（env + plugins.entries.agent-hint.settings）

    for name, cb in (
        ("on_session_start", _on_session_start),
        ("on_session_end", _on_session_end),
        ("on_session_finalize", _on_session_finalize),
        ("pre_tool_call", _on_pre_tool_call),
        ("post_tool_call", _on_post_tool_call),
        ("pre_approval_request", _on_pre_approval_request),
        ("post_approval_response", _on_post_approval_response),
        ("subagent_start", _on_subagent_start),
        ("subagent_stop", _on_subagent_stop),
        ("post_api_request", _on_post_api_request),
        ("post_compaction", _on_post_compaction),
    ):
        ctx.register_hook(name, cb)

    # 中间件常驻注册（关闭态在回调内早退，us 级）：manifest 声明的能力必须与真实注册一致，
    # 否则上游能力探针（hermes plugins validate）会判为「声明与现实不符」。
    ctx.register_middleware("llm_request", _on_llm_request)


# --- 工具函数 ---

def _sid(kw):
    """会话标识：多数 hook 传 ``session_id``；审批类 hook 在调用点传 ``session_key``，
    并在 observability context 已绑定时由 dispatcher 额外补上 ``session_id``
    （见上游 ``tools/approval_context.py::_fire_approval_hook``；未绑定时该键不存在）。
    两者都认，取到哪个用哪个。"""
    return str(kw.get("session_id") or kw.get("session_key") or "")


def _on():
    return _settings.enabled()


def _tools_signature(request):
    """``model + tools`` 的摘要：它变了就等于引擎眼里的前缀全变了。

    缓存键包含工具定义，工具集一换，旧前缀一个字节都复用不上，此时声明保留是在
    保护一个不存在的对象。
    """
    payload = {
        "model": str(request.get("model") or ""),
        "tools": request.get("tools") or [],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _has_empty_assistant(messages):
    """消息里存在空的 assistant 消息 -> 这一轮的前缀写不完整，不声明保留。"""
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if content is None:
            return True
        if isinstance(content, str) and not content.strip():
            return True
        if isinstance(content, list) and not content:
            return True
    return False


def _emit_manage(sid, verb):
    """下发一次 manage 请求；失败静默（``_sink`` 内部 warn-once）。"""
    if verb == "compact":
        epoch = _state.bump_epoch(sid)  # 先抬 epoch，编辑才作用在刚被重写掉的那一代
        call_index, _epoch, parent = _state.peek(sid)
    else:
        call_index, epoch, parent = _state.peek(sid)
    hint = _builder.build_manage_hint(
        session_id=sid, call_index=call_index, epoch=epoch,
        state=verb, parent_session_id=parent,
    )
    return _sink.dispatch(hint)


# --- hooks ---

def _on_session_start(**kw):
    if not _on():
        return
    sid = _sid(kw)
    if not sid:
        return
    _state.set_state(sid, "running")


def _on_session_end(**kw):
    if not _on():
        return
    sid = _sid(kw)
    if not sid:
        return
    _state.clear_inflight(sid)  # turn 结束清在飞兜底
    _state.set_state(sid, "finalize")  # 内部状态：下一次请求据此判定“新一轮”


def _on_session_finalize(**kw):
    if not _on():
        return
    sid = _sid(kw)
    if not sid:
        return
    _emit_manage(sid, "stop")
    _state.drop(sid)  # 会话真关闭回收


def _on_pre_tool_call(**kw):
    if not _on():
        return None
    sid = _sid(kw)
    if not sid:
        return None
    _state.tool_started(sid, str(kw.get("tool_call_id") or ""))
    return None  # 纯观察，绝不返回 block/approve/modify


def _on_post_tool_call(**kw):
    if not _on():
        return
    sid = _sid(kw)
    if not sid:
        return
    _state.tool_finished(sid, str(kw.get("tool_call_id") or ""))


def _on_pre_approval_request(**kw):
    if not _on():
        return
    sid = _sid(kw)
    if not sid:
        return
    _state.set_state(sid, "pause")
    _emit_manage(sid, "pause")


def _on_post_approval_response(**kw):
    if not _on():
        return
    sid = _sid(kw)
    if not sid:
        return
    _state.set_state(sid, "running")
    _emit_manage(sid, "resume")


def _on_subagent_start(**kw):
    if not _on():
        return
    _state.subagent_start(str(kw.get("parent_session_id") or ""),
                          str(kw.get("child_session_id") or ""))


def _on_subagent_stop(**kw):
    if not _on():
        return
    _state.subagent_stop(str(kw.get("child_session_id") or ""))


def _on_post_api_request(**kw):
    if not _on():
        return
    _stats.collect(**kw)


# 宿主的压缩 kind 闭合集（agent/compaction_events.py:33）；换代口径见 _settings.epoch_on
def _on_post_compaction(**kw):
    """每次**已提交**压缩之后回调一次（hermes >=0.21.6 的 post_compaction）。

    覆盖自动/手动压缩、overflow 恢复、网关 /compress、微压缩与工具结果裁剪。
    kw：session_id / old_session_id / kind / reason / before_tokens / after_tokens /
    messages_removed；返回值被忽略（纯观察）。

    只有「逻辑输入被重写」的 kind 才换代（默认 summarize / micro_summarize /
    provider_native）。换代走 ``compact`` manage 请求：它先抬 epoch，再把编辑作用在
    ``epoch - 1``——刚被重写掉的那一代。宿主没给 kind 时按"重写过"处理：宁可多抬
    一代，也不漏掉一次真重写。
    """
    if not _on():
        return
    sid = _sid(kw)
    if not sid:
        return
    allowed = _settings.epoch_on()
    if allowed is not None:
        if not allowed:
            return
        kind = kw.get("kind")
        if kind is not None and str(kind) not in allowed:
            return
    _emit_manage(sid, "compact")


# --- 显式入口（上游没有稳定的 compaction hook 时由调用方直接调用） ---

def notify_compact(session_id=None, **_kw):
    """上下文刚被重写：抬高 epoch 并下发 ``compact`` manage 请求。

    ``compact`` 的编辑作用在 ``epoch - 1``（刚被重写掉的那一代），新 epoch 的内容
    是这一轮的输入，卸载它等于卸载刚写好的东西。
    """
    if not _on():
        return False
    sid = str(session_id or _kw.get("session_id") or "")
    if not sid:
        return False
    return _emit_manage(sid, "compact")


# --- middleware ---

_AUX_PURPOSES = ("compaction", "summarization", "summary", "title")


def _is_aux_request(kw):
    """辅助请求判定（RFC §1.3 / §3.3.5）：压缩摘要、会话标题这类请求不带任何 hint。

    Hermes 宿主没有独立的 compaction 事件（issue #118382），所以辅助性只能由
    中间件调用点显式声明：``auxiliary=1``，或 ``purpose`` 属辅助类。声明责任在
    宿主/适配器一侧，插件不做消息条数猜测——猜错的代价（主请求裸奔）远大于
    漏判（辅助请求多带一个头）。
    """
    if kw.get("auxiliary"):
        return True
    return str(kw.get("purpose") or "").strip().lower() in _AUX_PURPOSES


def _on_llm_request(**kw):
    if not _on():
        return None
    req = kw.get("request")
    if not isinstance(req, dict):
        return None
    if _is_aux_request(kw):
        # 辅助请求豁免：原样放行，不带 hint，也不推进 call_index（RFC §1.3/§3.3.5）。
        return None
    sid = _sid(kw)
    if not sid:
        return None
    try:
        call_index, _state_name, _since, epoch, _resumed = _state.note_call(
            sid, str(kw.get("api_request_id") or ""))
        parent_session_id = _state.lineage(sid)
        messages = req.get("messages")

        # 三种情况下这一轮不该声明保留：前缀刚被重写、工具集/模型变了、前缀里还有写了一半的
        # assistant 消息。三者都意味着“下一轮能复用的东西”与此刻信封里的东西不是同一份。
        rewrite = _state.consume_rewrite(sid)
        signature_changed = _state.note_signature(sid, _tools_signature(req))
        cache = None
        if not rewrite and not signature_changed and not _has_empty_assistant(messages):
            ttl_s = _settings.cache_ttl_s()
            if ttl_s is None:
                ttl_s = _builder.estimate_ttl_s(interval_ms=_stats.interval_ms(sid))
            cache = _builder.cache_control(
                messages,
                ttl_s=ttl_s,
                # None = auto：偏移落在当前逻辑输入末尾（含 system）。
                end=_settings.cache_pin_end(),
            )

        level = _settings.priority_level()
        hint = _builder.request_hint(
            session_id=sid, call_index=call_index, epoch=epoch,
            state=_builder.ordinary_state(call_index),
            parent_session_id=parent_session_id,
            priority=_builder.priority_block(level) if level is not None else None,
            cache_control=cache,
        )
        req = dict(req)  # 浅拷贝顶层（COW，绝不改入参）
        headers = dict(req.get("extra_headers") or {})
        headers[_builder.HINT_HEADER] = _codec.encode(hint)
        req["extra_headers"] = headers
        return {"request": req}
    except Exception as e:
        _sink._warn_once("llm_request", "hint build failed (kept request as-is): " + str(e))
        return None  # 保持请求原样（fail-safe，warn-once 见 stderr）
