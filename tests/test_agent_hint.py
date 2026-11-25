"""agent-hint 插件单测（v0.8.0，归一化四块协议）。

覆盖 _codec / _builder / _state / _stats / _settings / _sink 六模块 + 中间件/hooks 契约。
装载：conftest 的 ah_plugin fixture（importlib 包方式，相对导入可用）。

协议权威定义：dsh ``docs/design/20261125-01-merged-header-protocol.md``。
本文件同时是“退役字段不再出现”的守卫——任何一块被悄悄加回来，这里会红。
"""
import json

import pytest


# ---------------- fixtures ----------------

@pytest.fixture(autouse=True)
def _reset_global(ah_plugin, monkeypatch):
    """每例前重置进程内状态 + 清缓存快照；结束后再清，避免跨例污染。"""
    def _clear():
        ah_plugin._state._reset()
        ah_plugin._stats._reset()
        ah_plugin._settings._ctx = None
        ah_plugin._settings._reset()
        ah_plugin._sink._ndjson_path = None
        ah_plugin._sink._warned.clear()
        ah_plugin._sink.reset_state()
        ah_plugin._sink._http_q = None
        ah_plugin._sink._http_worker = None
        ah_plugin._sink._http_url = None

    _clear()
    yield
    _clear()


class _FakeCtx:
    def __init__(self, cfg):
        self._cfg = cfg

    def get_config(self, key, default=None):
        return self._cfg.get(key, default)


class _Registry:
    """记录 register(ctx) 真实注册了哪些 hook / middleware。"""

    def __init__(self):
        self.hooks = {}
        self.middleware = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_middleware(self, name, cb):
        self.middleware[name] = cb


LONG_PREFIX = "lorem ipsum dolor sit amet " * 40  # ~1080 chars，约 270 token，超过一个块

RETIRED_BLOCKS = ("agent_status", "prediction", "derivation", "agent_type",
                  "strict_priority", "latency_control")


def _decode_headers(result):
    raw = (result.get("request") or {}).get("extra_headers", {}).get("X-Agent-Hint")
    assert isinstance(raw, str), "the middleware must inject the hint header"
    return json.loads(__import__("base64").urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())


# ---------------- _codec ----------------

def test_codec_ascii_roundtrip(ah_plugin):
    c = ah_plugin._codec
    obj = {"session": {"session_id": "s1", "call_index": 1}}
    assert c.decode(c.encode(obj)) == obj


def test_codec_non_ascii(ah_plugin):
    c = ah_plugin._codec
    obj = {"session": {"session_id": "会话-一"}}
    enc = c.encode(obj)
    assert "=" not in enc  # 无 padding
    assert c.decode(enc) == obj


def test_codec_decode_invalid_raises(ah_plugin):
    with pytest.raises(ValueError):
        ah_plugin._codec.decode("!!not-base64!!")


# ---------------- _builder: 四块 ----------------

def test_request_hint_full_blocks(ah_plugin):
    b = ah_plugin._builder
    hint = b.request_hint(
        session_id="s1", call_index=2, epoch=0, state="append",
        parent_session_id="s-parent",
        priority={"level": 3, "reason": "user_waiting"},
        cache_control={"type": "ephemeral", "ttl_s": 120, "msg_offset": 4},
    )
    assert hint["session"] == {"session_id": "s1", "state": "append",
                               "call_index": 2, "epoch": 0, "parent_session_id": "s-parent"}
    assert hint["priority_control"] == {"priority": {"level": 3, "reason": "user_waiting"}}
    assert hint["cache_control"] == {"type": "ephemeral", "ttl_s": 120, "msg_offset": 4}
    assert "context_management" not in hint  # 只有 manage 请求带它


def test_request_hint_omits_optional_blocks(ah_plugin):
    b = ah_plugin._builder
    hint = b.request_hint(session_id="s1", call_index=1, epoch=0, state="start")
    assert set(hint) == {"session"}  # 根会话：无 parent，无 priority，无 cache
    assert set(hint["session"]) == {"session_id", "state", "call_index", "epoch"}


