"""
本地开发服务器,用于在部署到 Vercel 之前本地试运行。

使用方法:
    1. cp .env.example .env  并填入 DEEPSEEK_API_KEY
    2. python scripts/dev_server.py
    3. 浏览器打开 http://localhost:8000

它做了 Vercel 同样的事情:
- /            → public/index.html
- /api/products → api/products.py
- /api/generate → api/generate.py
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
    spec = importlib.util.spec_from_file_location(api_file, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handler


class Router(BaseHTTPRequestHandler):
    def _serve_static(self, rel_path):
        full = ROOT / "public" / rel_path
        if not full.exists() or not full.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        ext = full.suffix.lower()
        ctypes = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript"}
        self.send_response(200)
        self.send_header("Content-Type", ctypes.get(ext, "application/octet-stream"))
        self.end_headers()
        self.wfile.write(full.read_bytes())

    def _proxy_to_api(self, api_file):
        try:
            HandlerCls = load_handler(api_file)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Failed to load {api_file}: {e}".encode("utf-8"))
            return

        # 让 BaseHTTPRequestHandler 的子类去处理这个请求
        # 直接把当前请求"重新派发"给目标 handler
        HandlerCls.__init__ = BaseHTTPRequestHandler.__init__  # 防止覆盖
        # 通过模拟方式调用:重新构造一个 handler 对象,代理 self 的 request/wfile
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
        # 调用对应方法
        method = getattr(proxy, f"do_{self.command}", None)
        if method is None:
            self.send_response(405)
            self.end_headers()
            return
        method()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            return self._serve_static("index.html")
        if self.path.startswith("/api/products"):
            return self._proxy_to_api("products.py")
        if self.path.startswith("/public/"):
            return self._serve_static(self.path[len("/public/"):])
        # static fallback
        return self._serve_static(self.path.lstrip("/"))

    def do_POST(self):
        if self.path.startswith("/api/generate"):
            return self._proxy_to_api("generate.py")
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        if self.path.startswith("/api/"):
            target = "generate.py" if "generate" in self.path else "products.py"
            return self._proxy_to_api(target)
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        # 简洁日志
        sys.stderr.write(f"[{self.command}] {self.path} → {args[1] if len(args)>1 else ''}\n")


def main():
    load_env()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("⚠️  警告:DEEPSEEK_API_KEY 未配置,生成接口会返回 500。")
        print("   请创建 .env 文件并填入 key,或导出环境变量。\n")

    if not (ROOT / "data" / "products.json").exists():
        print("⚠️  data/products.json 不存在,正在生成...")
        os.system(f'python3 "{ROOT / "scripts" / "build_products.py"}"')

    print(f"🚀 本地服务已启动: http://localhost:{PORT}")
    print(f"   按 Ctrl+C 停止\n")
    HTTPServer(("0.0.0.0", PORT), Router).serve_forever()


if __name__ == "__main__":
    main()
