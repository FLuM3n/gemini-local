"""Loopback-only Chat Completions bridge for the bundled Gemini terminal."""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import select
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

from gemini_terminal import TerminalError, auth_failure, exact_model, read_env, safe_error

MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


class CitationCodec:
    """Keep application citations outside Gemini Web's native [cite: ...] syntax."""
    prefix = "[AIDEA_SOURCE:"
    ids_pattern = r"[A-Za-z0-9_-]+(?:\s*[,，;；]\s*[A-Za-z0-9_-]+)*"
    marker = re.compile(r"\[\[cite:(" + ids_pattern + r")\]\]")

    def __init__(self, prompt):
        self.allowed = set()
        self.buffer = ""

        def encode(match):
            ids = re.split(r"\s*[,，;；]\s*", match[1])
            self.allowed.update(ids)
            return " ".join(self.prefix + identifier + "]" for identifier in dict.fromkeys(ids))

        self.prompt = self.marker.sub(encode, prompt)
        if self.allowed:
            self.prompt = (
                "Application citation transport: [AIDEA_SOURCE:ID] labels below identify supplied document evidence, "
                "not Gemini native citations. Use the exact supplied labels when citing. Do not replace them with empty brackets "
                "or invent IDs. Display web links as normal Markdown links, without enclosing the entire link in backticks, "
                "unless the user asks for literal code syntax.\n\n" + self.prompt
            )

    def feed(self, text, *, final=False):
        if not self.allowed:
            return text
        self.buffer += text
        result = []
        while self.buffer:
            start = self.buffer.find(self.prefix)
            if start < 0:
                # An SSE chunk may end halfway through the opening token.
                keep = 0
                if not final:
                    for length in range(1, min(len(self.prefix), len(self.buffer) + 1)):
                        if self.buffer.endswith(self.prefix[:length]):
                            keep = length
                result.append(self.buffer[:-keep] if keep else self.buffer)
                self.buffer = self.buffer[-keep:] if keep else ""
                break
            if start:
                result.append(self.buffer[:start])
                self.buffer = self.buffer[start:]
            end = self.buffer.find("]", len(self.prefix))
            if end < 0:
                if not final and len(self.buffer) <= 512:
                    break
                result.append(self.buffer[0])
                self.buffer = self.buffer[1:]
                continue
            marker = self.buffer[:end + 1]
            payload = self.buffer[len(self.prefix):end].strip()
            ids = re.split(r"\s*[,，;；]\s*", payload)
            if re.fullmatch(self.ids_pattern, payload) and all(identifier in self.allowed for identifier in ids):
                result.append(" ".join("[[cite:" + identifier + "]]" for identifier in dict.fromkeys(ids)))
            else:
                # Never assign an unknown label to a supplied source.
                result.append(marker)
            self.buffer = self.buffer[end + 1:]
        return "".join(result)


class RequestError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def decode_request(body: object) -> tuple[str, list[tuple[bytes, str]]]:
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise RequestError("messages 必须是消息数组。")
    if not body["messages"]:
        raise RequestError("messages 不能为空。")
    if body.get("tools"):
        raise RequestError("此服务目前提供文字和图片问答，未实现工具调用。")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise RequestError("stream 必须为布尔值。")
    if body.get("n", 1) != 1:
        raise RequestError("每次请求仅支持一条回答。")
    turns = []
    images = []
    image_bytes = 0
    for message in body["messages"]:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant"}:
            raise RequestError("消息角色仅支持 system、developer、user、assistant。")
        content = message.get("content")
        if isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = content
        else:
            raise RequestError("消息 content 必须是文字或内容数组。")
        text = []
        for part in parts:
            if not isinstance(part, dict):
                raise RequestError("消息内容格式错误。")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            elif part.get("type") == "image_url":
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                if not isinstance(url, str) or not url.startswith("data:"):
                    raise RequestError("图片请使用插件上传的 base64 data URL。")
                header, separator, encoded = url.partition(",")
                mime = header[5:].removesuffix(";base64").lower()
                if not separator or not header.endswith(";base64") or mime not in IMAGE_SUFFIXES:
                    raise RequestError("图片仅支持 PNG、JPEG、WebP、GIF 的 base64 data URL。")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise RequestError("图片 base64 内容无效。") from None
                image_bytes += len(data)
                if not data or image_bytes > MAX_IMAGE_BYTES:
                    raise RequestError("图片为空或合计超过16MB。", 413)
                images.append((data, IMAGE_SUFFIXES[mime]))
            else:
                raise RequestError("消息包含未支持的内容类型。")
        label = {"system": "System", "developer": "Developer", "user": "User", "assistant": "Assistant"}[message["role"]]
        turns.append(f"{label}: " + "\n".join(text))
    if not any(turn.partition(": ")[2].strip() for turn in turns) and not images:
        raise RequestError("请求没有文字或图片。")
    return "\n\n".join(turns) + "\n\nAssistant:", images


