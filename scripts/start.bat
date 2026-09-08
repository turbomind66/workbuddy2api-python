@echo off
chcp 65001 >nul
REM ============================================
REM workbuddy2api-python 一键启动脚本 (Windows)
REM ============================================

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [i] 未检测到虚拟环境，正在创建...
    python -m venv .venv
    call .venv\Scripts\activate
    echo [i] 正在安装依赖...
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate
)

if not exist "config.json" (
    echo [i] 未检测到 config.json，从模板创建...
    copy config.example.json config.json >nul
    echo [!] 请先编辑 config.json 修改 api_key，然后再运行本脚本。
    pause
    exit /b 1
)

echo [i] 启动服务...
python cli/server.py -config config.json
pause
