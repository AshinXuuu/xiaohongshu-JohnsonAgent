"""服务端会话 Token(无状态 HMAC 签名)。

为什么需要它:
    以前所有接口靠前端在请求体里带明文 {部门+姓名+工号} 来证明身份,
    这三项都能被匿名枚举/伪造 —— 任何人都能冒充管理员。
    现在改为:登录成功后由服务端签发一个带签名、有时效的 Token,
    之后每个请求带 `Authorization: Bearer <token>`,服务端验签才认。
    前端无法伪造(不知道 SESSION_SECRET),也改不动 localStorage 越权。

设计要点:
    - 无状态:Token 自带签名,不依赖数据库/共享 session,契合 Vercel 多实例 Serverless。
    - 身份 vs 权限:Token 只承载"已认证的身份"(部门/姓名/工号);
      具体能做什么(role)仍由各接口用 lib.auth 实时查库判定,改角色即时生效。
    - 密钥:优先读 SESSION_SECRET;未配置时回退到已有的高熵密钥,
      再不行则用进程内随机值(会导致重启后需重新登录,属于 fail-safe)。
"""
import os
import hmac
import json
import time
import base64
import hashlib
import secrets as _secrets

# 24 小时,与前端会话有效期一致
TTL_SECONDS = 24 * 60 * 60


def _secret() -> bytes:
    """签名密钥。生产环境请在 Vercel 配置 SESSION_SECRET(任意长随机串)。"""
    s = (
        os.environ.get("SESSION_SECRET")
        or os.environ.get("COS_SECRET_KEY")      # 回退:已有的高熵密钥
        or os.environ.get("DEEPSEEK_API_KEY")
    )
    if not s:
        # 最后兜底:进程内随机值。Token 无法跨实例/重启验证 → 用户需重新登录,
        # 但绝不使用可预测的硬编码密钥(避免伪造)。
        global _EPHEMERAL
        try:
            s = _EPHEMERAL
        except NameError:
            s = _EPHEMERAL = _secrets.token_hex(32)
            print("[SESSION] 警告:未配置 SESSION_SECRET,使用进程内随机密钥,"
                  "多实例/重启后登录态会失效。请尽快在环境变量设置 SESSION_SECRET。", flush=True)
    return s.encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str) -> str:
    mac = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64url_encode(mac.digest())


def issue_token(user: dict) -> str:
    """登录成功后签发 Token。user 需含 department / name / emp_id。"""
    now = int(time.time())
    payload = {
        "d": user.get("department"),
        "n": user.get("name"),
        "e": str(user.get("emp_id") or ""),
        "r": user.get("role") or "staff",   # 仅作参考,鉴权仍实时查库
        "o": user.get("org") or "johnson",
        "iat": now,
        "exp": now + TTL_SECONDS,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return payload_b64 + "." + _sign(payload_b64)


def verify_token(token: str):
    """验签 + 校验有效期。通过则返回身份 dict,否则 None。"""
    if not token or "." not in token:
        return None
    payload_b64, sig = token.rsplit(".", 1)
    expected = _sign(payload_b64)
    # 定长比较,防时序攻击
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return {
        "department": payload.get("d"),
        "name": payload.get("n"),
        "emp_id": payload.get("e"),
        "role": payload.get("r") or "staff",
        "org": payload.get("o") or "johnson",
    }


def user_from_headers(headers):
    """从请求头 Authorization: Bearer <token> 取出已认证身份;失败返回 None。

    headers 为 http.server 的 headers 对象(支持 .get)。
    """
    try:
        auth = headers.get("Authorization") or headers.get("authorization") or ""
    except Exception:
        auth = ""
    if not auth.lower().startswith("bearer "):
        return None
    return verify_token(auth[7:].strip())
