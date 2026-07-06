"""统一的 SQLite 连接管理(2026-07 性能加固)。

为什么:
    此前 6 个 store 模块各自复制 _conn(),每次函数调用都
    connect → PRAGMA → close;一次看板请求会开几十个连接(N+1 连接风暴),
    且 synchronous 档位各模块不一致。

方案:
    - 每线程复用一个连接(threading.local)。ThreadingHTTPServer 每请求一线程,
      正好一请求一连接;连接跨请求复用,PRAGMA 只执行一次。
    - 各 store 的 `_conn()` 改为调用这里的 get_conn();原有代码里成百处
      `finally: c.close()` 不必改 —— 返回的是 _ConnProxy,close() 不断开连接,
      只在有未提交事务时回滚(语义与真 close 一致:未提交的丢弃)。
    - 统一 PRAGMA:WAL + synchronous=NORMAL(WAL 下安全且明显更快)+ busy_timeout。

注意:
    - 显式事务(BEGIN IMMEDIATE ... COMMIT/ROLLBACK)照常工作,勿跨"close"留事务。
    - 单文件数据库单写者,写锁竞争由 busy_timeout 缓冲。
"""
import os
import sqlite3
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_local = threading.local()


def _db_path():
    # 每次现算:测试/脚本会通过环境变量切换数据库路径
    return os.environ.get('USAGE_DB_PATH', str(_ROOT / 'data' / 'usage.db'))


class _ConnProxy:
    """把 close() 变成「回滚未提交事务但保持连接」的代理,其余全部透传。"""
    __slots__ = ('_c',)

    def __init__(self, c):
        self._c = c

    def close(self):
        try:
            if self._c.in_transaction:
                self._c.rollback()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __setattr__(self, name, value):
        if name == '_c':
            object.__setattr__(self, name, value)
        else:
            setattr(self._c, name, value)   # 如 row_factory / isolation_level


def get_conn(row_factory=sqlite3.Row):
    """取当前线程的复用连接(路径变化时自动重建)。"""
    path = _db_path()
    c = getattr(_local, 'conn', None)
    if c is None or getattr(_local, 'path', None) != path:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=10, check_same_thread=False)
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        c.execute('PRAGMA busy_timeout=10000')
        _local.conn = c
        _local.path = path
    c.row_factory = row_factory
    return _ConnProxy(c)