def test_request_hint_carries_no_retired_blocks(ah_plugin):
    b = ah_plugin._builder
    hint = b.request_hint(session_id="s1", call_index=1, epoch=0, state="start",
                          parent_session_id="p",
                          priority={"level": 1, "reason": "critical_path"},
                          cache_control={"type": "ephemeral", "ttl_s": 60, "msg_offset": 1})
    for name in RETIRED_BLOCKS:
        assert name not in hint, name
        assert name not in hint["session"], name
    assert "range" not in hint["cache_control"]  # 单对象，没有 range/unit


def test_request_hint_refuses_manage_states(ah_plugin):
    b = ah_plugin._builder
    for state in b.MANAGE_STATES:
        with pytest.raises(ValueError):
            b.request_hint(session_id="s1", call_index=1, epoch=0, state=state)


def test_ordinary_state_is_start_only_for_the_first_call(ah_plugin):
    b = ah_plugin._builder
    assert b.ordinary_state(0) == "start"
    assert b.ordinary_state(1) == "start"
    assert b.ordinary_state(2) == "append"
    assert b.ordinary_state("oops") == "append"


# ---------------- _builder: priority_control ----------------

@pytest.mark.parametrize("configured,expected", [
    (1, 1), (2, 1), (3, 3), (4, 3), (90, 3), (-1, 1),
])
def test_priority_block_folds_level_and_pairs_reason(ah_plugin, configured, expected):
    """level 只保留 {1, 3}，reason 由 level 推导——协议不允许二者自相矛盾。"""
    block = ah_plugin._builder.priority_block(configured)
    assert block["level"] == expected
    assert block["reason"] == ("user_waiting" if expected == 3 else "critical_path")


def test_priority_reason_pairs_are_frozen(ah_plugin):
    b = ah_plugin._builder
    assert set(b.PRIORITY_REASON_BY_LEVEL) == {1, 3}
    assert b.PRIORITY_REASONS == ("user_waiting", "critical_path")
    with pytest.raises(TypeError):
        b.PRIORITY_REASON_BY_LEVEL[2] = "whatever"


# ---------------- _builder: cache_control ----------------

def test_cache_control_is_one_object_with_message_offset(ah_plugin):
    b = ah_plugin._builder
    messages = [{"role": "system", "content": LONG_PREFIX}, {"role": "user", "content": "hi"}]
    block = b.cache_control(messages, ttl_s=120)
    assert block == {"type": "ephemeral", "ttl_s": 120, "msg_offset": 2}
    assert set(block) == {"type", "ttl_s", "msg_offset"}


def test_cache_control_explicit_offset(ah_plugin):
    b = ah_plugin._builder
    messages = [{"role": f"m{i}", "content": LONG_PREFIX} for i in range(6)]
    assert b.cache_control(messages, ttl_s=300, end=2)["msg_offset"] == 2
    assert b.cache_control(messages, ttl_s=300, end=0) is None    # 0 = 不发
    assert b.cache_control(messages, ttl_s=300, end=99) is None   # 越界宁可不发


def test_cache_control_requires_ttl_and_prefix(ah_plugin):
    b = ah_plugin._builder
    messages = [{"role": "system", "content": LONG_PREFIX}]
    assert b.cache_control(messages, ttl_s=0) is None
    assert b.cache_control(messages, ttl_s=None) is None
    assert b.cache_control([], ttl_s=120) is None
    assert b.cache_control("not-a-list", ttl_s=120) is None
    # 前缀不足一个缓存块：不为不存在的东西声明保留
    short = [{"role": "system", "content": "hi"}]
    assert b.cache_control(short, ttl_s=120) is None


def test_estimate_prefix_tokens_counts_cjk_per_character(ah_plugin):
    b = ah_plugin._builder
    assert b.estimate_prefix_tokens("会话一二三四") == 6
    assert b.estimate_prefix_tokens("abcdefgh") == 2
    assert b.estimate_prefix_tokens([{"content": "abcdefgh"}]) == 2


# ---------------- _builder: manage requests ----------------

