"""AgentHint 对象构造（纯函数，无副作用）。

归一化后的协议只有四块（权威定义见 dsh
``docs/design/20261125-01-merged-header-protocol.md``）：

- ``session``（必有）
- ``context_management``（仅 manage 请求）
- ``priority_control``
- ``cache_control``

``agent_status`` / ``prediction`` / ``derivation`` / ``agent_type`` /
``strict_priority`` / ``latency_control`` 以及 ``cache_control[].range`` 全部移除：
线格式只描述“这一轮请求处在会话的什么位置”，不再描述运行中 Agent 的内心状态。
运行中状态是本进程的私事，进程自己知道，引擎不需要，也没有任何一层能兑现它。

``session.state`` 只有 ``start`` / ``append`` 出现在普通请求里；``pause`` /
``resume`` / ``stop`` / ``compact`` 各自占用一次**专用 manage 请求**（见
``build_manage_hint``）。
"""
from types import MappingProxyType

HINT_HEADER = "X-Agent-Hint"
MAX_HINT_BYTES = 4096

SESSION_STATES = ("start", "append", "pause", "resume", "stop", "compact")
MANAGE_STATES = ("pause", "resume", "stop", "compact")

CONTEXT_EDIT_TARGETS = ("session",)
#: manage 动词 -> 上下文编辑动作。``stop`` 与 ``compact`` 都要求引擎丢掉旧内容，
#: 区别只在 ``compact`` 之后同一 epoch 还会继续被引用（故它带 ``epoch - 1``）。
STATE_EDIT = MappingProxyType({
    "pause": "offload",
    "resume": "prefetch",
    "stop": "evict",
    "compact": "evict",
})

CACHE_CONTROL_TYPES = ("ephemeral",)

PRIORITY_INTERACTIVE = 3
PRIORITY_BACKGROUND = 1
PRIORITY_LEVELS = (PRIORITY_BACKGROUND, PRIORITY_INTERACTIVE)
PRIORITY_REASONS = ("user_waiting", "critical_path")
#: 协议要求 level 与 reason 成对出现：3 只能是“有人正在等”，1 只能是“关键路径上的自驱续跑”。
#: 允许自由配对等于允许一个自相矛盾的准入信号，消费侧只能猜。
PRIORITY_REASON_BY_LEVEL = MappingProxyType({
    PRIORITY_INTERACTIVE: "user_waiting",
    PRIORITY_BACKGROUND: "critical_path",
})

TTL_MIN_S = 60
TTL_MAX_S = 3600
TTL_SAFETY_FACTOR = 2
TTL_FALLBACK_GAP_S = 300

#: 引擎按块（128 token）缓存并整块哈希：不足一个块的前缀根本不会被存下来，
#: 为它声明保留等于要求引擎保留一个不存在的东西。
MIN_CACHEABLE_PREFIX_TOKENS = 128

_CJK_RANGES = ((0x2E80, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))
_CHARS_PER_TOKEN = 4


def estimate_prefix_tokens(messages):
    """估算消息前缀的 token 数（无 tokenizer）。

    CJK 按字计 1 token（所服务的 tokenizer 大致如此），其余按 4 字符 1 token。
    只用于判断前缀能否占满一个缓存块，绝不用于推导 TTL。
    """
    return _estimate_tokens(messages)


