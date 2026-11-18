"""Small, browserless Gemini Web chat terminal for the existing gemcli environment.

Only the installed gemini-webapi SDK and Python's standard library are used.
The SDK's metadata hooks are isolated in NativeChat; verified against 2.1.1.
Interactive chat and an optional loopback API mode share browser sign-in.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import copy
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any, Callable


# Ship the exact SDK snapshot used by the standalone terminal on every platform.
_vendor = Path(__file__).resolve().parent / "vendor"
if (_vendor / "gemini_webapi").is_dir() and str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))


def default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GeminiTerminal"
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GeminiTerminal"


class TerminalError(Exception):
    pass


class UserCancelled(Exception):
    pass


class LoginRequired(TerminalError):
    pass


def auth_failure(exc: BaseException) -> bool:
    return isinstance(exc, LoginRequired) or type(exc).__name__ == "AuthError"


def read_env(path: Path) -> dict[str, str]:
    """Read literal dotenv values; never execute shell expressions or expand secrets."""
    if not path.is_file():
        return {}
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


@dataclass
class Settings:
    home: Path
    cookies: dict[str, str] = field(repr=False)
    proxy: str | None = field(default=None, repr=False)
    account: str = ""
    auto_refresh: bool = False
    cache_dir: Path | None = None
    timeout: float = 120
    quota_timeout: float = 6

    @classmethod
    def load(cls, home: Path, cache_dir: Path | None = None) -> Settings:
        home = home.resolve()
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        if sys.platform == "darwin":
            home.chmod(0o700)
        values = dict(os.environ)
        values.update(read_env(home / ".env.gemini"))
        psid = values.get("GEMINI_1PSID", "")
        cookies = {"__Secure-1PSID": psid} if psid else {}
        if psidts := values.get("GEMINI_1PSIDTS"):
            cookies["__Secure-1PSIDTS"] = psidts
        from gemini_auth import load_session
        cookies = load_session(home) or cookies
        account = values.get("GEMINI_AUTHUSER", "").strip()
        if account and not account.isdecimal():
            raise TerminalError("GEMINI_AUTHUSER 必须是账号序号。")
        refresh = values.get("GEMINI_AUTO_REFRESH", "0").strip().lower()
        if refresh not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise TerminalError("GEMINI_AUTO_REFRESH 应为 1 或 0。")
        return cls(
            home=home,
            cookies=cookies,
            proxy=values.get("GEMINI_PROXY") or None,
            account=account,
            auto_refresh=refresh in {"1", "true", "yes", "on"},
            cache_dir=(cache_dir or home / ".gemcli-cache").resolve(),
        )


def safe_error(exc: BaseException, settings: Settings | None = None) -> str:
    text = str(exc) or type(exc).__name__
    if settings:
        for secret in (*settings.cookies.values(), settings.proxy):
            if secret:
                text = text.replace(secret, "<已隐藏>")
    text = re.sub(r"\.cached_cookies_[^\s/\\\"']+", "<登录缓存>", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1<已隐藏>@", text)
    return text[:600]


def remaining_percent(metric: Any) -> int | None:
    """SDK usage_percentage is USED percent; remaining_credits has no denominator."""
    if not isinstance(metric, dict):
        return None
    used = metric.get("usage_percentage")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        level = metric.get("usage_level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            return None
        used = level * 100
    if not math.isfinite(used) or not 0 <= used <= 100:
        return None
    return 100 - round(used)


def quota_line(info: Any, *, fresh: bool = True) -> str:
    info = info if isinstance(info, dict) else {}
    parts = []
    for key, window in (("current_5h", "5h"), ("weekly", "7d")):
        remaining = remaining_percent(info.get(key)) if fresh else None
        parts.append(f"{remaining}%(" + window + ")" if remaining is not None else f"--({window})")
    suffix = "" if fresh else "（本次读取失败）"
    return "额度剩余：" + ", ".join(parts) + suffix


def available_models(client: Any) -> list[Any]:
    return [m for m in client.list_models() or [] if getattr(m, "is_available", False)]


def exact_model(models: list[Any], name: str) -> Any:
    """No substring matching, invented version aliases, or silent tier substitution."""
    target = name.strip().casefold()
    if target.startswith("models/"):
        target = target[7:]
    primary = [m for m in models if target in {
        str(m.model_id).casefold(), str(m.model_name).casefold(), str(m.display_name).casefold()
    }]
    matches = primary or [m for m in models if target in {
        str(alias).casefold() for alias in getattr(m, "aliases", [])
    }]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise TerminalError("名称对应多个模型，请用 /models 中的序号或完整 ID。")
    raise TerminalError("账号当前没有这个模型，请用 /models 查看可选项。")


def choose_default(models: list[Any]) -> Any:
    if not models:
        raise TerminalError("账号未返回可用模型，请稍后重新启动以重新获取。")
    for name in ("gemini-flash", "gemini-auto"):
        try:
            return exact_model(models, name)
        except TerminalError:
            pass
    return models[0]


def make_sdk_client(settings: Settings) -> Any:
    # Set the SDK cache before any SDK import; never read a browser cookie store ourselves.
    if settings.cache_dir:
        settings.cache_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if sys.platform == "darwin":
            settings.cache_dir.chmod(0o700)
        os.environ["GEMINI_COOKIE_PATH"] = str(settings.cache_dir)
    try:
        from gemini_webapi import GeminiClient
        from gemini_webapi.utils.logger import logger
    except ImportError as exc:
        raise TerminalError("运行文件不完整，请完整解压独立版 ZIP 后重新启动。") from exc
    # SDK logs may include cookie-bearing cache filenames. Use only our sanitized errors.
    logger.remove()
    # The SDK otherwise scans/decrypts every browser in a thread pool, even when
    # explicit cookies were supplied. Our dedicated sign-in replaces that path.
    import importlib
    token_module = importlib.import_module("gemini_webapi.utils.get_access_token")
    token_module.load_browser_cookies = lambda **kwargs: {}
    if settings.account:
        from gemini_auth import configure_sdk_account
        configure_sdk_account(settings.account)
    client = GeminiClient(
        settings.cookies.get("__Secure-1PSID"),
        settings.cookies.get("__Secure-1PSIDTS"),
        proxy=settings.proxy,
    )
    for name, value in settings.cookies.items():
        client.cookies.set(name, value, domain=".google.com", path="/", secure=True)
    return client


class NativeChat:
    def __init__(self, settings: Settings, factory: Callable = make_sdk_client):
        self.settings = settings
        self.factory = factory
        self.client: Any = None
        self.chat: Any = None
        self.models: list[Any] = []
        self.model: Any = None
        self.extended = True
        self.allow_login = factory is make_sdk_client
        self.request_lock = asyncio.Lock()

    async def serialized(self, operation, *args):
        # One SDK client serves the independent terminal and Zotero conversations.
        async with self.request_lock:
            return await operation(*args)

    async def connect(self, requested: str | None = None) -> None:
        try:
            if not self.settings.cookies.get("__Secure-1PSID"):
                raise LoginRequired("尚未建立登录会话。")
            await self._connect_once(requested)
        except Exception as exc:
            if not self.allow_login or not auth_failure(exc):
                raise
            from gemini_auth import recover_login
            async def verify(cookies):
                self.settings.cookies = cookies
                # Delete only this app's rejected SDK cache for this exact session.
                if self.factory is make_sdk_client and self.settings.cache_dir:
                    from gemini_webapi.utils.rotate_1psidts import _get_cookies_cache_path
                    probe = make_sdk_client(self.settings)
                    cache = _get_cookies_cache_path(probe.cookies)
                    if cache and cache.parent.resolve() == self.settings.cache_dir.resolve():
                        cache.unlink(missing_ok=True)
                try:
                    await self._connect_once(requested)
                    return True
                except Exception as error:
                    if auth_failure(error):
                        return False
                    raise
            await recover_login(self.settings, verify, announce=lambda message: print(message, flush=True))

    async def _connect_once(self, requested: str | None = None) -> None:
        self.client = self.factory(self.settings)
        try:
            await asyncio.wait_for(self.client.init(
                timeout=self.settings.timeout, auto_close=False,
                auto_refresh=self.settings.auto_refresh, refresh_interval=540, verbose=False,
            ), timeout=self.settings.timeout)
            self.check_account()
            self.models = available_models(self.client)
            self.model = exact_model(self.models, requested) if requested else choose_default(self.models)
            self.chat = self.client.start_chat(model=self.model)
        except BaseException:
            await self.close()
            raise

    def check_account(self) -> None:
        status = getattr(self.client, "account_status", None)
        if getattr(status, "name", "") == "UNAUTHENTICATED":
            raise LoginRequired("网页会话需要自动恢复登录。")
        if getattr(status, "name", "") != "AVAILABLE":
            raise TerminalError("Google 账号暂时不可用（" + getattr(status, "name", "未知状态") +
                                "），请确认账号访问权限后重新启动。")

    def select_model(self, name: str) -> None:
        # Numeric selection is a UI convenience; canonical IDs are used for requests.
        if name.isdecimal() and 1 <= int(name) <= len(self.models):
            model = self.models[int(name) - 1]
        else:
            model = exact_model(self.models, name)
        self.model = model
        self.chat.model = model

    async def refresh_models(self) -> None:
        hook = getattr(self.client, "_fetch_user_status", None)
        if not callable(hook) or not hasattr(self.client, "_model_registry"):
            raise TerminalError("当前 SDK 不支持独立刷新，请退出后重新启动。")
        previous = self.client._model_registry
        self.client._model_registry = {}
        try:
            await asyncio.wait_for(hook(), timeout=30)
            self.check_account()
            models = available_models(self.client)
            if not models:
                raise TerminalError("刷新未返回模型列表；保留上次列表。")
        except BaseException:
            self.client._model_registry = previous
            raise
        self.models = models
        old_id = getattr(self.model, "model_id", None)
        current = next((m for m in models if m.model_id == old_id), None)
        self.model = current
        if current is not None:
            self.chat.model = current
        else:
            raise TerminalError("模型列表已更新，原模型已不在列表中；请用 /model 重新选择。")

    def clear(self) -> None:
        self.chat = self.client.start_chat(model=self.model)

    async def refresh_quota(self) -> tuple[dict, bool]:
        hook = getattr(self.client, "_fetch_usage_info", None)
        if not callable(hook):
            return {}, False
        try:
            # Clear first so a malformed/no-op response cannot appear freshly fetched.
            self.client._usage_info = {}
            await asyncio.wait_for(hook(), timeout=self.settings.quota_timeout)
            info = self.client.usage_info
            if not isinstance(info, dict) or not info:
                return {}, False
            return copy.deepcopy(info), True
        except Exception:
            return {}, False

    async def send(self, prompt: str, emit: Callable[[str], None]) -> None:
        metadata = copy.deepcopy(self.chat.metadata)
        requested = getattr(self.model, "model_id", None)
        emitted = False
        def forward(piece):
            nonlocal emitted
            emitted = True
            emit(piece)
        try:
            await self._send_once(prompt, forward)
        except Exception as error:
            if not self.allow_login or not auth_failure(error) or emitted:
                raise
            await self.close()
            # Validation must go through connect's bounded recovery path.
            await self.connect(requested)
            self.chat.metadata = metadata
            await self._send_once(prompt, emit)

    async def _send_once(self, prompt: str, emit: Callable[[str], None]) -> None:
        if self.model is None:
            raise TerminalError("请先用 /model 选择当前可用模型。")
        self.check_account()
        before = copy.deepcopy(self.chat.metadata)
        emitted = False
        last = None
        stream = self.chat.send_message_stream(
            prompt, temporary=True, extended_thinking=self.extended,
        )
        try:
            async for output in stream:
                last = output
                piece = getattr(output, "text_delta", None) or ""
                if piece:
                    emitted = True
                    emit(piece)
            if not emitted and last is not None and (text := getattr(last, "text", "")):
                emit(text)
                emitted = True
            if not emitted:
                raise TerminalError("没有收到文本回复；可用 /clear 新开对话后重试。")
        except BaseException:
            if emitted and last is not None:
                last.metadata = copy.deepcopy(self.chat.metadata)
                self.chat.last_output = last
            else:
                self.chat.metadata = before
            raise
        finally:
            await stream.aclose()

    async def close(self) -> None:
        if self.client is not None:
            try:
                await asyncio.wait_for(self.client.close(), timeout=8)
            finally:
                self.client = None


class Worker:
    """One SDK event loop, alive during input so cookie refresh does not stall."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="gemcli-network")
        self.thread.start()
        self.ready.wait()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def run(self, coro, events: queue.SimpleQueue | None = None):
        finished = threading.Event()
        future: concurrent.futures.Future = concurrent.futures.Future()
        task_box: dict[str, asyncio.Task] = {}

        def complete(task: asyncio.Task) -> None:
            try:
                if task.cancelled():
                    future.cancel()
                elif (error := task.exception()) is not None:
                    future.set_exception(error)
                else:
                    future.set_result(task.result())
            finally:
                finished.set()

        def launch() -> None:
            # The task's done callback also fires if Ctrl+C cancels before its first step.
            task = self.loop.create_task(coro)
            task_box["task"] = task
            task.add_done_callback(complete)

        self.loop.call_soon_threadsafe(launch)

        def drain() -> None:
            if events is not None:
                while True:
                    try:
                        piece = events.get_nowait()
                    except queue.Empty:
                        break
                    sys.stdout.write(piece)
                    sys.stdout.flush()

        try:
            while not future.done():
                drain()
                time.sleep(0.05)
            drain()
            return future.result()
        except KeyboardInterrupt:
            def cancel() -> None:
                if task := task_box.get("task"):
                    task.cancel()
            self.loop.call_soon_threadsafe(cancel)
            if not finished.wait(timeout=35):
                raise TerminalError("取消尚未完成，请退出终端后重开。") from None
            drain()
            raise UserCancelled from None

    def close(self) -> None:
        async def shutdown():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=2)
            await self.loop.shutdown_asyncgens()
        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            future.result(timeout=4)
        except (Exception, KeyboardInterrupt):
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=4)