@pytest.mark.parametrize("state,edit", [
    ("pause", "offload"), ("resume", "prefetch"), ("stop", "evict"),
])
def test_build_manage_hint_maps_verb_to_edit(ah_plugin, state, edit):
    b = ah_plugin._builder
    hint = b.build_manage_hint(session_id="s1", call_index=3, epoch=2, state=state)
    assert hint["session"] == {"session_id": "s1", "state": state, "call_index": 3, "epoch": 2}
    cm = hint["context_management"]
    assert cm["manage_request"] is True
    # edits 只描述“对哪个会话做什么”：版本号属于 session 身份，两处都写会有两份真相
    assert cm["edits"] == [{"type": edit, "target": "session"}]
    assert "priority_control" not in hint and "cache_control" not in hint


def test_compact_selects_evict_and_trims_the_epoch(ah_plugin):
    hint = ah_plugin._builder.build_manage_hint(
        session_id="s1", call_index=3, epoch=2, state="compact")
    assert hint["session"] == {"session_id": "s1", "state": "compact",
                               "call_index": 3, "epoch": 1}
    assert hint["context_management"]["edits"] == [{"type": "evict", "target": "session"}]


def test_compact_names_the_previous_epoch(ah_plugin):
    """compact 描述刚被重写掉的那一代，所以 session.epoch 带 epoch - 1。"""
    b = ah_plugin._builder
    hint = b.build_manage_hint(session_id="s1", call_index=1, epoch=3, state="compact")
    assert hint["session"]["epoch"] == 2
    floor = b.build_manage_hint(session_id="s1", call_index=1, epoch=0, state="compact")
    assert floor["session"]["epoch"] == 0  # 下限 0


def test_build_manage_hint_refuses_ordinary_states(ah_plugin):
    b = ah_plugin._builder
    for state in ("start", "append"):
        with pytest.raises(ValueError):
            b.build_manage_hint(session_id="s1", call_index=1, epoch=0, state=state)


def test_manage_body_is_a_placeholder(ah_plugin):
    assert ah_plugin._builder.manage_body("m") == {"model": "m", "messages": [], "stream": False}
    # 没配 model 就整段省略：空的 model 会被网关判为非法请求
    assert ah_plugin._builder.manage_body("") == {"messages": [], "stream": False}


# ---------------- _builder: TTL ----------------

def test_estimate_ttl_s_follows_the_interval_doubling_rule(ah_plugin):
    b = ah_plugin._builder
    assert b.estimate_ttl_s(interval_ms=120_000) == 240   # 120s * 2
    assert b.estimate_ttl_s(interval_ms=5_000) == 60      # 10s -> 抬到下限
    assert b.estimate_ttl_s() == 600                      # 默认 gap 300 * 2
    assert b.estimate_ttl_s(interval_ms=7_200_000) == b.TTL_MAX_S  # 截断到 3600
    assert b.estimate_ttl_s(interval_ms=0) == 600


# ---------------- _state ----------------

def test_call_index_starts_at_one(ah_plugin):
    assert ah_plugin._state.note_call("s1", "r1")[0] == 1


def test_call_index_dedup_same_request_id(ah_plugin):
    s = ah_plugin._state
    assert s.note_call("s1", "r1")[0] == 1
    assert s.note_call("s1", "r1")[0] == 1  # 重试不涨
    assert s.note_call("s1", "r2")[0] == 2


def test_parallel_tools_gate_running(ah_plugin):
    s = ah_plugin._state
    s.tool_started("s1", "c1")
    s.tool_started("s1", "c2")
    assert s.tool_finished("s1", "c1") is None  # 在飞未清空
    assert s.tool_finished("s1", "c2") is not None


def test_finalize_then_resume(ah_plugin):
    s = ah_plugin._state
    s.set_state("s1", "finalize")
    _ci, state, _since, _epoch, resumed = s.note_call("s1", "r9")
    assert resumed is True
    assert state == "running"


def test_lineage_returns_parent_only(ah_plugin):
    """derivation/child_role 已退役：子会话需要的只有“我爹是谁”。"""
    s = ah_plugin._state
    assert s.lineage("s-child") is None
    s.subagent_start("s-parent", "s-child")
    assert s.lineage("s-child") == "s-parent"
    s.subagent_stop("s-child")
    assert s.lineage("s-child") is None
    s.subagent_start("s-parent", "")  # 空 child 防御跳过
    assert s.lineage("") is None


def test_peek_does_not_advance_anything(ah_plugin):
    s = ah_plugin._state
    assert s.peek("s1") == (0, 0, None)  # 未知会话不建条目
    s.note_call("s1", "r1")
    assert s.peek("s1")[0] == 1
    assert s.peek("s1")[0] == 1  # 读不改


