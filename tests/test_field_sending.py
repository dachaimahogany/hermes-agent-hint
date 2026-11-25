"""字段发射实验：每个字段一个场景，走真实插件注册 + 真实中间件。

运行：python3 -m pytest tests/test_field_sending.py -q -s

一行一个字段，两个方向都测：该发的发得对（值、口径、钳制），退役的发不出来（断言缺席）。
只测"该发的"会漏掉最常见的一种回归——某次改动顺手把 retired 字段又带回来了。
"""
import base64
import json

CONTEXT_MESSAGES = 2  # _req() 给的是 system + user；msg_offset 的口径是线上条数


def _ctx():
    hooks, mw = {}, {}

    class _C:
        def register_hook(self, name, cb):
            hooks[name] = cb

        def register_middleware(self, name, cb):
            mw[name] = cb

    return _C(), hooks, mw


def _req():
    return {"model": "m", "messages": [
        {"role": "system", "content": "sys " + "x " * 400},
        {"role": "user", "content": "hi"},
    ]}


def _encode(hint):
    raw = base64.urlsafe_b64encode(json.dumps(hint).encode()).decode()
    return raw.rstrip("=")


def _decode(raw):
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def _hint(mw, sid, api_id, **extra):
    out = mw["llm_request"](request=_req(), session_id=sid, api_request_id=api_id, **extra)
    assert out is not None, "hint must be attached"
    raw = out["request"]["extra_headers"]["X-Agent-Hint"]
    return _decode(raw)


def _enable(plugin, monkeypatch, **env):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    plugin._settings._reset()
    plugin._sink.reset_state()
    ctx = _ctx()
    plugin.register(ctx[0])
    return ctx[1], ctx[2]


def _capture_manage(plugin, monkeypatch):
    """把 manage 请求截在 dispatch 口上，保持 build_manage_hint 的真实产物。"""
    sent = []
    monkeypatch.setattr(plugin._sink, "dispatch", lambda hint, model=None: sent.append(hint) or True)
    return sent


def test_session_id_is_stable(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch)
    a = _hint(mw, "s-7f3a2c", "r-1")["session"]
    b = _hint(mw, "s-7f3a2c", "r-2")["session"]
    assert a["session_id"] == b["session_id"] == "s-7f3a2c"
    assert (a["call_index"], b["call_index"]) == (1, 2)
    print(f"  session_id      : {a['session_id']} (call {a['call_index']} → {b['call_index']})")


def test_session_state_is_start_then_append(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch)
    first = _hint(mw, "s-state", "r-1")["session"]["state"]
    second = _hint(mw, "s-state", "r-2")["session"]["state"]
    assert (first, second) == ("start", "append")
    print(f"  session.state   : {first} → {second}")


def test_epoch_is_bumped_by_the_host_signal(ah_plugin, monkeypatch):
    """epoch 不从消息条数回落嗅探：宿主在真压缩后回调 post_compaction，每压缩一次 +1。"""
    _, mw = _enable(ah_plugin, monkeypatch)
    _capture_manage(ah_plugin, monkeypatch)
    assert _hint(mw, "s-e", "r-1")["session"]["epoch"] == 0
    ah_plugin._on_post_compaction(session_id="s-e", kind="summarize", before_tokens=90000,
                                  after_tokens=12000, messages_removed=57)
    assert _hint(mw, "s-e", "r-2")["session"]["epoch"] == 1
    ah_plugin._on_post_compaction(session_id="s-e", kind="micro_summarize")
    assert _hint(mw, "s-e", "r-3")["session"]["epoch"] == 2
    print("  epoch           : 0 → 压缩 1 → 微压缩 2")


def test_prune_tool_results_does_not_bump_epoch(ah_plugin, monkeypatch):
    """只裁短工具结果、消息条数不变：缓存前缀没换，抬代会让分代跟着裁剪抖动。"""
    _, mw = _enable(ah_plugin, monkeypatch)
    _capture_manage(ah_plugin, monkeypatch)
    _hint(mw, "s-prune", "r-1")
    ah_plugin._on_post_compaction(session_id="s-prune", kind="prune_tool_results")
    assert _hint(mw, "s-prune", "r-2")["session"]["epoch"] == 0
    print("  epoch           : prune_tool_results → 不抬代")


def test_retired_fields_are_absent(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch)
    hint = _hint(mw, "s-retired", "r-1")
    for retired in ("agent_status", "agent_type", "derivation", "prediction",
                    "strict_priority", "latency_control", "priority"):
        assert retired not in hint, retired
        assert retired not in hint["session"], retired
    assert sorted(hint) == ["cache_control", "priority_control", "session"]
    print(f"  members         : {', '.join(sorted(hint))}")


def test_priority_folds_to_two_levels(ah_plugin, monkeypatch):
    # 档位由部署方给（缺省 3），插件只负责折档 + 由 level 反推 reason——不猜"这一轮是不是有人在等"。
    _, mw = _enable(ah_plugin, monkeypatch)
    opening = _hint(mw, "s-prio", "r-1")["priority_control"]["priority"]
    later = _hint(mw, "s-prio", "r-2")["priority_control"]["priority"]
    assert opening == {"level": 3, "reason": "user_waiting"}
    assert later == opening
    print(f"  priority        : 缺省 3 → {opening}（两轮同档）")


