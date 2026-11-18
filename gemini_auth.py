"""User-owned browser sign-in with Windows DPAPI or macOS Keychain persistence."""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import hashlib
import sys
import tempfile
import os
from pathlib import Path
import time


class LoginError(Exception):
    pass


def configure_sdk_account(index: str) -> None:
    """Account routing contained in this application; no legacy server package."""
    import importlib
    from gemini_webapi.constants import Endpoint
    if not str(index).isdecimal():
        raise LoginError("账号序号必须是数字。")
    prefix = f"https://gemini.google.com/u/{index}"
    class Endpoints:
        GOOGLE = str(Endpoint.GOOGLE)
        INIT = prefix + "/app"
        GENERATE = prefix + "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
        ROTATE_COOKIES = str(Endpoint.ROTATE_COOKIES)
        UPLOAD = str(Endpoint.UPLOAD)
        BATCH_EXEC = prefix + "/_/BardChatUi/data/batchexecute"
    for module in ("gemini_webapi.client", "gemini_webapi.utils.get_access_token", "gemini_webapi.utils.rotate_1psidts"):
        importlib.import_module(module).Endpoint = Endpoints


def protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise LoginError("此版本的加密登录存储需要 Windows。")
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise LoginError("无法读取此 Windows 用户的登录存储，请重新登录。")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)


def session_path(home: Path) -> Path:
    return home / ".gemcli-session.dat"


def _mac_keyring():
    try:
        from keyring.backends.macOS import Keyring
        return Keyring()
    except Exception as error:
        raise LoginError("无法使用 macOS 钥匙串，请先完成依赖安装并解锁登录钥匙串。") from error


def _keychain_account(home: Path) -> str:
    identifier = hashlib.sha256(str(home.resolve()).encode("utf-8")).hexdigest()[:24]
    return "session-" + identifier


def load_session(home: Path) -> dict[str, str]:
    if sys.platform == "darwin":
        try:
            raw = _mac_keyring().get_password("GeminiLocal", _keychain_account(home))
        except Exception as error:
            raise LoginError("无法读取 macOS 登录钥匙串，请解锁钥匙串后重试。") from error
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    else:
        try:
            data = json.loads(protect(session_path(home).read_bytes(), decrypt=True))
        except (OSError, LoginError, ValueError, TypeError):
            return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def save_session(home: Path, cookies: dict[str, str]) -> None:
    raw = json.dumps(cookies, ensure_ascii=False)
    if sys.platform == "darwin":
        try:
            _mac_keyring().set_password("GeminiLocal", _keychain_account(home), raw)
            return
        except Exception as error:
            raise LoginError("无法保存到 macOS 钥匙串，请解锁钥匙串后重新连接。登录信息未写入明文文件。") from error
    encrypted = protect(raw.encode("utf-8"))
    home.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".gemcli-session-", suffix=".tmp", dir=home)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encrypted)
        os.replace(temporary, session_path(home))
    finally:
        temporary.unlink(missing_ok=True)


def google_cookies(items: list[dict]) -> dict[str, str]:
    result = {}
    now = time.time()
    for cookie in items:
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if domain != "google.com" and not domain.endswith(".google.com"):
            continue
        expiry = cookie.get("expires", -1)
        if isinstance(expiry, (int, float)) and expiry > 0 and expiry < now:
            continue
        if cookie.get("path", "/") == "/" and cookie.get("name") and cookie.get("value"):
            result[str(cookie["name"])] = str(cookie["value"])
    return result


async def recover_login(settings, verify, announce=print):
    from gemini_browser_link import recover_login as recover
    return await recover(settings, verify, announce=announce)
