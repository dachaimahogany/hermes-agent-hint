"""配置面（进程级快照）：env 与 plugins.entries.agent-hint.settings 二源合一。

优先级 settings > env > 缺省；register(ctx) 时 bind(ctx) 一次，之后全部读内存快照，
避免在热路径（每次 API 请求 / hook）重复走 ctx.get_config 的磁盘读。

归一化后只剩三类配置：开关、准入档位、缓存声明——外加 manage 请求的出口。
``agent_type`` / ``status_sink`` / ``status_url`` / ``finalize_on`` /
``suspend_after_s`` / ``next_action_map`` / ``cache_task_end`` 全部退役：它们要么给
已删除的字段供值，要么描述线格式里不存在的状态。
"""
import os

_ENV_PREFIX = "AGENT_HINT_"

_DEFAULTS = {
    "enabled": "0",
    "priority_level": "3",
    "cache_pin_end": "auto",
    "cache_ttl_s": "auto",
    "manage_sink": "",
    "manage_url": "",
    "manage_model": "",
    "manage_token_env": "AGENT_HINT_TOKEN",
    "manage_timeout_s": "5",
    "manage_max_retries": "1",
    "manage_queue_limit": "64",
    # 哪些 post_compaction 的 kind 算「上下文重写」、要抬会话 epoch。默认三档重写语义；
    # prune_tool_results（只裁短工具结果、消息条数不变）默认不算，需要时显式加。
    "epoch_on": "summarize,micro_summarize,provider_native",
}

_ctx = None
_snapshot = None


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def bind(ctx):
    """register(ctx) 时调用一次；随后 load 全量快照。"""
    global _ctx
    _ctx = ctx


def _load():
    global _snapshot
    out = {}
    for key, default in _DEFAULTS.items():
        val = None
        if _ctx is not None:
            try:
                val = _ctx.get_config(key, None)
            except Exception:
                val = None
        if val is None:
            val = os.environ.get(_ENV_PREFIX + key.upper(), None)
        if val is None:
            val = default
        out[key] = val
    _snapshot = out
    return out


def _reset():
    """测试/回收辅助：清空快照以强制下次读取重新加载。"""
    global _snapshot
    _snapshot = None


def _get(key):
    if _snapshot is None:
        _load()
    return _snapshot.get(key, _DEFAULTS.get(key))


def enabled():
    return _truthy(_get("enabled"))


def priority_level():
    """准入档位；``<= 0`` / 非数字 -> None（整段省略）。"""
    level = _int(_get("priority_level"), 0)
    if level <= 0:
        return None
    return level


def cache_pin_end():
    """``msg_offset`` 的位置：``auto``（默认）= 当前逻辑输入末尾，``0`` = 不发，
    正整数 = 显式下标。

    历史名字 ``cache_pin_end`` 保留只为配置兼容；``pin`` 本身早已退役。
    """
    raw = str(_get("cache_pin_end") if _get("cache_pin_end") is not None else "auto").strip().lower()
    if raw in ("", "auto"):
        return None
    return max(0, _int(raw, 0))


def cache_ttl_s():
    """ephemeral 保留秒数：None=按运行时信号推导（默认），0=不声明，正整数=固定值。

    固定值按消费侧的钳制口径校验：低于 60 无效、超过 3600 超协议上限；两者都按 0 处理
    ——宁可不声明，也不发一个会被消费侧静默改写的值。
    """
    raw = str(_get("cache_ttl_s") or "").strip().lower()
    if raw in ("", "auto", "none", "null"):
        return None
    ttl = _int(raw, 0)
    if ttl <= 0:
        return 0
    if ttl < 60 or ttl > 3600:
        return 0
    return ttl


def manage_sink():
    """manage 请求的出口：``ndjson`` / ``http`` / ``none``。

    ``none`` 是缺省：没有配 URL 就一个字节都不发。``ndjson`` 把将要发出的 manage
    请求写成一行供本地检查（e2e 与排障用），不产生任何网络行为。
    """
    raw = str(_get("manage_sink") or "").strip().lower()
    if raw in ("ndjson", "http", "none"):
        return raw
    return "http" if manage_url() else "none"


def manage_url():
    return str(_get("manage_url") or "").strip()


def manage_model():
    return str(_get("manage_model") or "").strip()


def manage_token_env():
    return str(_get("manage_token_env") or "AGENT_HINT_TOKEN").strip()


def manage_token():
    """从环境变量读 bearer token；没配就没有 Authorization 头。"""
    name = manage_token_env()
    if not name:
        return ""
    return str(os.environ.get(name) or "").strip()


def manage_timeout_s():
    seconds = _int(_get("manage_timeout_s"), 5)
    return max(1, min(30, seconds))


def manage_max_retries():
    return max(0, min(3, _int(_get("manage_max_retries"), 1)))


def manage_queue_limit():
    return max(1, min(1024, _int(_get("manage_queue_limit"), 64)))


def epoch_on():
    """哪些 ``post_compaction.kind`` 算「上下文重写」、要抬会话 epoch。

    宿主的 kind 闭合集是 ``summarize / micro_summarize / prune_tool_results /
    provider_native``。默认取三档重写语义：那才是缓存前缀真正失效的时刻；
    ``prune_tool_results`` 只裁短工具结果、消息条数不变，抬 epoch 会让分代跟着
    "裁剪"抖动。``all`` 收全部，``none`` 一律不抬。
    """
    raw = str(_get("epoch_on") or "").strip().lower()
    if raw in ("", "none", "off", "0", "false"):
        return frozenset()
    if raw in ("all", "*"):
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
