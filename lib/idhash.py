"""身份证后 6 位的加盐哈希(keyed HMAC)。

为什么不用普通 sha256:
    后 6 位只有 100 万种组合,普通哈希可离线暴力反推。
    这里用带服务端密钥的 HMAC-SHA256:只有拿到 ID6_SALT 才能算/验,
    仅拿到仓库或数据库(明文已被替换为哈希)无法反推。

存储格式:
    "h1$" + hmac_sha256_hex   —— 前缀用于区分“已哈希 / 遗留明文”,便于平滑迁移。

兼容:
    verify_id6 对遗留明文仍能校验,保证迁移期间无人被锁在门外。
"""
import os
import hmac
import hashlib

_PREFIX = 'h1$'


def _key() -> bytes:
    s = (os.environ.get('ID6_SALT')
         or os.environ.get('SESSION_SECRET')
         or os.environ.get('DEEPSEEK_API_KEY'))
    if not s:
        raise RuntimeError('ID6 哈希密钥未配置,请在 .env 设置 ID6_SALT')
    return s.encode('utf-8')


def _norm(raw) -> str:
    # 末位可能是 X,统一大写;去空白
    return str(raw or '').strip().upper()


def is_hashed(stored) -> bool:
    return isinstance(stored, str) and stored.startswith(_PREFIX)


def hash_id6(raw) -> str:
    """把后 6 位转成存储用哈希;空值返回空串;已是哈希则原样返回(幂等)。"""
    v = _norm(raw)
    if not v:
        return ''
    if v.startswith(_PREFIX):
        return v
    return _PREFIX + hmac.new(_key(), v.encode('utf-8'), hashlib.sha256).hexdigest()


def verify_id6(raw_input, stored):
    """校验用户输入是否匹配存储值。
    返回:
        True  —— 已配置且匹配
        False —— 已配置但不匹配
        None  —— 未配置(调用方决定是否放行,维持过渡期宽松)
    """
    stored = stored or ''
    if not stored:
        return None
    inp = _norm(raw_input)
    if not inp:
        return False
    if is_hashed(stored):
        return hmac.compare_digest(hash_id6(inp), stored)
    # 遗留明文(迁移前)——仍可校验
    return hmac.compare_digest(inp, _norm(stored))
