"""
本地 / 生产开发服务器(动态路由版)

它做了 Vercel 同样的事情:
- /              → public/index.html
- /api/<name>    → 自动找 api/<name>.py 调用对应 handler
- /public/*      → 静态文件

新增 API 端点只需要在 api/ 下放一个 <name>.py,不用改这个文件。

启动:
    python3 scripts/dev_server.py
环境变量:
    PORT (默认 8000)
"""
import os
import sys
import importlib.util
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("PORT", "8000"))


def load_env():
    """简单的 .env 加载,无需 python-dotenv 依赖"""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_HANDLER_CACHE = {}
_HANDLER_LOCK = __import__('threading').Lock()

# 请求体大小上限(字节):防止超大 body 打爆内存。
# 封面生成会带 base64 图片(单张上限 4MB),留足冗余取 12MB。
MAX_BODY_SIZE = int(os.environ.get("MAX_BODY_SIZE", str(12 * 1024 * 1024)))


def load_handler(api_file: str):
    """动态加载 api/*.py 的 handler 类,并缓存(避免每次请求重新 exec 模块、重复 import PIL 等)。
    代码更新在部署时随进程重启生效。"""
    cached = _HANDLER_CACHE.get(api_file)
    if cached is not None:
        return cached
    with _HANDLER_LOCK:   # 首次并发加载时避免同一模块被 exec 两次
        cached = _HANDLER_CACHE.get(api_file)
        if cached is not None:
            return cached
        path = ROOT / "api" / api_file
        spec = importlib.util.spec_from_file_location(f"api_{api_file}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _HANDLER_CACHE[api_file] = mod.handler
        return mod.handler


def resolve_api_file(path: str):
    """
    把请求 path 映射到 api/*.py 文件名。
      /api/login              → login.py
      /api/cover-fields       → cover-fields.py
      /api/cover-generate?... → cover-generate.py
      /api/admin-stats        → admin-stats.py
    返回 None 表示不是 API 请求或文件不存在。
    """
    if not path.startswith("/api/"):
        return None
    rest = path[len("/api/"):]
    # 去掉 query string 和后续路径
    name = rest.split("?")[0].split("/")[0].strip()
    if not name:
        return None
    api_file = f"{name}.py"
    if (ROOT / "api" / api_file).exists():
        return api_file
    return None


class Router(BaseHTTPRequestHandler):
    # 允许对外提供的静态文件后缀白名单;不在名单内的一律 404,
    # 避免把 .py / .env / .db 等源码或配置当静态文件下载。
    _STATIC_CTYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }
    _PUBLIC_DIR = (ROOT / "public").resolve()

    def _serve_static(self, rel_path):
        # 防路径穿越:归一化后必须仍在 public/ 目录内。
        # 直接挡掉 ../ 逃逸(如 /../.env、/../data/users.json),不依赖前置 Nginx 归一化。
        from urllib.parse import unquote
        rel_path = unquote(rel_path or "")
        full = (self._PUBLIC_DIR / rel_path).resolve()
        if full != self._PUBLIC_DIR and self._PUBLIC_DIR not in full.parents:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return
        if not full.exists() or not full.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        ext = full.suffix.lower()
        # 后缀白名单:未知类型不提供,避免源码/配置被下载
        if ext not in self._STATIC_CTYPES:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", self._STATIC_CTYPES[ext])
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(full.read_bytes())

    def _proxy_to_api(self, api_file):
        import json
        # 请求体大小上限:超限直接 413,避免超大 body 读入内存(DoS)。
        try:
            clen = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            clen = 0
        if clen > MAX_BODY_SIZE:
            self.send_response(413)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "请求体过大"}, ensure_ascii=False).encode("utf-8"))
            return
        try:
            HandlerCls = load_handler(api_file)
        except Exception as e:
            # 详情只进服务端日志,客户端给通用文案,避免泄露路径/栈信息
            sys.stderr.write(f"[LOAD-ERROR] {api_file}: {e}\n")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "服务暂时不可用,请稍后再试"}, ensure_ascii=False).encode("utf-8"))
            return

        # 构造一个 handler 实例,代理本次请求的 IO。
        # wfile 包一层字节计数:handler 半途抛异常时,只有"一个字节都没写出"
        # 才补写错误响应,避免在已写出的响应后面追加垃圾字节(畸形报文)。
        class _CountingW:
            def __init__(s, raw): s.raw, s.written = raw, 0
            def write(s, b):
                s.written += len(b)
                return s.raw.write(b)
            def __getattr__(s, name): return getattr(s.raw, name)
        wcount = _CountingW(self.wfile)
        proxy = HandlerCls.__new__(HandlerCls)
        proxy.rfile = self.rfile
        proxy.wfile = wcount
        proxy.headers = self.headers
        proxy.command = self.command
        proxy.path = self.path
        proxy.client_address = self.client_address
        proxy.request_version = self.request_version
        proxy.server = self.server
        proxy.connection = self.connection
        proxy.requestline = self.requestline

        method = getattr(proxy, f"do_{self.command}", None)
        if method is None:
            self.send_response(405)
            self.end_headers()
            return
        try:
            method()
        except Exception as e:
            # 详情只进服务端日志;只有 handler 完全没写出任何字节时才补一个完整错误响应
            sys.stderr.write(f"[HANDLER-ERROR] {api_file}: {e}\n")
            import traceback; traceback.print_exc(file=sys.stderr)
            if wcount.written == 0:
                try:
                    body = json.dumps({"error": "服务器内部错误"}, ensure_ascii=False).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass
            else:
                # 已写出部分响应,无法补救,直接断开这条连接
                try:
                    self.close_connection = True
                except Exception:
                    pass

    def _handle_any(self):
        """统一入口:优先尝试 API 路由,再走静态文件"""
        api_file = resolve_api_file(self.path)
        if api_file:
            return self._proxy_to_api(api_file)

        # 静态文件
        # 根路径 / 走门户 (portal.html);/index.html 直接走文案生成
        if self.path == "/":
            return self._serve_static("portal.html")
        if self.path == "/index.html":
            return self._serve_static("index.html")
        if self.path.startswith("/public/"):
            return self._serve_static(self.path[len("/public/"):])
        # 兜底:把根路径剩余部分当成 public 下的文件
        return self._serve_static(self.path.lstrip("/"))

    def do_GET(self):
        self._handle_any()

    def do_POST(self):
        api_file = resolve_api_file(self.path)
        if api_file:
            return self._proxy_to_api(api_file)
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def do_OPTIONS(self):
        api_file = resolve_api_file(self.path)
        if api_file:
            return self._proxy_to_api(api_file)
        # 默认 CORS preflight 响应
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.command}] {self.path}\n")


