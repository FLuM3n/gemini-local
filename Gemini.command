#!/bin/bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
unset PYTHONHOME PYTHONPATH
export PYTHONUTF8=1
for candidate in python3.12 python3.13 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,14) else 1)' >/dev/null 2>&1; then
    exec "$candidate" -B bootstrap.py "$@"
  fi
done
printf '%s\n' '请先从 python.org 安装 Python 3.12，再重新打开 Gemini.command。'
printf '%s\n' 'https://www.python.org/downloads/macos/'
read -r -p '按回车关闭……' _gemini_close
exit 1