class GeminiBridge:
    def __init__(self, session, worker, api_key: str = "", extended: bool | None = None):
        self.session = session
        self.worker = worker
        self.api_key = api_key
        self.lock = getattr(session, "request_lock", None) or asyncio.Lock()
        self.extended = session.extended if extended is None else extended
        self.tasks = set()
        self.stopping = False
        self.slots = threading.BoundedSemaphore(4)

    def error_text(self, error: Exception) -> str:
        text = safe_error(error, self.session.settings)
        return text.replace(self.api_key, "<已隐藏>") if self.api_key else text

    def model(self, name: object):
        if name is None:
            if self.session.model is None:
                raise RequestError("请指定账号当前可用的模型。", 404)
            return self.session.model
        if not isinstance(name, str) or not name.strip():
            raise RequestError("model 必须是模型名称。")
        try:
            return exact_model(self.session.models, name)
        except TerminalError as error:
            raise RequestError(str(error), 404) from None

    def models(self):
        return [{"id": model.model_name or model.model_id, "object": "model", "owned_by": "google-gemini-web", "display_name": model.display_name} for model in self.session.models]

    def submit(self, prompt, images, model, extended, events):
        if self.stopping:
            raise RequestError("本地服务正在退出。", 503)
        return asyncio.run_coroutine_threadsafe(self.generate(prompt, images, model, extended, events), self.worker.loop)

    async def close(self):
        self.stopping = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def generate(self, prompt, images, model, extended, events):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with self.lock:
                self.session.check_account()
                # A fresh SDK request uses exactly the caller's history. It does
                # not append Zotero prompts to the interactive terminal chat.
                with tempfile.TemporaryDirectory(prefix="gemini-zotero-") as directory:
                    files = []
                    for index, (data, suffix) in enumerate(images):
                        file = Path(directory) / f"page-{index + 1}{suffix}"
                        file.write_bytes(data)
                        files.append(file)
                    codec = CitationCodec(prompt)
                    stream = self.session.client.generate_content_stream(
                        codec.prompt, files=files or None, model=model,
                        temporary=True, extended_thinking=extended,
                    )
                    emitted = False
                    received_delta = False
                    last = None
                    try:
                        async for output in stream:
                            last = output
                            delta = getattr(output, "text_delta", "") or ""
                            if delta:
                                received_delta = True
                                delta = codec.feed(delta)
                                if delta:
                                    emitted = True
                                    events.put(("text", delta))
                        if not received_delta and last is not None and getattr(last, "text", ""):
                            delta = codec.feed(last.text)
                            if delta:
                                emitted = True
                                events.put(("text", delta))
                        tail = codec.feed("", final=True)
                        if tail:
                            emitted = True
                            events.put(("text", tail))
                        if not emitted:
                            raise TerminalError("Gemini 返回空内容，请重试或切换模型。")
                    finally:
                        await stream.aclose()
            events.put(("done", None))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            status = 401 if auth_failure(error) else 502
            message = "Gemini 登录已失效，请重启本地服务并完成浏览器登录。" if status == 401 else self.error_text(error)
            events.put(("error", (status, message)))
        finally:
            self.tasks.discard(task)


class LocalAPIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, bridge):
        self.bridge = bridge
        super().__init__(address, APIHandler)


