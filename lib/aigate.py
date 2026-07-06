"""全局 AI 并发闸(2026-07 性能加固)。

为什么:
    ThreadingHTTPServer 每请求一线程,AI 调用一次占线程 60-190 秒。
    没有全局上限时,突发流量会让几十个线程同时挂在上游(DeepSeek/豆包)上,
    耗尽线程与上游配额。限流(ratelimit)管的是"每人每分钟",
    这里管的是"全服务器同一时刻"。

用法:
    with gate():                      # 在真正发起上游 HTTP 请求处包裹
        urllib.request.urlopen(...)
    拿不到槽位(排队超时)抛 AIBusyError,调用方回 503。

参数(.env 可调):
    AI_MAX_CONCURRENCY  同时在途的上游 AI 请求数上限(默认 4)
    AI_QUEUE_TIMEOUT    排队等槽位的秒数,超时报忙(默认 20)
"""
import os
import threading
from contextlib import contextmanager

_MAX = max(1, int(os.environ.get('AI_MAX_CONCURRENCY', '4')))
_TIMEOUT = float(os.environ.get('AI_QUEUE_TIMEOUT', '20'))
_sem = threading.BoundedSemaphore(_MAX)


class AIBusyError(RuntimeError):
    pass


@contextmanager
def gate():
    ok = _sem.acquire(timeout=_TIMEOUT)
    if not ok:
        raise AIBusyError(f'当前生成人数较多(同时 {_MAX} 路),排队 {int(_TIMEOUT)} 秒仍未轮到,请稍后重试')
    try:
        yield
    finally:
        _sem.release()
