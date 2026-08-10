@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 萤核-人脸视频分类 一键启动

REM ============================================================
REM  一键启动：萤核本体 + Python 外挂服务(端口5002)
REM  用法：双击运行。首次运行会自动安装依赖并集成前端。
REM ============================================================

REM ---------- 路径配置（按需修改）----------
REM 萤核本体根目录（含 package.json 的目录）
set "FIREFLY_ROOT=..\..\firefly-ai-folder-cn"
REM Python 外挂服务目录（本脚本所在 scripts 的上级 face_service）
set "FACE_SERVICE_ROOT=..\face_service"
REM 萤核前端启动命令（开发模式；若用打包版改为对应可执行文件路径）
set "FIREFLY_CMD=npm run dev"
REM ----------------------------------------

echo ============================================================
echo  萤核-人脸视频分类 一键启动
echo ============================================================

REM 1) 检查 Python
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 python，请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version') do set "PY_VER=%%v"
echo [1/5] Python 检测：!PY_VER!

REM 2) 检查 .env（不存在则从示例复制）
pushd "%FACE_SERVICE_ROOT%" >nul 2>nul
if not exist ".env" (
    if exist ".env.example" (
        copy /Y ".env.example" ".env" >nul
        echo [2/5] 已从 .env.example 生成 .env，请按需修改 FIREFLY_DB_PATH 与模型路径。
    ) else (
        echo [警告] 未找到 .env.example，将使用默认配置。
    )
) else (
    echo [2/5] .env 已存在。
)

REM 3) 安装 Python 依赖（首次或缺失时）
echo [3/5] 检查 Python 依赖...
python -c "import fastapi,insightface,faiss,cv2" >nul 2>nul
if errorlevel 1 (
    echo       缺失依赖，开始安装（requirements.txt）...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请手动执行：pip install -r requirements.txt
        pause
        popd
        exit /b 1
    )
) else (
    echo       依赖已就绪。
)
popd >nul

REM 4) 启动 Python 外挂服务（端口5002），新窗口
echo [4/5] 启动 Python 外挂服务（端口 5002）...
start "face_service :5002" cmd /k "cd /d %FACE_SERVICE_ROOT% && python run.py"

REM 等待服务就绪
echo       等待服务就绪...
set /a tries=0
:wait_svc
timeout /t 1 >nul
python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:5002/api/health',timeout=1)" >nul 2>nul
if errorlevel 1 (
    set /a tries+=1
    if !tries! lss 20 (
        goto wait_svc
    ) else (
        echo [警告] face_service 30 秒内未就绪，仍尝试启动萤核。请检查 face_service 窗口日志。
    )
) else (
    echo       face_service 已就绪。
)

REM 5) 启动萤核本体
if not exist "%FIREFLY_ROOT%\package.json" (
    echo [警告] 未在 %FIREFLY_ROOT% 找到萤核 package.json，跳过萤核启动。
    echo        请修改本脚本顶部的 FIREFLY_ROOT 变量指向萤核目录。
    pause
    exit /b 0
)
echo [5/5] 启动萤核本体...
pushd "%FIREFLY_ROOT%" >nul
start "萤核本体" cmd /k "%FIREFLY_CMD%"
popd >nul

echo.
echo ============================================================
echo  启动完成：
echo   - Python 外挂服务：http://127.0.0.1:5002
echo   - 萤核前端：请在萤核侧边栏点击【人脸视频分类】
echo  关闭对应窗口即可停止服务。
echo ============================================================
echo.
pause