def test_priority_level_folds_and_reason_follows(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch, AGENT_HINT_PRIORITY_LEVEL="4")
    assert _hint(mw, "s-p4", "r-1")["priority_control"]["priority"] == {
        "level": 3, "reason": "user_waiting"}
    ah_plugin._settings._reset()
    monkeypatch.setenv("AGENT_HINT_PRIORITY_LEVEL", "2")
    ah_plugin._settings._reset()
    assert _hint(mw, "s-p2", "r-1")["priority_control"]["priority"] == {
        "level": 1, "reason": "critical_path"}
    print("  priority 折档   : 4 → 3/user_waiting，2 → 1/critical_path")


def test_cache_control_is_a_single_object(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch, AGENT_HINT_CACHE_TTL_S="600")
    cc = _hint(mw, "s-cc", "r-1")["cache_control"]
    assert isinstance(cc, dict), "cache_control must be one object, not an array"
    assert sorted(cc) == ["msg_offset", "ttl_s", "type"]
    assert cc["type"] == "ephemeral"
    assert cc["ttl_s"] == 600
    assert cc["msg_offset"] == CONTEXT_MESSAGES
    print(f"  cache_control   : {json.dumps(cc)}")


def test_cache_ttl_clamps_to_the_protocol_range(ah_plugin, monkeypatch):
    for configured, expected in (("20", 0), ("90", 90), ("7200", 0)):
        _, mw = _enable(ah_plugin, monkeypatch, AGENT_HINT_CACHE_TTL_S=configured)
        cc = _hint(mw, "s-ttl-" + configured, "r-1").get("cache_control")
        if expected == 0:
            assert cc is None, f"ttl_s={configured} is out of range and must be dropped"
        else:
            assert cc["ttl_s"] == expected
    print("  ttl_s           : 20 → 不发，90 → 90，7200 → 不发")


def test_cache_control_is_dropped_on_a_rewrite_round(ah_plugin, monkeypatch):
    _, mw = _enable(ah_plugin, monkeypatch, AGENT_HINT_CACHE_TTL_S="600")
    _capture_manage(ah_plugin, monkeypatch)
    assert _hint(mw, "s-rw", "r-1")["cache_control"]["ttl_s"] == 600
    ah_plugin._on_post_compaction(session_id="s-rw", kind="summarize")
    rewritten = _hint(mw, "s-rw", "r-2")
    assert "cache_control" not in rewritten, "the prefix this request would pin was just rewritten"
    assert rewritten["session"]["state"] == "append"
    print("  rewrite round   : cache_control 缺席，session 照发")


def test_an_auxiliary_request_carries_nothing(ah_plugin, monkeypatch):
    """RFC §1.3 / §3.3.5：压缩摘要请求不发头，也不推进 call_index。"""
    _, mw = _enable(ah_plugin, monkeypatch)
    _hint(mw, "s-aux", "r-1")
    for kw in ({"auxiliary": 1}, {"purpose": "compaction"}, {"purpose": "summarization"},
               {"purpose": "summary"}, {"purpose": "title"}):
        out = mw["llm_request"](request=_req(), session_id="s-aux", api_request_id="aux", **kw)
        assert out is None, kw
    after = _hint(mw, "s-aux", "r-2")
    assert after["session"]["call_index"] == 2, "the auxiliary request must not consume a call slot"
    print("  aux request     : 原样放行，call_index 仍为 2")


def test_manage_request_shape(ah_plugin, monkeypatch):
    sent = _capture_manage(ah_plugin, monkeypatch)
    _, mw = _enable(ah_plugin, monkeypatch)
    _hint(mw, "s-mg", "r-1")
    sent.clear()

    ah_plugin._on_pre_approval_request(session_id="s-mg", kind="approval")
    ah_plugin._on_post_approval_response(session_id="s-mg", choice="once")
    ah_plugin._on_session_finalize(session_id="s-mg")
    verbs = [h["session"]["state"] for h in sent]
    assert verbs == ["pause", "resume", "stop"], verbs
    assert [h["context_management"]["edits"] for h in sent] == [
        [{"type": "offload", "target": "session"}],
        [{"type": "prefetch", "target": "session"}],
        [{"type": "evict", "target": "session"}],
    ]
    for h in sent:
        assert sorted(h) == ["context_management", "session"]
        assert h["context_management"]["manage_request"] is True
    print(f"  manage          : {' → '.join(verbs)}")


def test_compact_names_the_dropped_generation(ah_plugin, monkeypatch):
    sent = _capture_manage(ah_plugin, monkeypatch)
    _, mw = _enable(ah_plugin, monkeypatch)
    _hint(mw, "s-cp", "r-1")
    sent.clear()
    ah_plugin._on_post_compaction(session_id="s-cp", kind="summarize")
    assert len(sent) == 1
    hint = sent[0]
    assert hint["session"]["state"] == "compact"
    assert hint["context_management"]["edits"] == [{"type": "evict", "target": "session"}]
    # 新 epoch 的内容是这一轮的输入，卸载它等于卸载刚写好的东西：编辑作用在 epoch - 1。
    assert hint["session"]["epoch"] == 0
    print(f"  compact         : epoch={hint['session']['epoch']}（新代是 {hint['session']['epoch'] + 1}）")
