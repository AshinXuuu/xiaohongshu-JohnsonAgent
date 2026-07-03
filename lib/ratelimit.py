"""付费 AI 接口的限流 / 每日配额。

为什么需要:
    generate / qa / cover-generate / cover-fields 每次调用都会消耗 DeepSeek / 豆包额度。
    虽然现在都要求登录了,但任一有效员工令牌仍可无限刷,烧钱(经济型 DoS)。
    这里加两道闸:
      1) 突发限流:每人每分钟最多 N 次(滑动窗口,内存计数)。
      2) 每日配额:每人每自然日最多 M 次(落 usage.db,重启不清零)。

设计:
    - 单进程 systemd 常驻服务,内存滑动窗口足够;每日配额落库以防重启绕过。
    - 限流键用 emp_id(已认证身份),取不到时回退到 IP,保证匿名兜底也有约束。
    - 各上限可用环境变量覆盖,便于线上按额度调参:
        RL_PER_MIN(默认 10)  RL_PER_DAY(默认 200)
    - 任何异常都 fail-open(放行),绝不因限流器故障阻断正常业务。
"""
import os
import time
import threading

_PER_MIN = int(os.environ.get('RL_PER_MIN', '10'))
_PER_DAY = int(os.environ.get('RL_PER_DAY', '200'))
_WINDOW = 60  # 秒

_lock = threading.Lock()
# key -> [时间戳,...](仅保留窗口内)
_hits = {}


def _today():
    return time.strftime('%Y-%m-%d', time.localtime())


def _burst_ok(key: str) -> bool:
    """滑动窗口:窗口内命中数 < 上限则放行并记一次。"""
    now = time.time()
    with _lock:
        arr = _hits.get(key)
        if arr is None:
            arr = []
            _hits[key] = arr
        # 清掉窗口外的
        cutoff = now - _WINDOW
        i = 0
        for i in range(len(arr)):
            if arr[i] >= cutoff:
                break
        else:
            i = len(arr)
        if i:
            del arr[:i]
        if len(arr) >= _PER_MIN:
            return False
        arr.append(now)
        # 顺手做个粗清理,避免 _hits 无限增长
        if len(_hits) > 5000:
            for k in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
                _hits.pop(k, None)
        return True


def _day_ok(key: str) -> bool:
    """每日配额:落 usage.db 的 rate_daily(key, day, count)。达上限返回 False。"""
    try:
        from lib.kv_store import _get_conn  # 复用现有连接(WAL)
    except Exception:
        return True  # 拿不到 DB 就放行,fail-open
    day = _today()
    try:
        conn = _get_conn()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rate_daily("
                "key TEXT, day TEXT, count INTEGER, PRIMARY KEY(key, day))")
            cur = conn.execute(
                "SELECT count FROM rate_daily WHERE key=? AND day=?", (key, day))
            row = cur.fetchone()
            used = (row[0] if row else 0)
            if used >= _PER_DAY:
                return False
            conn.execute(
                "INSERT INTO rate_daily(key, day, count) VALUES(?,?,1) "
                "ON CONFLICT(key, day) DO UPDATE SET count = count + 1",
                (key, day))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return True  # 任何 DB 异常都放行,不阻断业务


def check(user: dict, ip: str = '', action: str = 'ai'):
    """返回 (allowed: bool, error_msg: str|None)。
    allowed=False 时,调用方应回 429 + error_msg。
    """
    emp = ''
    try:
        emp = (user or {}).get('emp_id') or ''
    except Exception:
        emp = ''
    key = f"{action}:{emp or ('ip:' + (ip or 'unknown'))}"
    try:
        if not _burst_ok(key):
            return False, f"操作太频繁,请稍后再试(每分钟最多 {_PER_MIN} 次)"
        if not _day_ok(key):
            return False, f"今日生成次数已达上限({_PER_DAY} 次),请明天再来或联系管理员"
    except Exception:
        return True, None  # fail-open
    return True, None
