"""字段发射实验：每个字段一个场景，走真实插件注册 + 真实中间件。

运行：python3 -m pytest tests/test_field_sending.py -q -s
"""
import base64
import json
import os
import time


def _ctx():
    hooks, mw = {}, {}

    class _C:
        def register_hook(self, name, cb):
            hooks[name] = cb

        def register_middleware(self, name, cb):
            mw[name] = cb

    return _C(), hooks, mw


def _req(n=2):
    return {"model": "m", "messages": [
        {"role": "system", "content": "sys " + "x " * 400},
        {"role": "user", "content": "hi"},
    ][:n]}


def _hint(mw, sid, api_id):
    out = mw["llm_request"](request=_req(), session_id=sid, api_request_id=api_id)
    raw = out["request"]["extra_headers"]["X-Agent-Hint"]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def _enable(plugin, monkeypatch, **env):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    plugin._settings._reset()
    ctx = _ctx()
    plugin.register(ctx[0])
    return ctx[1], ctx[2]


def test_session_id_is_stable(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch)
    a = _hint(mw, "s-7f3a2c", "r-1")["session"]
    b = _hint(mw, "s-7f3a2c", "r-2")["session"]
    assert a["session_id"] == b["session_id"] == "s-7f3a2c"
    assert (a["call_index"], b["call_index"]) == (1, 2)
    print(f"  session_id      : {a['session_id']} (call {a['call_index']} → {b['call_index']})")


def test_fork_lineage_is_declared(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch,
                    AGENT_HINT_LINEAGE='{"relation":"fork","parent_session_id":"s-root01","fork_point":12}')
    s = _hint(mw, "s-child", "r-1")["session"]
    assert s["parent_session_id"] == "s-root01"
    assert s["derivation"] == {"relation": "fork", "fork_point": 12}
    print(f"  fork            : {json.dumps(s['derivation'])} parent={s['parent_session_id']}")


def test_spawn_lineage_is_native(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch)
    ah_plugin._on_subagent_start(parent_session_id="s-root01", child_session_id="s-child",
                                 child_role="worker")
    s = _hint(mw, "s-child", "r-1")["session"]
    assert s["parent_session_id"] == "s-root01"
    assert s["derivation"] == {"relation": "spawn", "join": "sync"}
    print(f"  spawn           : {json.dumps(s['derivation'])} parent={s['parent_session_id']}")


def test_epoch_bumps_when_context_shrinks(ah_plugin, monkeypatch):
    """0.7.1 起 epoch 不再从消息条数回落嗅探（宿主才知道压缩真发生没）：
    宿主在真实压缩后调 `_state.bump_epoch()`，每压缩一次 +1。"""
    _, mw = _enable(ah_plugin, monkeypatch)
    e0 = _hint(mw, "s-e", "r-1")["session"]["epoch"]
    ah_plugin._state.bump_epoch("s-e")
    e1 = _hint(mw, "s-e", "r-2")["session"]["epoch"]
    ah_plugin._state.bump_epoch("s-e")
    e2 = _hint(mw, "s-e", "r-3")["session"]["epoch"]
    assert (e0, e1, e2) == (0, 1, 2)
    print(f"  epoch           : 初始 {e0}，压缩一次 → {e1}，再压缩 → {e2}")


def test_agent_states(ah_plugin, monkeypatch):
    """请求头只有四值：等工具（tool）在线缆上折成 running，等审批是 pause；
    resume 是一次性放行边，只描述紧跟放行的那一条请求，其后回 running。
    finalize 默认 session_end 模式：turn 结束后的第一条请求头带它，再往后回 running。"""
    _, mw = _enable(ah_plugin, monkeypatch)
    sid = "s-state"
    running = _hint(mw, sid, "r-1")["agent_status"]["state"]
    ah_plugin._on_pre_tool_call(session_id=sid, tool_name="read", tool_call_id="c1")
    tool = _hint(mw, sid, "r-2")["agent_status"]["state"]  # 工具执行 → 内部词，上线缆折 running
    ah_plugin._on_post_tool_call(session_id=sid, tool_name="read", tool_call_id="c1")
    ah_plugin._on_pre_approval_request(session_id=sid, kind="approval")
    pause = _hint(mw, sid, "r-3")["agent_status"]["state"]
    ah_plugin._on_post_approval_response(session_id=sid, choice="once")
    resume = _hint(mw, sid, "r-4")["agent_status"]["state"]
    after = _hint(mw, sid, "r-5")["agent_status"]["state"]
    ah_plugin._on_session_end(session_id=sid, completed=True)
    settled = _hint(mw, sid, "r-6")["agent_status"]["state"]  # turn 结束 → finalize 骑一次
    print(f"  state 序列      : {running} → {tool} → {pause} → {resume} → {after} → settle={settled}")
    assert (running, tool, pause, resume, after, settled) == (
        "running", "running", "pause", "resume", "running", "finalize")


def test_suspend_folds_to_pause_on_the_wire(ah_plugin, monkeypatch):
    """Hermes 没有事件化 suspend：出口折成 pause，事件通道仍保留 suspend。"""
    _, mw = _enable(ah_plugin, monkeypatch)
    sid = "s-susp"
    ah_plugin._on_pre_approval_request(session_id=sid, kind="approval")
    ah_plugin._state.set_state(sid, "suspend")
    assert _hint(mw, sid, "r-1")["agent_status"]["state"] == "pause"
    print("  suspend 折并    : wire=pause")


def test_prediction_fields(ah_plugin, monkeypatch):
    # remaining_calls / next_action 是启发式，必须显式配置映射（不硬编码业务 SOP）
    _, mw = _enable(ah_plugin, monkeypatch,
                    AGENT_HINT_NEXT_ACTION_MAP='{"__default__": ["step_go", 3, "tool"]}')
    sid = "s-pred"
    now = time.time()
    for i in range(3):
        ah_plugin._on_post_api_request(session_id=sid, usage={"output_tokens": 420},
                                       api_duration=4.1, ended_at=now - 10 + i)
    p = _hint(mw, sid, "r-1")["prediction"]
    for key in ("output_tokens", "latency_ms", "interval_ms"):
        assert p.get(key) is not None, key
    print(f"  prediction      : output_tokens={p.get('output_tokens')} latency_ms={p.get('latency_ms')} "
          f"interval_ms={p.get('interval_ms')} remaining_calls={p.get('remaining_calls')} "
          f"next_action={p.get('next_action')}")


def test_priority_is_two_level(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch, AGENT_HINT_PRIORITY_LEVEL="4",
                    AGENT_HINT_PRIORITY_REASON="critical_path")
    pr = _hint(mw, "s-prio", "r-1")["priority"]
    assert pr == {"level": 3, "reason": "critical_path"}
    print(f"  priority        : 配置 4 → {json.dumps(pr)}")


def test_cache_control_is_single_and_covers_input(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch, AGENT_HINT_CACHE_TTL_S="600")
    cc = _hint(mw, "s-cc", "r-1")["cache_control"]
    assert isinstance(cc, list) and len(cc) == 1
    assert cc[0]["type"] == "ephemeral"
    assert cc[0]["ttl_s"] == 600
    assert cc[0]["range"]["end"] == 2
    print(f"  cache_control   : {json.dumps(cc[0])}")