HELP = "/models 模型列表 | /model 序号或名称 | /refresh 刷新模型 | /quota 额度\n" \
       "/thinking extended|standard | /clear 新对话 | /exit 退出\n" \
       "Ctrl+C：输入时清空当前输入；回复时中断接收。"


def print_models(session: NativeChat) -> None:
    for number, model in enumerate(session.models, 1):
        mark = "*" if session.model is not None and model.model_id == session.model.model_id else " "
        print(f"{mark} {number}. {model.display_name} | {model.model_name} | {model.model_id}")


def print_selection(session: NativeChat) -> None:
    model = session.model
    thinking = "Extended（网页扩展思考）" if session.extended else "Standard"
    if model is None:
        print("尚未选择可用模型。")
    else:
        print(f"模型：{model.display_name} [{model.model_name}] | 思考：{thinking}")


def show_quota(worker: Worker, session: NativeChat) -> None:
    try:
        info, fresh = worker.run(session.serialized(session.refresh_quota))
        print(quota_line(info, fresh=fresh))
    except UserCancelled:
        print(quota_line({}, fresh=False))


def interactive(worker: Worker, session: NativeChat) -> None:
    print_selection(session)
    print(quota_line(session.client.usage_info))
    print(HELP, "\n")
    while True:
        try:
            prompt = input("You> ").strip()
        except EOFError:
            return
        except KeyboardInterrupt:
            print("\n输入已取消。\n")
            continue
        if not prompt:
            continue
        if prompt.startswith("/"):
            command, _, argument = prompt.partition(" ")
            command, argument = command.lower(), argument.strip()
            try:
                if command in {"/exit", "/quit"}:
                    return
                if command == "/help":
                    print(HELP)
                elif command == "/models":
                    print_models(session)
                elif command == "/model":
                    if argument:
                        session.select_model(argument)
                    print_selection(session)
                elif command == "/refresh":
                    worker.run(session.serialized(session.refresh_models))
                    print_models(session)
                elif command == "/quota":
                    show_quota(worker, session)
                elif command == "/clear":
                    session.clear()
                    print("已新开对话。")
                elif command == "/thinking":
                    if argument not in {"", "extended", "standard"}:
                        raise TerminalError("用法：/thinking extended 或 /thinking standard")
                    if argument:
                        session.extended = argument == "extended"
                    print_selection(session)
                else:
                    print("未知命令，输入 /help 查看帮助。")
            except UserCancelled:
                print("已取消。")
            except Exception as exc:
                print("[错误]", safe_error(exc, session.settings))
            print()
            continue
        events: queue.SimpleQueue = queue.SimpleQueue()
        print("Gemini> ", end="", flush=True)
        try:
            worker.run(session.serialized(session.send, prompt, events.put), events)
        except UserCancelled:
            print("\n[已中断接收，可继续提问。]")
        except Exception as exc:
            print("\n[错误]", safe_error(exc, session.settings))
        else:
            print()
        show_quota(worker, session)
        print()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Gemini 独立轻量终端")
    parser.add_argument("model_name", nargs="?", help="可选的启动模型名称")
    parser.add_argument("--model", help="启动模型，必须属于账号实时列表")
    parser.add_argument("--home", type=Path, default=default_home())
    parser.add_argument("--cache-dir", type=Path, help="登录缓存目录")
    parser.add_argument("--thinking", choices=("extended", "standard"), default="extended")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="只读取模型和额度，不发送聊天请求")
    modes.add_argument("--serve", action="store_true", help="运行供 Zotero 使用的本地 API 服务")
    modes.add_argument("--terminal-only", action="store_true", help="只运行终端聊天，不开启 Zotero 服务")
    parser.add_argument("--api-thinking", choices=("extended", "standard"), default="standard", help="合并模式下 Zotero 的思考模式，默认 standard")
    parser.add_argument("--port", type=int, default=8100, help="本地 API 端口，默认8100")
    parser.add_argument("--no-login", action="store_true", help="禁止弹出登录窗口，仅用于诊断")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在1到65535之间。")
    settings = None
    worker = None
    session = None
    try:
        cache_dir = args.cache_dir
        if args.serve and cache_dir is None:
            cache_dir = args.home / ".gemini-api-cache"
        settings = Settings.load(args.home, cache_dir)
        session = NativeChat(settings)
        session.allow_login = not args.no_login
        session.extended = args.thinking == "extended"
        worker = Worker()
        print("正在连接 Gemini 网页端并获取模型列表……", flush=True)
        worker.run(session.connect(args.model or args.model_name))
        if args.serve:
            from gemini_api import serve
            serve(session, worker, args.port)
        elif args.check:
            print_selection(session)
            print_models(session)
            print(quota_line(session.client.usage_info))
        else:
            if args.terminal_only:
                interactive(worker, session)
            else:
                from gemini_api import background_service
                with background_service(session, worker, args.port, extended=args.api_thinking == "extended"):
                    interactive(worker, session)
        return 0
    except UserCancelled:
        print("\n已取消连接。")
        return 130
    except Exception as exc:
        print("[错误]", safe_error(exc, settings), file=sys.stderr)
        return 1
    finally:
        if worker:
            if session:
                try:
                    worker.run(session.close())
                except (Exception, KeyboardInterrupt):
                    pass
            worker.close()


if __name__ == "__main__":
    # The optional API imports this module; keep shared exception identities.
    sys.modules.setdefault("gemini_terminal", sys.modules[__name__])
    raise SystemExit(main())
