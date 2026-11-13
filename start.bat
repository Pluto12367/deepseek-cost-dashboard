@echo off
chcp 65001 >nul
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 (
    echo [!] 未找到 Python,请先安装 Python 3.10+ 后重试
    pause
    exit /b 1
)
echo [1/2] 安装依赖...
python -m pip install -q -r requirements.txt
echo [2/2] 启动面板与代理...
python main.py
if errorlevel 1 pause