class APIHandler(BaseHTTPRequestHandler):
    # Closing the HTTP/1.0 stream delimits SSE without custom chunked encoding.
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_args):
        pass

    def json_response(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, message):
        self.json_response(status, {"error": {"message": message, "type": "gemini_local_error"}})

    def authorize(self):
        key = self.server.bridge.api_key
        token = self.headers.get("Authorization", "")
        if key and not hmac.compare_digest(token.encode(), ("Bearer " + key).encode()):
            raise RequestError("本地服务访问 Key 不正确。", 401)
        host = self.headers.get("Host", "").lower()
        if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            raise RequestError("仅接受本机服务地址。", 403)
        origin = self.headers.get("Origin", "")
        if origin and origin not in {"chrome://zotero", "resource://zotero"} and not (origin == "null" and key):
            raise RequestError("请通过 Zotero 或本机客户端调用此接口。", 403)

    def do_GET(self):
        try:
            self.authorize()
            path = urlsplit(self.path).path.rstrip("/")
            if path in {"", "/v1", "/health"}:
                self.json_response(200, {"status": "ok", "service": "gemini-terminal-zotero", "model": getattr(self.server.bridge.session.model, "model_name", None), "endpoints": ["/v1/models", "/v1/chat/completions"]})
            elif path == "/v1/models":
                self.json_response(200, {"object": "list", "data": self.server.bridge.models()})
            else:
                self.error(404, "接口不存在。")
        except RequestError as error:
            self.error(error.status, str(error))

    def do_POST(self):
        acquired = False
        future = None
        try:
            self.authorize()
            if urlsplit(self.path).path.rstrip("/") != "/v1/chat/completions":
                raise RequestError("请使用 /v1/chat/completions。", 404)
            if self.headers.get("Transfer-Encoding"):
                raise RequestError("请使用带 Content-Length 的 JSON 请求。")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise RequestError("Content-Length 无效。") from None
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise RequestError("请求为空或超过32MB。", 413)
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise RequestError("请求必须是有效 JSON。") from None
            prompt, images = decode_request(body)
            bridge = self.server.bridge
            model = bridge.model(body.get("model"))
            extended = body.get("extended_thinking", bridge.extended)
            if not isinstance(extended, bool):
                raise RequestError("extended_thinking 必须为布尔值。")
            acquired = bridge.slots.acquire(blocking=False)
            if not acquired:
                raise RequestError("本地服务正在处理其他请求，请稍后再试。", 429)
            events = queue.SimpleQueue()
            future = bridge.submit(prompt, images, model, extended, events)
            self.deliver(events, future, model.model_name, body.get("stream", False))
        except RequestError as error:
            self.error(error.status, str(error))
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass
        except Exception:
            self.error(500, "本地服务处理失败，请重启服务后重试。")
        finally:
            if future is not None and not future.done():
                future.cancel()
            if acquired:
                self.server.bridge.slots.release()

    def disconnected(self):
        readable, _, _ = select.select([self.connection], [], [], 0)
        return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)

    def deliver(self, events, future, model, stream):
        request_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        text = []
        started = False
        last_write = time.monotonic()

        def chunk(delta, finish=None):
            return {"id": request_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        def send_event(payload):
            self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        while True:
            try:
                kind, value = events.get(timeout=0.5)
            except queue.Empty:
                if self.disconnected():
                    return
                if started and time.monotonic() - last_write >= 5:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                if future.done() and future.cancelled():
                    return
                continue
            if kind == "error":
                status, message = value
                if not started:
                    self.error(status, message)
                else:
                    # Older Zotero clients ignore SSE error objects. Include an
                    # explicit incomplete-answer notice so partial text is clear.
                    send_event(chunk({"content": "\n\n[Gemini 服务错误，本轮回答未完成：" + message + "]"}))
                    send_event({"error": {"message": message, "type": "gemini_local_error"}})
                return
            if kind == "text":
                text.append(value)
                if stream:
                    if not started:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        started = True
                        send_event(chunk({"role": "assistant"}))
                    send_event(chunk({"content": value}))
                    last_write = time.monotonic()
            elif kind == "done":
                if stream:
                    send_event(chunk({}, "stop"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    self.json_response(200, {"id": request_id, "object": "chat.completion", "created": created, "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(text)}, "finish_reason": "stop"}]})
                return


def create_server(session, worker, port=8100, *, extended=None):
    values = dict(os.environ)
    values.update(read_env(session.settings.home / ".env.gemini"))
    bridge = GeminiBridge(session, worker, values.get("GEMINI_LOCAL_API_KEY", ""), extended=extended)
    try:
        return LocalAPIServer(("127.0.0.1", port), bridge)
    except OSError as error:
        if error.errno in {48, 98, 10048} or getattr(error, "winerror", None) == 10048:
            raise TerminalError(f"端口 {port} 已被占用。请关闭旧的 Gemini 服务窗口后重开，或使用 --port 8101 并修改插件地址。") from None
        raise


def announce(server):
    bridge = server.bridge
    print(f"Zotero 本地服务已就绪：http://127.0.0.1:{server.server_port}/v1/", flush=True)
    print("可在 AIdea 自定义 API 中选择 Gemini 网页版（本地），然后获取模型列表。", flush=True)
    print("访问 Key：" + ("已启用，请在插件中填写本机配置的 Key。" if bridge.api_key else "未设置，插件 Key 栏可留空。"), flush=True)
    print("Zotero 思考模式：" + ("Extended" if bridge.extended else "Standard"), flush=True)


@contextmanager
def background_service(session, worker, port=8100, *, extended=False):
    with create_server(session, worker, port, extended=extended) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True, name="gemini-zotero-api")
        thread.start()
        try:
            announce(server)
            print("可直接在此窗口聊天；Zotero 同时可用。输入 /exit 一起退出。", flush=True)
            yield server
        finally:
            server.bridge.stopping = True
            server.shutdown()
            thread.join(timeout=3)
            worker.run(server.bridge.close())


def serve(session, worker, port=8100):
    with create_server(session, worker, port) as server:
        announce(server)
        print("保持此窗口运行。按 Ctrl+C 停止服务。", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\n本地服务已停止。", flush=True)
        finally:
            worker.run(server.bridge.close())
