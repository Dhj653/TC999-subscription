@echo off
REM ================================================================
REM  萤核 + 人脸视频分类外挂 一键启动（Windows）
REM  v2 新增：角色库联动、口罩兼容、>=2视频建夹
REM ================================================================
REM  用法：双击运行，或 cmd 中执行 start_all_windows.bat
REM  前置：
REM    1. Python 3.10+ 已安装（勾选 Add to PATH）
REM    2. face_service/requirements.txt 依赖已安装
REM       （首次会自动 pip install -r face_service\requirements.txt）
REM    3. ffmpeg / ffprobe 已在 PATH 或 face_service 目录下
REM    4. InsightFace buffalo_l 模型在 face_service\models\models\buffalo_l\
REM       （若 .env 中 DISABLE_MODEL_DOWNLOAD=false 会自动下载）
REM    5. 萤核本体已启动
REM ================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] 切换到外挂目录...
cd /d "%~dp0face_service"
if errorlevel 1 (
  echo ❌ 找不到 face_service 目录，请确认脚本位置正确。
  pause & exit /b 1
)

REM --- 自动生成 .env（首次运行）---
if not exist ".env" (
  echo [首次] 生成默认 .env ...
  copy /Y ".env.example" ".env" >nul

  REM 尝试自动探测萤核数据库路径，否则使用外挂自带 data/face_service.db
  set "FFDB="
  if exist "%APPDATA%\firefly-ai-folder-cn\data.db" (
    set "FFDB=%APPDATA%\firefly-ai-folder-cn\data.db"
  ) else if exist "%APPDATA%\yonuc-ai-folder-desktop\data.db" (
    set "FFDB=%APPDATA%\yonuc-ai-folder-desktop\data.db"
  )

  if defined FFDB (
    REM 把 FIREFLY_DB_PATH 替换为探测到的路径
    powershell -NoProfile -Command ^
      "$c = Get-Content '.env' -Raw; $c = $c -replace 'FIREFLY_DB_PATH=\\./data/face_service\\.db','FIREFLY_DB_PATH=!FFDB:\=\\!'; Set-Content '.env' -Value $c -Encoding UTF8"
    echo ✅ 已自动配置萤核数据库路径：!FFDB!
  ) else (
    echo ℹ️  未检测到萤核数据库，使用外挂自带：face_service\data\face_service.db
  )
)

REM --- 依赖安装（首次或缺少时）---
echo [2/4] 检查 Python 依赖...
python -c "import fastapi, insightface, faiss, cv2, numpy" 2>nul
if errorlevel 1 (
  echo ⚠  依赖缺失，开始安装 requirements.txt ...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo ❌ 依赖安装失败，请手动 pip install -r face_service\requirements.txt
    pause & exit /b 2
  )
)

REM --- 端口检查 ---
echo [3/4] 检查 5002 端口是否被占用...
netstat -ano | findstr ":5002" | findstr LISTENING >nul
if not errorlevel 1 (
  echo ℹ️  5002 端口已有进程监听，假定 face_service 已启动。
) else (
  REM 启动外挂服务（后台）
  echo [4/4] 启动 face_service FastAPI 服务（后台），端口 5002 ...
  start "face_service_v2" /min cmd /k "cd /d ""%~dp0face_service"" && python run.py"
  REM 等待 5 秒让服务起来
  timeout /t 5 /nobreak >nul
)

REM --- 探测服务可用 ---
curl -s http://127.0.0.1:5002/api/health >nul 2>nul
if errorlevel 1 (
  echo ⚠  服务响应慢，再等 5 秒 ...
  timeout /t 5 /nobreak >nul
)

curl -s http://127.0.0.1:5002/api/health | findstr "success" >nul
if errorlevel 1 (
  echo ❌ 外挂服务未能正常启动，请检查上方 face_service 窗口的报错日志。
  pause & exit /b 3
)

echo ✅ 外挂服务 v2 已就绪：http://127.0.0.1:5002
echo.
echo 🔔 功能说明：
echo   • 同一角色 ≥ 2 个视频才建夹并移动
echo   • 戴口罩女性自动兼容（阈值放宽）
echo   • 工作文件夹 + 角色库自动匹配
echo   • 角色重命名联动重命名磁盘文件夹
echo   • 删除角色不删磁盘文件，用户自行决定
echo.
pause
endlocal
