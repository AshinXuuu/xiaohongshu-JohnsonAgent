"""
GET /api/canva-callback?code=...&state=...

Canva 授权完成后跳回来。我们做两件事:
  1. 用 code 换 access_token + refresh_token
  2. 展示一个 HTML 页面,把 refresh_token 显示出来,提示运营把它复制到 Vercel env

之所以不直接存到 db,是因为 Vercel serverless 没有持久存储,而且这一步是"一次性管理操作",
管理员手动复制 token 到环境变量是最稳的方式。
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import os
import sys
import json
import base64
import hmac
import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cover.canva_client import exchange_code_for_token


def verify_cookie(cookie_value: str, secret: str):
    try:
        raw, sig = cookie_value.split(".")
        expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            return None
        padding = "=" * (-len(raw) % 4)
        return json.loads(base64.urlsafe_b64decode(raw + padding).decode())
    except Exception:
        return None


def parse_cookies(cookie_header: str):
    out = {}
    if not cookie_header:
        return out
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


SUCCESS_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>Canva 集成完成</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; max-width: 720px;
         margin: 60px auto; padding: 0 24px; color: #222; line-height: 1.6; }}
  h1 {{ color: #00C4CC; }}
  .token {{ background: #f5f5f5; padding: 18px; border-radius: 8px;
            font-family: monospace; word-break: break-all; font-size: 13px;
            border: 1px dashed #aaa; margin: 16px 0; }}
  code {{ background: #eef; padding: 2px 6px; border-radius: 4px; }}
  ol li {{ margin-bottom: 12px; }}
  .warn {{ background: #fff8e1; border-left: 4px solid #ffc107; padding: 12px 16px; }}
</style>
</head>
<body>
<h1>✅ Canva 授权成功</h1>

<p>下面是这次拿到的 <strong>Refresh Token</strong>,有效期约 1 年,需要你做一次性配置:</p>

<div class="token" id="tok">{refresh_token}</div>

<button onclick="navigator.clipboard.writeText(document.getElementById('tok').textContent.trim());this.textContent='已复制 ✓'">
点击复制
</button>

<h2>接下来怎么用</h2>
<ol>
  <li>打开 Vercel 项目 <strong>Settings → Environment Variables</strong></li>
  <li>找到 <code>CANVA_REFRESH_TOKEN</code>(如果不存在就新建),把上面的值粘进 Value</li>
  <li>Production / Preview / Development 三个环境都勾选,Save</li>
  <li>进 Deployments → 最近一次 Redeploy(不勾 cache),等部署完成</li>
  <li>之后业务点"去 Canva 做封面",后台自动用这个 token 调 API,业务无感</li>
</ol>

<div class="warn">
⚠️ 这个 token 等同于"以你的 Canva 账号操作"的密钥,不要泄漏。
如果不小心泄漏,去 Canva Developer 后台 Revoke 后重新走授权流程即可。
</div>
</body>
</html>"""

ERROR_PAGE = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>授权失败</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:60px auto;padding:0 24px">
<h1 style="color:#c0392b">⚠️ Canva 授权失败</h1>
<p>{msg}</p>
<p><a href="/api/canva-auth">重试授权</a></p>
</body></html>"""


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        client_id = os.environ.get("CANVA_CLIENT_ID", "")
        client_secret = os.environ.get("CANVA_CLIENT_SECRET", "")
        redirect_uri = os.environ.get("CANVA_REDIRECT_URI", "")

        q = parse_qs(urlparse(self.path).query)
        code = q.get("code", [""])[0]
        state_param = q.get("state", [""])[0]
        error = q.get("error", [""])[0]

        if error:
            return self._html(ERROR_PAGE.format(msg=f"Canva 返回错误:{error}"))
        if not code:
            return self._html(ERROR_PAGE.format(msg="没收到 code 参数,请重新发起授权"))

        # 验证 cookie 里的 verifier 和 state
        cookies = parse_cookies(self.headers.get("Cookie", ""))
        payload = verify_cookie(cookies.get("canva_oauth", ""), client_secret)
        if not payload:
            return self._html(ERROR_PAGE.format(msg="OAuth 状态校验失败(cookie 缺失或过期),请重新发起授权"))
        if payload.get("s") != state_param:
            return self._html(ERROR_PAGE.format(msg="state 不匹配,可能是 CSRF 攻击,已拒绝"))

        try:
            token = exchange_code_for_token(
                client_id, client_secret,
                code=code,
                code_verifier=payload["v"],
                redirect_uri=redirect_uri,
            )
        except Exception as e:
            return self._html(ERROR_PAGE.format(msg=f"换取 token 失败:{e}"))

        refresh = token.get("refresh_token", "")
        if not refresh:
            return self._html(ERROR_PAGE.format(msg="Canva 没返回 refresh_token,可能 scope 不对"))

        return self._html(SUCCESS_PAGE.format(refresh_token=refresh))

    def _html(self, body):
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # 清掉一次性 cookie
        self.send_header("Set-Cookie", "canva_oauth=; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(b)
