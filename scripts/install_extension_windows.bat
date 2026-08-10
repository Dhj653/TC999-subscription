@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 萤核-人脸视频分类 首次安装

REM ============================================================
REM  首次安装：集成前端文件 + 安装 Python 依赖
REM  仅在萤核 sidebar.js 增加一条路由（自动备份原文件）。
REM ============================================================

REM ---------- 路径配置（按需修改）----------
set "FIREFLY_ROOT=..\..\firefly-ai-folder-cn"
set "FACE_SERVICE_ROOT=..\face_service"
set "EXT_SRC=..\frontend_extension"
REM 集成到萤根项目内的独立外部文件夹（不放入 src-frontend/views）
set "EXT_DST=%FIREFLY_ROOT%\face_video_ext"
REM ----------------------------------------

echo ============================================================
echo  萤核-人脸视频分类 首次安装
echo ============================================================

REM 1) 复制前端扩展文件到独立外部文件夹
echo [1/4] 复制前端扩展文件...
if not exist "%EXT_DST%" mkdir "%EXT_DST%"
copy /Y "%EXT_SRC%\face_video_sidebar.js" "%EXT_DST%\face_video_sidebar.js" >nul
xcopy /Y /I /E "%EXT_SRC%\views" "%EXT_DST%\views" >nul
echo      已复制到 %EXT_DST%

REM 2) 打补丁：在 sidebar.js 增加一条路由（自动备份）
echo [2/4] 打补丁 sidebar.js...
set "SIDEBAR=%FIREFLY_ROOT%\src-frontend\src\layout\sidebar.js"
if not exist "%SIDEBAR%" (
    echo [警告] 未找到 %SIDEBAR%
    echo        请手动在萤核 sidebar.js 顶部增加：
    echo            import { faceVideoMenu } from '../../../face_video_ext/face_video_sidebar.js'
    echo        并在菜单数组中追加：
    echo            ...faceVideoMenu
) else (
    python "%~dp0patch_sidebar.py" "%SIDEBAR%" "%EXT_DST%\face_video_sidebar.js"
)

REM 3) 安装 Python 依赖
echo [3/4] 安装 Python 依赖...
pushd "%FACE_SERVICE_ROOT%" >nul
if not exist ".env" copy /Y ".env.example" ".env" >nul
python -m pip install -r requirements.txt
popd >nul

REM 4) 提示放置模型
echo [4/4] 检查 InsightFace 模型...
if not exist "%FACE_SERVICE_ROOT%\models\models\buffalo_l" (
    echo [提示] 未检测到本地模型 buffalo_l。
    echo        请下载 buffalo_l.zip 解压到：%FACE_SERVICE_ROOT%\models\models\buffalo_l\
    echo        （包含 det_10g.onnx / genderage.onnx / w600k_r50.onnx 等）
    echo        或在 .env 设置 DISABLE_MODEL_DOWNLOAD=false 允许首次联网下载。
)
echo.
echo ============================================================
echo  安装完成。请双击 start_all_windows.bat 启动。
echo ============================================================
pause
