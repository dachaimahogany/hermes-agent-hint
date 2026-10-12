"""X-Agent-Hint 头编解码：UTF-8 JSON -> base64url（去 padding），零第三方依赖。

线格式（见 README “The `X-Agent-Hint` header” 小节）：
序列化为 UTF-8 JSON 后编码为 base64url（去 padding），放入 X-Agent-Hint 头；请求体保持原格式。
"""
import base64
import json


def encode(obj):
    """dict -> base64url 字符串（urlsafe + 去 padding）。"""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(s):
    """base64url 字符串 -> dict（补 padding 后解码；非法输入抛 ValueError）。"""
    padded = s + "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