def _estimate_tokens(value):
    if isinstance(value, str):
        cjk = other = 0
        for ch in value:
            code = ord(ch)
            if any(lo <= code <= hi for lo, hi in _CJK_RANGES):
                cjk += 1
            else:
                other += 1
        return cjk + -(-other // _CHARS_PER_TOKEN)
    if isinstance(value, (list, tuple)):
        return sum(_estimate_tokens(item) for item in value)
    if isinstance(value, dict):
        return sum(_estimate_tokens(item) for item in value.values())
    return 0


def ordinary_state(call_index):
    """普通请求的 ``session.state``：会话的第一个请求 ``start``，其余 ``append``。

    重试沿用同一个 ``call_index``，因此重试仍是 ``start``——它是同一次逻辑请求，
    不是“第二次打开会话”。
    """
    try:
        index = int(call_index)
    except (TypeError, ValueError):
        return "append"
    return "start" if index <= 1 else "append"


def priority_block(level):
    """把任意配置档位折叠进二值域 ``{1, 3}`` 并配好 reason。

    消费侧只拿 ``priority.level`` 与一条准入线比较，多于两档的取值没有对应语义；
    多出来的档位只会让调用方以为存在它们又拿不到的区分。``level >= 3`` 是“有人正在等”
    的交互档，其余一律背景档（v1 遗留的 2/4 折叠后落在它们原本就在的那一侧）。
    ``level <= 0`` 由调用方判定为“整段省略”。
    """
    try:
        value = int(level)
    except (TypeError, ValueError):
        value = PRIORITY_BACKGROUND
    folded = PRIORITY_INTERACTIVE if value >= PRIORITY_INTERACTIVE else PRIORITY_BACKGROUND
    return {"level": folded, "reason": PRIORITY_REASON_BY_LEVEL[folded]}


def request_hint(session_id, call_index, epoch, state, parent_session_id=None,
                 priority=None, cache_control=None):
    """构造普通请求（``/v1/chat/completions`` 主调用）的四块 AgentHint。

    ``state`` 只接受 ``start`` / ``append``：其余四个动词属于 manage 请求，
    混进普通请求会让引擎同时看到“这是新一轮”和“请卸载上下文”两个指令。
    缺省块整段省略（``priority`` / ``cache_control`` 为空即不写）。
    """
    if state not in ("start", "append"):
        raise ValueError("ordinary requests carry start|append, got %r" % (state,))
    session = {
        "session_id": str(session_id),
        "state": state,
        "call_index": int(call_index),
        "epoch": int(epoch),
    }
    if parent_session_id:
        session["parent_session_id"] = str(parent_session_id)
    hint = {"session": session}
    if priority:
        hint["priority_control"] = {"priority": dict(priority)}
    if cache_control:
        hint["cache_control"] = dict(cache_control)
    return hint


def build_manage_hint(session_id, call_index, epoch, state, parent_session_id=None):
    """构造 manage 请求的 hint：``session(state=动词)`` + ``context_management``。

    ``compact`` 描述的是“刚刚被重写掉的那一代上下文”，因此 ``session.epoch`` 带的是
    ``epoch - 1``（下限 0），而 ``edits[]`` 只写 ``type`` 与 ``target``。编辑描述的是
    “对哪个会话做什么”，版本号属于 session 身份本身——两处都写会让消费侧有两份不一致的
    真相。调用方在重写落地时已经把自己的计数器抬高过一次，这里减回去。
    """
    if state not in MANAGE_STATES:
        raise ValueError("manage requests carry %s, got %r" % ("|".join(MANAGE_STATES), state))
    wire_epoch = max(0, int(epoch) - 1) if state == "compact" else int(epoch)
    session = {
        "session_id": str(session_id),
        "state": state,
        "call_index": int(call_index),
        "epoch": wire_epoch,
    }
    if parent_session_id:
        session["parent_session_id"] = str(parent_session_id)
    return {
        "session": session,
        "context_management": {
            "manage_request": True,
            "edits": [{
                "type": STATE_EDIT[state],
                "target": CONTEXT_EDIT_TARGETS[0],
            }],
        },
    }


def manage_body(model):
    """manage 请求的占位体。

    它不携带任何对话内容——管理意图全在 header 里，body 只是让网关有一个合法的
    OpenAI 请求可以路由。消费侧必须在 ``context_management.manage_request`` 为真时
    接受空 ``messages``。
    """
    body = {"messages": [], "stream": False}
    if model:
        body["model"] = str(model)  # 未配置就整段省略，空的 model 会被网关判为非法
    return body


def cache_control(messages, ttl_s=None, end=None):
    """会话前缀的保留声明：**单一对象 + 显式 TTL + 消息偏移**。

    ``msg_offset`` 是本请求的线格式消息条数（``system`` 提示词算第 1 条），也就是
    “下一轮可以复用到第几条”。旧协议的 ``range: {end, unit}`` 已移除：只有一种单位，
    写出来只是多一层嵌套。

    ``ttl_s <= 0`` / 前缀不足一个块（``MIN_CACHEABLE_PREFIX_TOKENS``）/ ``end`` 越界时
    都不产出——没有 TTL 就没有可声明的保留，越界的偏移会让消费侧整条丢弃。
    """
    if not isinstance(messages, list) or not messages:
        return None
    if ttl_s is None or ttl_s <= 0:
        return None
    target = len(messages) if end is None else int(end)
    if target <= 0 or target > len(messages):
        return None
    if estimate_prefix_tokens(messages[:target]) < MIN_CACHEABLE_PREFIX_TOKENS:
        return None
    return {
        "type": CACHE_CONTROL_TYPES[0],
        "ttl_s": int(ttl_s),
        "msg_offset": target,
    }


def estimate_ttl_s(interval_ms=None):
    """按运行时信号估算保留时长：``clamp(round(间隔秒 × 2), 60, 3600)``。

    只认“最近一次请求结束到这次请求开始”的间隔——它才是会话真实的空闲长度。
    没有观测值时退回协议默认 gap（300s → 600s）。

    乘以安全系数后落在协议窗口内：低于下限抬到 60s，高于上限**截断到 3600s**
    （实测：保留生效的充要条件是 ``ttl_s > 空闲间隔``；不声明时 600s 间隔命中 0%，
    声明 3600s 命中 99.8%，因此截断严格优于丢弃）。
    """
    gap = None
    if isinstance(interval_ms, (int, float)) and interval_ms > 0:
        gap = interval_ms / 1000.0
    else:
        gap = TTL_FALLBACK_GAP_S
    raw = gap * TTL_SAFETY_FACTOR
    return max(TTL_MIN_S, min(TTL_MAX_S, int(round(raw))))
