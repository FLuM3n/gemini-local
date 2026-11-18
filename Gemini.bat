@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Gemini Local - Terminal + Zotero
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
if exist "%~dp0runtime\python.exe" goto portable
where py >nul 2>nul
if errorlevel 1 goto pythonpath
for %%V in (3.12 3.13 3.11) do (
    py -%%V -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "GEMINI_PY_LAUNCH=py -%%V"
        goto systempython
    )
)
:pythonpath
where python >nul 2>nul
if errorlevel 1 goto missing
set "GEMINI_PY_LAUNCH=python"
:systempython
%GEMINI_PY_LAUNCH% -B "%~dp0bootstrap.py" %*
goto finished
:portable
"%~dp0runtime\python.exe" -B "%~dp0gemini_terminal.py" %*
:finished
set "GEMINI_EXIT=%errorlevel%"
if not "%GEMINI_EXIT%"=="0" pause
exit /b %GEMINI_EXIT%
:missing
echo Please install Python 3.12 or download the Windows portable release.
pause
exit /b 1
