"""
GET /api/canva-auth — 发起 Canva OAuth

行为:
  1. 生成 PKCE verifier/challenge 和 state
  2. 用一个签名 cookie 把 verifier 临时存起来(回调时取出)
  3. 302 跳转到 Canva 授权页

这一步主要给"运营管理员"用 — 一个人授权一次,把 refresh_token 存到 Vercel env 后,
所有业务后续都共用这个 token,不需要每个业务自己授权。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import sys
import hmac
import hashlib
import json
import time
import base64

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cover.canva_client import gen_pkce, build_authorize_url


def sign_cookie(payload: dict, secret: str) -> str:
    """用 client_secret 签一个简易 JWT-like cookie"""
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{raw}.{sig}"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        client_id = os.environ.get("CANVA_CLIENT_ID", "")
        client_secret = os.environ.get("CANVA_CLIENT_SECRET", "")
        redirect_uri = os.environ.get("CANVA_REDIRECT_URI", "")
        if not client_id or not client_secret or not redirect_uri:
            return self._error("环境变量缺失: CANVA_CLIENT_ID / CANVA_CLIENT_SECRET / CANVA_REDIRECT_URI")

        verifier, challenge = gen_pkce()
        state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
        cookie = sign_cookie({"v": verifier, "s": state, "t": int(time.time())}, client_secret)

        auth_url = build_authorize_url(client_id, redirect_uri, challenge, state)

        self.send_response(302)
        self.send_header("Location", auth_url)
        # cookie 用 SameSite=Lax 才能在跨站重定向回来时仍然带上
        self.send_header(
            "Set-Cookie",
            f"canva_oauth={cookie}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax",
        )
        self.end_headers()

    def _error(self, msg):
        body = msg.encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