def test_signature_change_is_detected_once(ah_plugin):
    s = ah_plugin._state
    assert s.note_signature("s1", "a") is False  # 第一次没有“上一轮”
    assert s.note_signature("s1", "a") is False
    assert s.note_signature("s1", "b") is True
    assert s.note_signature("s1", "b") is False


def test_rewrite_flag_is_consumed_once(ah_plugin):
    s = ah_plugin._state
    s.note_call("s1", "r1")
    assert s.consume_rewrite("s1") is False
    assert s.bump_epoch("s1") == 1
    assert s.consume_rewrite("s1") is True
    assert s.consume_rewrite("s1") is False
    # 未见过的会话也要能抬代：压缩可能发生在第一条请求之前，只查不建会把那次重写弄丢。
    assert s.bump_epoch("s-nonexistent") == 1
    assert s.consume_rewrite("s-nonexistent") is True


def test_drop_and_lru_purge_subagent_refs(ah_plugin):
    s = ah_plugin._state
    s.subagent_start("p0", "c0")
    assert "c0" in s._subagent
    for i in range(s._MAX_SESSIONS):
        s.note_call("s%d" % i, "r")
    assert "c0" not in s._subagent  # LRU 淘汰一并清父映射
    s.subagent_start("p1", "c1")
    s.drop("c1")
    assert "c1" not in s._subagent


def test_epoch_provider_seam(ah_plugin):
    s = ah_plugin._state
    assert s.note_call("s1", "r1")[3] == 0
    assert s.bump_epoch("s1") == 1
    assert s.note_call("s1", "r2")[3] == 1
    s.set_epoch_provider(lambda _sid: 42)
    assert s.note_call("s1", "r3")[3] == 42
    s.set_epoch_provider(lambda _sid: 1 / 0)  # provider 异常回退内部计数器
    assert s.note_call("s1", "r4")[3] == 1
    s.set_epoch_provider(None)


# ---------------- _stats ----------------

def test_collect_then_snapshot(ah_plugin):
    st = ah_plugin._stats
    st.collect(session_id="s1", usage={"input_tokens": 10, "output_tokens": 20},
               api_duration=0.5, first_chunk_at=100.0, started_at=99.5, ended_at=100.5)
    snap = st.snapshot("s1")
    assert snap["output_tokens"] == 20
    assert snap["latency_ms"] == 500
    assert "interval_ms" in snap  # 上一次结束到现在
    assert "next_action" not in snap and "remaining_calls" not in snap  # prediction 已退役


def test_usage_none_skipped(ah_plugin):
    st = ah_plugin._stats
    st.collect(session_id="s1", usage=None, api_duration=1.0)
    assert st.snapshot("s1") is None
    assert st.interval_ms("s1") is None


def test_interval_only_sample(ah_plugin):
    """只有结束时刻也能给出间隔：TTL 推导不依赖 usage。"""
    st = ah_plugin._stats
    st.collect(session_id="s1", usage=None, ended_at=0.0)  # usage None -> 不记样本
    assert st.interval_ms("s1") is None
    st.collect(session_id="s1", usage={"output_tokens": 1}, ended_at=0.0)
    assert st.interval_ms("s1") is not None


def test_stats_snapshot_thread_safe(ah_plugin):
    import threading
    st = ah_plugin._stats
    caught = []

    def _worker(base):
        for j in range(300):
            st.collect(session_id="s%d" % (base % 4), usage={"output_tokens": j},
                       api_duration=0.001, started_at=0.0, first_chunk_at=0.001, ended_at=0.002)
            try:
                st.snapshot("s%d" % (base % 4))
            except Exception as ex:
                caught.append(ex)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not caught, "snapshot 并发下不应抛异常: %r" % caught


# ---------------- _settings ----------------

def test_settings_precedence(ah_plugin, monkeypatch):
    s = ah_plugin._settings
    monkeypatch.setenv("AGENT_HINT_MANAGE_MODEL", "from-env")
    s.bind(_FakeCtx({"manage_model": "from-settings"}))
    assert s.manage_model() == "from-settings"