def main():
    load_env()

    # 安全硬性前提:会话签名密钥与身份证哈希盐必须显式配置。
    # 缺失即拒绝启动 —— 否则历史上会静默回退到云 API Key 当签名密钥
    # (API Key 泄露 = 可伪造任意管理员 Token;轮换云密钥 = 全站掉线)。
    missing = [k for k in ("SESSION_SECRET", "ID6_SALT") if not os.environ.get(k)]
    if missing:
        print("❌ 拒绝启动:缺少必需环境变量 " + ", ".join(missing))
        print("   请在 .env 中补上(生成随机串:python3 -c \"import secrets;print(secrets.token_hex(32))\")")
        print("   注意:首次配置/更换 SESSION_SECRET 会使全员登录态失效,需重新登录(数据无影响)。")
        sys.exit(1)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("⚠️  警告:DEEPSEEK_API_KEY 未配置,生成接口会返回 500。\n")

    if not (ROOT / "data" / "products.json").exists():
        print("⚠️  data/products.json 不存在,正在生成...")
        os.system(f'python3 "{ROOT / "scripts" / "build_products.py"}"')

    # 列出注册到的 API 端点
    api_dir = ROOT / "api"
    apis = sorted(p.stem for p in api_dir.glob("*.py") if p.is_file())
    print(f"🚀 服务已启动: http://0.0.0.0:{PORT}")
    print(f"   注册的 API 端点 ({len(apis)} 个):")
    for a in apis:
        print(f"     - /api/{a}")
    print(f"   按 Ctrl+C 停止\n")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Router)
    server.daemon_threads = True     # 请求并发处理:图片/接口互不阻塞;慢的领取生成也不再卡住图片
    server.serve_forever()


if __name__ == "__main__":
    main()
