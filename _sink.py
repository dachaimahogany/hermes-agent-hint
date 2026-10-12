"""状态事件上报（三模式：ndjson / http / none）。一切失败静默（warn-once），永不抛。

- ndjson：同步追加单行 JSON 到 $HERMES_HOME/agent-hint-events.ndjson（加锁，us 级；
  e2e 依赖其一次性 hermes -z 退出前落盘）。
- http：后台单 worker daemon 串行 + 有界队列（满即丢）+ 单请求超时 2s + 失败重试
  1 次后弃 + atexit 有界 drain（join <=3s）。seq 仅作头、不进 payload。
- none：静默降级。
"""
import atexit
import json
import os
import queue
import sys
import threading
import time

from . import _settings

_ndjson_path = None
_ndjson_lock = threading.Lock()

_http_q = None
_http_worker = None
_http_url = None
_http_lock = threading.Lock()  # _http_queue 初始化互斥（review A4）
_warned = set()
_seq = {}  # session_id -> 已发送事件序号（仅 http 头用）
_seq_lock = threading.Lock()  # _seq 计数互斥（review A4）


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
        _ndjson_path = os.path.join(home, "agent-hint-events.ndjson")
    return _ndjson_path


def _next_seq(sid):
    with _seq_lock:
        n = _seq.get(sid, 0) + 1
        _seq[sid] = n
        return n


def emit(event):
    """分发一条状态事件；永不抛异常。"""
    try:
        mode = _settings.status_sink()
        if mode == "ndjson":
            _emit_ndjson(event)
        elif mode == "http":
            _emit_http(event)
        # none：静默降级
    except Exception as e:
        _warn_once("emit", "emit failed: " + str(e))


def _emit_ndjson(event):
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _ndjson_lock:
        try:
            with open(_ndjson_file(), "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            _warn_once("ndjson", "ndjson append failed: " + str(e))


def _http_queue():
    global _http_q, _http_worker, _http_url
    with _http_lock:  # 双重检查锁，防 TOCTOU 重复建 worker（review A4）
        if _http_worker is not None:
            return _http_q
        _http_url = _settings.status_url()
        if not _http_url:
            return None
        _http_q = queue.Queue(maxsize=256)
        _http_worker = threading.Thread(target=_http_loop, daemon=True)
        _http_worker.start()
        atexit.register(_drain_http)
        return _http_q


def _emit_http(event):
    q = _http_queue()
    if q is None:
        return
    try:
        q.put_nowait(event)
    except queue.Full:
        _warn_once("http_full", "http sink queue full; dropping event")


def _post(url, body, headers, timeout):
    from urllib import request as _req
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = _req.Request(url, data=data, headers=headers, method="POST")
    with _req.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _http_loop():
    while True:
        event = _http_q.get()
        if event is None:
            break
        _post_event(event)


def _post_event(event):
    sid = event.get("session", {}).get("session_id", "")
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Hint-Session": str(sid),
        "X-Agent-Hint-Seq": str(_next_seq(sid)),
    }
    try:
        _post(_http_url, event, headers, timeout=2.0)
    except Exception:
        try:
            _post(_http_url, event, headers, timeout=2.0)  # 重试 1 次
        except Exception as e:
            _warn_once("http", "http sink failed after retry: " + str(e))


def _drain_http():
    if _http_q is None:
        return
    deadline = time.time() + 3.0
    try:
        while time.time() < deadline:
            try:
                event = _http_q.get_nowait()
            except queue.Empty:
                break
            _post_event(event)
    except Exception:
        pass
    try:
        _http_q.put_nowait(None)  # 通知 worker 退出
    except queue.Full:
        pass
