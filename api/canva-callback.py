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
from cover.canva_client import exchange_code_for_token, save_initial_refresh_token


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
  .ok-box {{ background: #f0fdf4; border-left: 4px solid #16a34a; padding: 16px 20px;
             border-radius: 4px; margin: 20px 0; }}
  .warn {{ background: #fff8e1; border-left: 4px solid #ffc107;
           padding: 12px 16px; margin: 20px 0; }}
  a.btn {{ display: inline-block; padding: 12px 24px; background: var(--primary, #ff2741);
           color: white; text-decoration: none; border-radius: 8px; font-weight: 600; }}
</style>
</head>
<body>
<h1>✅ Canva 授权成功</h1>

<div class="ok-box">
  <strong>系统已自动保存 refresh_token 到 Vercel KV,管理员无需任何手动操作。</strong>
  <br>从此刻起,业务可以正常使用"去 Canva 做封面"功能,token 会自动续期。
</div>

<p>你现在可以:</p>
<ul>
  <li>关掉本页面</li>
  <li>回到 <a href="/">网站首页</a> 试一次完整流程(生成文案 → 选标题 → 上传产品图 → 选模板 → 跳转 Canva)</li>
</ul>

<div class="warn">
⚠️ 如果将来出现"refresh token revoked / used twice"等错误,只要再访问一次本授权链接重新授权即可,
系统会自动覆盖新 token。无需碰任何环境变量。
</div>

<p style="text-align:center;margin-top:32px;">
  <a class="btn" href="/" style="background:#ff2741;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;">回首页</a>
</p>
</body>
</html>"""

KV_FAIL_PAGE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>需要先连接 Vercel KV</title></head>
<body style="font-family:sans-serif;max-width:680px;margin:60px auto;padding:0 24px;line-height:1.7">
<h1 style="color:#c0392b">⚠️ Vercel KV 还没接</h1>
<p>OAuth 授权成功了,但系统需要把 refresh_token 持久化保存。请按下面 3 步操作,然后<strong>重新点击 <a href="/api/canva-auth">授权链接</a></strong>:</p>
<ol>
  <li>打开 <a href="https://vercel.com/dashboard" target="_blank">Vercel Dashboard</a> → 你的项目</li>
  <li>顶部菜单 <strong>Storage</strong> → <strong>Create Database</strong> → 选 <strong>KV</strong>(免费)</li>
  <li>命名(如 "agent-cache"),创建后选 <strong>Connect Project</strong> → 选当前项目,三个环境都勾上 → Save。</li>
  <li>Vercel 会自动注入 <code>KV_REST_API_URL</code> 和 <code>KV_REST_API_TOKEN</code>。回 Deployments → Redeploy(不勾 cache)。</li>
</ol>
<p>部署完毕后,再回来访问 <a href="/api/canva-auth">/api/canva-auth</a> 重新走一次授权即可。</p>
<details>
  <summary>报错详情(调试用)</summary>
  <pre style="background:#f5f5f5;padding:12px;overflow:auto">{detail}</pre>
</details>
</body></html>"""

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

        # 直接把 refresh_token 存进 KV,管理员无需做任何手动操作
        try:
            save_initial_refresh_token(refresh)
        except Exception as e:
            return self._html(KV_FAIL_PAGE.format(detail=str(e)))

        return self._html(SUCCESS_PAGE)

    def _html(self, body):
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # 清掉一次性 cookie
        self.send_header("Set-Cookie", "canva_oauth=; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(b)
