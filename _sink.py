"""manage 请求的出口（ndjson / http / none）。一切失败静默（warn-once），永不抛。

manage 请求是**一次真实的 HTTP 调用**：``POST <manage_url>``，占位体
``{"model": ..., "messages": [], "stream": false}``，管理意图全在 ``X-Agent-Hint``
头里。旧协议的“异步状态上报”通道（``agent_status`` 事件流）已退役——引擎从来不消费
“Agent 现在在跑工具”这种事实，它只需要知道“这一轮之后请把上下文卸载掉”。

- ndjson：同步追加单行 JSON 到 ``$HERMES_HOME/agent-hint-manage.ndjson``（加锁，us 级；
  e2e 与排障依赖它在进程退出前落盘）。
- http：后台单 worker daemon 串行 + 有界队列（满即丢）+ 超时 + 失败重试后弃 +
  atexit 有界 drain。
- none：静默降级（缺省：没配 URL 就一个字节都不发）。
"""
import atexit
import json
import os
import queue
import sys
import threading
import time

from . import _builder, _codec, _settings

_ndjson_path = None
_ndjson_lock = threading.Lock()

_http_q = None
_http_worker = None
_http_url = None
_http_lock = threading.Lock()  # 队列初始化互斥
_warned = set()
_last_state = {}  # session_id -> 上一个已下发的 manage 动词
_last_lock = threading.Lock()


def _warn_once(key, msg):
    if key in _warned:
        return
    _warned.add(key)
    try:
        sys.stderr.write("[agent-hint] " + msg + "\n")
    except Exception:
        pass


def _ndjson_file():
    global _ndjson_path
    if _ndjson_path is None:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        _ndjson_path = os.path.join(home, "agent-hint-manage.ndjson")
    return _ndjson_path


def _should_send(sid, state):
    """连续同一个动词只发一次。

    触碰两次 ``pause``（先审批 A、再审批 B）之间没有任何状态变化，第二次卸载请求不会
    带来新的信息；而 ``pause -> resume -> pause`` 里的第二个 ``pause`` 是真变化，照发。
    """
    with _last_lock:
        if _last_state.get(sid) == state:
            return False
        _last_state[sid] = state
        return True


def reset_state():
    """测试/回收辅助：清空去重表（不动发件队列）。"""
    with _last_lock:
        _last_state.clear()


def dispatch(hint, model=None):
    """下发一次 manage 请求；返回是否真的发出。永不抛异常。"""
    try:
        session = hint.get("session") or {}
        sid = str(session.get("session_id") or "")
        state = str(session.get("state") or "")
        if not sid or not state:
            return False
        mode = _settings.manage_sink()
        if mode == "none":
            return False
        if not _should_send(sid, state):
            return False
        payload = {
            "hint": hint,
            "body": _builder.manage_body(model or _settings.manage_model()),
            "headers": {
                "Content-Type": "application/json",
                "X-Agent-Hint": _codec.encode(hint),
            },
        }
        token = _settings.manage_token()
        if token:
            payload["headers"]["Authorization"] = "Bearer " + token
        if mode == "ndjson":
            _emit_ndjson(payload)
            return True
        return _emit_http(payload)
    except Exception as e:
        _warn_once("dispatch", "manage dispatch failed: " + str(e))
        return False


def _emit_ndjson(payload):
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _ndjson_lock:
        try:
            with open(_ndjson_file(), "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            _warn_once("ndjson", "ndjson append failed: " + str(e))


def _http_queue():
    global _http_q, _http_worker, _http_url
    with _http_lock:  # 双重检查锁，防 TOCTOU 重复建 worker
        if _http_worker is not None:
            return _http_q
        _http_url = _settings.manage_url()
        if not _http_url:
            return None
        _http_q = queue.Queue(maxsize=_settings.manage_queue_limit())
        _http_worker = threading.Thread(target=_http_loop, daemon=True)
        _http_worker.start()
        atexit.register(_drain_http)
        return _http_q


def _emit_http(payload):
    q = _http_queue()
    if q is None:
        return False
    try:
        q.put_nowait(payload)
        return True
    except queue.Full:
        _warn_once("http_full", "manage sink queue full; dropping request")
        return False


def _post(url, body, headers, timeout):
    from urllib import request as _req
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = _req.Request(url, data=data, headers=headers, method="POST")
    with _req.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _http_loop():
    while True:
        payload = _http_q.get()
        if payload is None:
            break
        _post_payload(payload)


def _post_payload(payload):
    attempts = _settings.manage_max_retries() + 1
    timeout = float(_settings.manage_timeout_s())
    for attempt in range(attempts):
        try:
            _post(_http_url, payload["body"], payload["headers"], timeout)
            return True
        except Exception as e:
            if attempt + 1 >= attempts:
                _warn_once("http", "manage sink failed after %d attempt(s): %s" % (attempts, e))
                return False
    return False


def _drain_http():
    if _http_q is None:
        return
    deadline = time.time() + 3.0
    try:
        while time.time() < deadline:
            try:
                payload = _http_q.get_nowait()
            except queue.Empty:
                break
            if payload is None:
                continue
            _post_payload(payload)
    except Exception:
        pass
    try:
        _http_q.put_nowait(None)  # 通知 worker 退出
    except queue.Full:
        pass
