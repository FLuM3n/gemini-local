"""Prepare an isolated runtime when a bundled Windows Python is not present."""
from pathlib import Path
import hashlib
import os
import subprocess
import sys
import venv


def main(argv=None):
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        print("请安装 Python 3.12（支持 Python 3.11–3.13），再启动 Gemini。", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = root / "requirements.txt"
    expected = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = environment / ".gemini-requirements.sha256"
    try:
        if not interpreter.is_file():
            print("首次启动：正在准备独立 Python 环境……", flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        if not stamp.is_file() or stamp.read_text(encoding="ascii").strip() != expected:
            print("正在安装 Gemini 依赖，首次启动需要网络……", flush=True)
            subprocess.run([str(interpreter), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)], check=True)
            stamp.write_text(expected + "\n", encoding="ascii")
        args = list(sys.argv[1:] if argv is None else argv)
        os.execv(str(interpreter), [str(interpreter), "-B", str(root / "gemini_terminal.py"), *args])
    except (OSError, subprocess.CalledProcessError) as error:
        print("启动准备失败，请检查 Python 与网络后重试：" + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