def test_settings_env_over_default(ah_plugin, monkeypatch):
    s = ah_plugin._settings
    monkeypatch.setenv("AGENT_HINT_MANAGE_MODEL", "from-env")
    s._reset()
    assert s.manage_model() == "from-env"
    assert s.enabled() is False
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    s._reset()
    assert s.enabled() is True


def test_retired_settings_are_gone(ah_plugin):
    """退役键不再被读取：旧配置留着也不会悄悄改变行为。"""
    for key in ("agent_type", "status_sink", "status_url", "finalize_on",
                "suspend_after_s", "next_action_map", "cache_task_end", "priority_reason"):
        assert key not in ah_plugin._settings._DEFAULTS, key


def test_priority_level_zero_or_empty_omitted(ah_plugin, monkeypatch):
    s = ah_plugin._settings
    for raw in ("0", "", "-3", "abc"):
        monkeypatch.setenv("AGENT_HINT_PRIORITY_LEVEL", raw)
        s._reset()
        assert s.priority_level() is None, raw
    monkeypatch.setenv("AGENT_HINT_PRIORITY_LEVEL", "3")
    s._reset()
    assert s.priority_level() == 3


def test_cache_ttl_distinguishes_auto_none_and_fixed(ah_plugin, monkeypatch):
    s = ah_plugin._settings
    for raw in ("", "auto", "none", "null"):
        monkeypatch.setenv("AGENT_HINT_CACHE_TTL_S", raw)
        s._reset()
        assert s.cache_ttl_s() is None, raw
    for raw in ("0", "59", "3601", "-5", "abc"):
        monkeypatch.setenv("AGENT_HINT_CACHE_TTL_S", raw)
        s._reset()
        assert s.cache_ttl_s() == 0, raw
    monkeypatch.setenv("AGENT_HINT_CACHE_TTL_S", "120")
    s._reset()
    assert s.cache_ttl_s() == 120


def test_cache_pin_end_parsing(ah_plugin, monkeypatch):
    s = ah_plugin._settings
    monkeypatch.delenv("AGENT_HINT_CACHE_PIN_END", raising=False)
    s._reset()
    assert s.cache_pin_end() is None  # None = auto
    for raw in ("auto", ""):
        monkeypatch.setenv("AGENT_HINT_CACHE_PIN_END", raw)
        s._reset()
        assert s.cache_pin_end() is None, raw
    monkeypatch.setenv("AGENT_HINT_CACHE_PIN_END", "0")
    s._reset()
    assert s.cache_pin_end() == 0
    monkeypatch.setenv("AGENT_HINT_CACHE_PIN_END", "3")
    s._reset()
    assert s.cache_pin_end() == 3


def test_manage_sink_defaults_and_overrides(ah_plugin, monkeypatch):
    s = ah_plugin._settings
    for key in ("AGENT_HINT_MANAGE_SINK", "AGENT_HINT_MANAGE_URL"):
        monkeypatch.delenv(key, raising=False)
    s._reset()
    assert s.manage_sink() == "none"
    monkeypatch.setenv("AGENT_HINT_MANAGE_URL", "http://x/v1/chat/completions")
    s._reset()
    assert s.manage_sink() == "http"
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    s._reset()
    assert s.manage_sink() == "ndjson"


def test_manage_clamps(ah_plugin, monkeypatch):
    s = ah_plugin._settings
    monkeypatch.setenv("AGENT_HINT_MANAGE_TIMEOUT_S", "999")
    monkeypatch.setenv("AGENT_HINT_MANAGE_MAX_RETRIES", "99")
    monkeypatch.setenv("AGENT_HINT_MANAGE_QUEUE_LIMIT", "0")
    s._reset()
    assert s.manage_timeout_s() == 30
    assert s.manage_max_retries() == 3
    assert s.manage_queue_limit() == 1


def test_manage_token_reads_the_named_env(ah_plugin, monkeypatch):
    s = ah_plugin._settings
    monkeypatch.delenv("AGENT_HINT_TOKEN", raising=False)
    s._reset()
    assert s.manage_token() == ""
    monkeypatch.setenv("AGENT_HINT_TOKEN", "tok")
    s._reset()
    assert s.manage_token() == "tok"
    monkeypatch.setenv("AGENT_HINT_MANAGE_TOKEN_ENV", "OTHER_TOKEN")
    monkeypatch.setenv("OTHER_TOKEN", "tok2")
    s._reset()
    assert s.manage_token() == "tok2"


