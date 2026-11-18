"""Default-browser sign-in with an explicitly installed, local-only extension bridge."""
import asyncio
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import re
import secrets
import threading
import time
import webbrowser
import subprocess
import sys
from pathlib import Path


PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="gemini-terminal-login" content="1"><title>Gemini 终端登录连接</title>
<body style="max-width:680px;margin:60px auto;padding:20px;font:18px/1.8 system-ui">
<h1>连接 Gemini 终端</h1><p id="status">正在等待浏览器扩展……</p>
<p>首次使用：在 Chrome 或 Edge 的扩展管理页面，打开开发者模式，选择“加载已解压的扩展”，选中程序包内的 <b>browser-extension</b> 文件夹，然后刷新本页。</p>
<p>扩展会读取 Google 登录会话，仅传给本机正在运行的终端。成功连接后自动进入 Gemini 官方网页。</p>
<p><a href="https://gemini.google.com/app" target="_blank" rel="noreferrer">打开 Gemini 官方网页登录</a></p>
<p>网页登录本身不会自动连接终端；需要先让此页连接随包扩展。macOS 请使用 Chrome 或 Edge，Safari 不支持此扩展。</p></body></html>'''


def open_login_browser(url, new=2):
    if sys.platform == "darwin":
        # Safari cannot load the accompanying Chrome/Edge extension.
        for base in (Path("/Applications"), Path.home() / "Applications"):
            for app_name in ("Google Chrome.app", "Microsoft Edge.app"):
                application = base / app_name
                if application.is_dir():
                    result = subprocess.run(["/usr/bin/open", "-a", str(application), url],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return result.returncode == 0
    return webbrowser.open(url, new=new)


class LoginBridge:
    def __init__(self):
        self.token = secrets.token_hex(32)
        self.candidates = queue.Queue(maxsize=1)
        self.accepted = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)
            def log_message(self, *args):
                pass  # Do not log credentials, tokens or URLs.
            def reply(self, status, body, content_type="application/json"):
                origin = self.headers.get("Origin", "")
                self.send_response(status)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
                if re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Gemini-Auth")
                    self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Private-Network", "true")
                    self.send_header("Vary", "Origin")
                raw = body.encode("utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                if self.path != "/gemini-auth":
                    self.reply(404, '{}')
                    return
                self.reply(200, PAGE, "text/html")
            def do_OPTIONS(self):
                self.reply(204, "")
            def do_POST(self):
                expected_host = f"127.0.0.1:{owner.server.server_port}"
                origin = self.headers.get("Origin", "")
                token = self.headers.get("X-Gemini-Auth", "")
                valid_origin = not origin or bool(re.fullmatch(r"chrome-extension://[a-p]{32}", origin))
                if (self.path != "/session" or self.headers.get("Host") != expected_host or not valid_origin
                    or not re.fullmatch(r"[a-f0-9]{64}", token) or not hmac.compare_digest(token, owner.token)):
                    self.reply(403, '{}')
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 131072:
                        raise ValueError()
                    body = json.loads(self.rfile.read(length))
                    from gemini_auth import google_cookies
                    items = body.get("cookies", []) if isinstance(body, dict) else []
                    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                        raise ValueError()
                    cookies = google_cookies(items)
                except (ValueError, TypeError, OSError):
                    self.reply(400, '{}')
                    return
                if cookies.get("__Secure-1PSID") and not owner.accepted:
                    try:
                        owner.candidates.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        owner.candidates.put_nowait(cookies)
                    except queue.Full:
                        pass
                self.reply(200, json.dumps({"accepted": owner.accepted}))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="gemini-login-link")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/gemini-auth#gemini-login={self.token}"

    def start(self):
        self.thread.start()

    def take(self):
        try:
            return self.candidates.get(timeout=1)
        except queue.Empty:
            return None

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


async def recover_login(settings, verify, announce=print, bridge_factory=LoginBridge,
                        opener=open_login_browser, timeout=600):
    from gemini_auth import LoginError, save_session
    official = "https://gemini.google.com/" + (f"u/{settings.account}/" if settings.account else "") + "app"
    bridge = bridge_factory()
    bridge.start()
    announce("官方登录链接（可复制）：" + official)
    announce("本次终端连接链接（可复制到浏览器）：" + bridge.url)
    announce("首次需在 Chrome/Edge 加载随包 browser-extension 文件夹，再刷新连接页。")
    announce("正在打开普通浏览器标签页（macOS 优先 Chrome/Edge）；不会开启远程调试。")
    try:
        try:
            opened = await asyncio.to_thread(opener, bridge.url, new=2)
            if opened is False:
                announce("未能自动打开默认浏览器，请复制上面的终端连接链接手动打开。")
        except Exception:
            announce("未能自动打开默认浏览器，请复制上面的终端连接链接手动打开。")
        deadline = time.monotonic() + timeout
        checked = {}
        while time.monotonic() < deadline:
            cookies = await asyncio.to_thread(bridge.take)
            if not cookies:
                continue
            signature = tuple(sorted(cookies.items()))
            now = time.monotonic()
            if now - checked.get(signature, -100) < 5:
                continue
            checked[signature] = now
            # The ordinary browser owns cookie rotation; do not invalidate its session.
            settings.auto_refresh = False
            if await verify(cookies):
                save_session(settings.home, cookies)
                bridge.accepted = True
                announce("登录已验证并加密保存，可以返回终端聊天。浏览器标签页可自行关闭。")
                await asyncio.sleep(0.2)
                return
        raise LoginError("等待登录连接超时。请确认已加载随包扩展，并在同一浏览器打开本次终端连接链接。")
    finally:
        await asyncio.to_thread(bridge.close)
