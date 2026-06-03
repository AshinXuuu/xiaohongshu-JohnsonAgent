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
from http.server import BaseHTTPRequestHandler, HTTPServer

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


def load_handler(api_file: str):
    """动态加载 api/*.py 的 handler 类"""
    path = ROOT / "api" / api_file
    # 使用唯一 module name 避免缓存冲突
    spec = importlib.util.spec_from_file_location(f"api_{api_file}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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
    def _serve_static(self, rel_path):
        full = ROOT / "public" / rel_path
        if not full.exists() or not full.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        ext = full.suffix.lower()
        ctypes = {
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
        self.send_response(200)
        self.send_header("Content-Type", ctypes.get(ext, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(full.read_bytes())

    def _proxy_to_api(self, api_file):
        try:
            HandlerCls = load_handler(api_file)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            import json
            self.wfile.write(json.dumps({"error": f"Failed to load {api_file}: {e}"}).encode("utf-8"))
            return

        # 构造一个 handler 实例,代理本次请求的 IO
        proxy = HandlerCls.__new__(HandlerCls)
        proxy.rfile = self.rfile
        proxy.wfile = self.wfile
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
            try:
                import json
                self.wfile.write(json.dumps({"error": f"Handler error: {e}"}).encode("utf-8"))
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
    HTTPServer(("0.0.0.0", PORT), Router).serve_forever()


if __name__ == "__main__":
    main()