# ---------------- _sink ----------------

def _manage_lines(tmp_path):
    path = tmp_path / "agent-hint-manage.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().split("\n")]


def _pause_hint(ah_plugin, epoch=0, call_index=2):
    return ah_plugin._builder.build_manage_hint(
        session_id="s1", call_index=call_index, epoch=epoch, state="pause")


def test_ndjson_records_the_manage_request(tmp_path, ah_plugin, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    monkeypatch.setenv("AGENT_HINT_MANAGE_MODEL", "m")
    monkeypatch.setenv("AGENT_HINT_TOKEN", "secret-token")
    ah_plugin._settings._reset()
    assert ah_plugin._sink.dispatch(_pause_hint(ah_plugin)) is True
    records = _manage_lines(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["hint"]["session"]["state"] == "pause"
    assert record["body"] == {"model": "m", "messages": [], "stream": False}
    assert record["headers"]["Authorization"] == "Bearer secret-token"
    # token 只进 header，绝不进 hint（hint 会随请求落到引擎日志里）
    assert "secret-token" not in json.dumps(record["hint"])


def test_sink_none_sends_nothing(tmp_path, ah_plugin, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "none")
    ah_plugin._settings._reset()
    assert ah_plugin._sink.dispatch(_pause_hint(ah_plugin)) is False
    assert _manage_lines(tmp_path) == []


def test_consecutive_identical_verbs_are_deduped(tmp_path, ah_plugin, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    ah_plugin._settings._reset()
    _sink = ah_plugin._sink
    _b = ah_plugin._builder
    assert _sink.dispatch(_b.build_manage_hint(session_id="s1", call_index=1, epoch=0, state="pause"))
    assert not _sink.dispatch(_b.build_manage_hint(session_id="s1", call_index=1, epoch=0, state="pause"))
    assert _sink.dispatch(_b.build_manage_hint(session_id="s1", call_index=1, epoch=0, state="resume"))
    assert _sink.dispatch(_b.build_manage_hint(session_id="s1", call_index=1, epoch=0, state="pause"))
    assert len(_manage_lines(tmp_path)) == 3  # pause / resume / pause


def test_sink_never_raises(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    ah_plugin._settings._reset()
    ah_plugin._sink._ndjson_path = None
    import builtins
    real_open = builtins.open

    def _boom(*a, **k):
        raise IOError("disk full")

    monkeypatch.setattr(builtins, "open", _boom)
    ah_plugin._sink.dispatch(_pause_hint(ah_plugin))  # 吞掉，不抛
    monkeypatch.setattr(builtins, "open", real_open)


def test_dispatch_ignores_malformed_hint(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    ah_plugin._settings._reset()
    assert ah_plugin._sink.dispatch({}) is False
    assert ah_plugin._sink.dispatch({"session": {"session_id": "s1"}}) is False


# ---------------- middleware ----------------

def _request(messages=None, model="mock", tools=None):
    body = {"model": model, "messages": messages or [
        {"role": "system", "content": "sys " + LONG_PREFIX},
        {"role": "user", "content": "hello"},
    ]}
    if tools is not None:
        body["tools"] = tools
    return body


def test_middleware_injects_four_blocks_without_touching_the_body(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    req = _request()
    result = ah_plugin._on_llm_request(request=req, session_id="s1", api_request_id="r1")
    assert result is not None
    assert result["request"] is not req        # COW
    assert req.get("extra_headers") is None    # 入参未被改
    assert "agent_hint" not in result["request"]  # 零字段进 body

    hint = _decode_headers(result)
    assert hint["session"] == {"session_id": "s1", "state": "start",
                               "call_index": 1, "epoch": 0}
    assert hint["priority_control"] == {"priority": {"level": 3, "reason": "user_waiting"}}
    assert hint["cache_control"]["msg_offset"] == 2
    assert hint["cache_control"]["ttl_s"] == 600  # 无观测 -> 默认 gap 300 * 2
    for name in RETIRED_BLOCKS:
        assert name not in hint, name


def test_middleware_start_once_then_append(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    first = _decode_headers(ah_plugin._on_llm_request(
        request=_request(), session_id="s1", api_request_id="r1"))
    second = _decode_headers(ah_plugin._on_llm_request(
        request=_request(), session_id="s1", api_request_id="r2"))
    assert (first["session"]["state"], first["session"]["call_index"]) == ("start", 1)
    assert (second["session"]["state"], second["session"]["call_index"]) == ("append", 2)


def test_middleware_retry_keeps_the_same_call_index(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    ah_plugin._on_llm_request(request=_request(), session_id="s1", api_request_id="r1")
    retry = _decode_headers(ah_plugin._on_llm_request(
        request=_request(), session_id="s1", api_request_id="r1"))
    assert retry["session"]["call_index"] == 1
    assert retry["session"]["state"] == "start"  # 同一次逻辑请求，不是第二次打开会话


def test_middleware_drops_cache_on_an_empty_assistant_message(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    req = _request(messages=[
        {"role": "system", "content": "sys " + LONG_PREFIX},
        {"role": "assistant", "content": ""},
    ])
    hint = _decode_headers(ah_plugin._on_llm_request(request=req, session_id="s1"))
    assert "cache_control" not in hint


def test_middleware_drops_cache_when_model_or_tools_change(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    first = _decode_headers(ah_plugin._on_llm_request(
        request=_request(tools=[{"type": "function", "function": {"name": "a"}}]),
        session_id="s1", api_request_id="r1"))
    assert "cache_control" in first  # 第一轮没有“上一轮签名”
    same = _decode_headers(ah_plugin._on_llm_request(
        request=_request(tools=[{"type": "function", "function": {"name": "a"}}]),
        session_id="s1", api_request_id="r2"))
    assert "cache_control" in same
    changed = _decode_headers(ah_plugin._on_llm_request(
        request=_request(tools=[{"type": "function", "function": {"name": "b"}}]),
        session_id="s1", api_request_id="r3"))
    assert "cache_control" not in changed
    back = _decode_headers(ah_plugin._on_llm_request(
        request=_request(tools=[{"type": "function", "function": {"name": "b"}}]),
        session_id="s1", api_request_id="r4"))
    assert "cache_control" in back  # 签名稳定后恢复


def test_middleware_drops_cache_on_the_rewrite_round(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    ah_plugin._on_llm_request(request=_request(), session_id="s1", api_request_id="r1")
    assert ah_plugin._state.bump_epoch("s1") == 1  # 上下文被重写 -> 新一代
    hint = _decode_headers(ah_plugin._on_llm_request(
        request=_request(), session_id="s1", api_request_id="r2"))
    assert hint["session"]["epoch"] == 1
    assert hint["session"]["state"] == "append"
    assert "cache_control" not in hint
    after = _decode_headers(ah_plugin._on_llm_request(
        request=_request(), session_id="s1", api_request_id="r3"))
    assert "cache_control" in after  # 只有重写那一轮放弃声明


def test_middleware_drops_cache_for_a_short_prefix(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    hint = _decode_headers(ah_plugin._on_llm_request(
        request={"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
        session_id="s1"))
    assert "cache_control" not in hint


def test_middleware_omits_priority_when_level_is_zero(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    monkeypatch.setenv("AGENT_HINT_PRIORITY_LEVEL", "0")
    ah_plugin._settings._reset()
    hint = _decode_headers(ah_plugin._on_llm_request(request=_request(), session_id="s1"))
    assert "priority_control" not in hint


def test_middleware_folds_a_configured_priority_into_the_pair(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    monkeypatch.setenv("AGENT_HINT_PRIORITY_LEVEL", "4")
    ah_plugin._settings._reset()
    hint = _decode_headers(ah_plugin._on_llm_request(request=_request(), session_id="s1"))
    assert hint["priority_control"] == {"priority": {"level": 3, "reason": "user_waiting"}}


def test_middleware_ignores_malformed_input(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    assert ah_plugin._on_llm_request(request="not-a-dict", session_id="s1") is None
    assert ah_plugin._on_llm_request(request={}, session_id="") is None


def test_middleware_subagent_carries_parent_only(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    ah_plugin._state.subagent_start("s-parent", "s-child")
    hint = _decode_headers(ah_plugin._on_llm_request(
        request=_request(), session_id="s-child", api_request_id="r1"))
    assert hint["session"]["parent_session_id"] == "s-parent"
    assert hint["session"]["state"] == "start"  # 子会话的第一个请求也是 start
    assert "derivation" not in hint["session"]
    assert "agent_type" not in hint["session"]


# ---------------- hooks ----------------

def test_disabled_plugin_does_nothing(ah_plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "0")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    ah_plugin._settings._reset()
    assert ah_plugin._on_llm_request(request=_request(), session_id="s1") is None
    ah_plugin._on_pre_approval_request(session_id="s1")
    ah_plugin._on_session_finalize(session_id="s1")
    ah_plugin.notify_compact(session_id="s1")
    assert not (tmp_path / "agent-hint-manage.ndjson").exists()


def test_pre_tool_call_returns_none(ah_plugin, monkeypatch):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    ah_plugin._settings._reset()
    assert ah_plugin._on_pre_tool_call(session_id="s1", tool_name="t", tool_call_id="c1") is None


def test_approval_hooks_send_pause_then_resume(ah_plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    ah_plugin._settings._reset()
    ah_plugin._on_llm_request(request=_request(), session_id="s1", api_request_id="r1")
    ah_plugin._on_pre_approval_request(session_id="s1")
    ah_plugin._on_post_approval_response(session_id="s1")
    states = [r["hint"]["session"]["state"] for r in _manage_lines(tmp_path)]
    assert states == ["pause", "resume"]
    edits = [r["hint"]["context_management"]["edits"][0]["type"] for r in _manage_lines(tmp_path)]
    assert edits == ["offload", "prefetch"]
    # manage 请求自己不是 LLM 调用：call_index 不该被它推高
    assert _manage_lines(tmp_path)[0]["hint"]["session"]["call_index"] == 1


def test_session_finalize_sends_stop_and_forgets_the_session(ah_plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    ah_plugin._settings._reset()
    ah_plugin._on_session_start(session_id="s1")
    ah_plugin._on_llm_request(request=_request(), session_id="s1", api_request_id="r1")
    ah_plugin._on_session_finalize(session_id="s1")
    records = _manage_lines(tmp_path)
    assert len(records) == 1
    hint = records[0]["hint"]
    assert hint["session"]["state"] == "stop"
    assert hint["context_management"]["edits"][0]["type"] == "evict"
    assert records[0]["body"]["messages"] == []
    assert ah_plugin._state.lineage("s1") is None
    assert ah_plugin._state.peek("s1") == (0, 0, None)  # 会话已回收


def test_notify_compact_bumps_epoch_and_names_the_dropped_generation(ah_plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HINT_ENABLED", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HINT_MANAGE_SINK", "ndjson")
    ah_plugin._settings._reset()
    ah_plugin._on_llm_request(request=_request(), session_id="s1", api_request_id="r1")
    assert ah_plugin.notify_compact("s1") is True
    record = _manage_lines(tmp_path)[0]
    assert record["hint"]["session"]["state"] == "compact"
    assert record["hint"]["session"]["epoch"] == 0  # 被丢掉的那一代，不是新的一代
    assert record["hint"]["context_management"]["edits"] == [{"type": "evict", "target": "session"}]
    # 重写标记已置位：下一轮请求不声明缓存，且 epoch 已经抬到 1
    hint = _decode_headers(ah_plugin._on_llm_request(
        request=_request(), session_id="s1", api_request_id="r2"))
    assert hint["session"]["epoch"] == 1
    assert "cache_control" not in hint


def test_register_wires_every_declared_capability(ah_plugin, monkeypatch):
    """manifest 声明的能力必须与真实注册一致（上游能力探针会校验）。"""
    monkeypatch.setenv("AGENT_HINT_ENABLED", "0")
    ah_plugin._settings._reset()
    registry = _Registry()
    ah_plugin.register(registry)
    assert set(registry.hooks) == {
        "on_session_start", "on_session_end", "on_session_finalize",
        "pre_tool_call", "post_tool_call",
        "pre_approval_request", "post_approval_response",
        "subagent_start", "subagent_stop", "post_api_request",
        "post_compaction",
    }
    assert set(registry.middleware) == {"llm_request"}
