# 萤核-人脸视频分类外挂扩展

基于 `firefly-ai-folder-cn v3.2.1（萤核）` 的外挂扩展，新增**人脸视频分类**能力，并补齐对标 FileNeatAI 的缺失功能。

- 架构：萤核本体（Node.js + Vue3 + SQLite）+ 独立 Python FastAPI 外挂服务（端口 5002）
- 约束：尽量不修改原版源码；仅 `sidebar.js` 增加一条路由；复用萤核 SQLite 仅新增 3 张表
- 人脸：InsightFace 本地模型 + FAISS 聚类（禁用 Ollama，禁入 Node 端）

## 目录结构

```
.
├── face_service/                 # Python FastAPI 外挂服务（端口 5002）
│   ├── app/
│   │   ├── main.py               # FastAPI 入口（CORS + 启动建表 + 断点续跑）
│   │   ├── config.py             # .env 配置加载 + 运行时可调参数
│   │   ├── database.py            # 复用萤核 SQLite，仅新增 3 表
│   │   ├── models.py             # Pydantic 请求模型
│   │   ├── routers/              # scan / groups / operations 三组路由
│   │   ├── services/
│   │   │   ├── face_engine.py    # InsightFace 封装：女性正脸过滤 + 特征
│   │   │   ├── video_processor.py# ffmpeg 抽帧（文件/内存字节）
│   │   │   ├── clustering.py     # FAISS 增量聚类 + 人名冲突判定
│   │   │   ├── archive.py         # zip/7z 内存解析（加密跳过）
│   │   │   ├── name_parser.py    # 视频文件名人名解析
│   │   │   ├── path_safety.py     # 路径安全校验
│   │   │   ├── file_mover.py     # 文件移动/回滚（测试模式管控）
│   │   │   └── task_queue.py     # 串行任务编排（单视频处理完清缓存）
│   │   └── utils/logger.py
│   ├── models/                   # 放置 InsightFace buffalo_l 模型包
│   ├── logs/
│   ├── run.py
│   ├── requirements.txt
│   ├── .env.example
│   └── .env                      # 首次安装自动生成
├── frontend_extension/           # 前端新增（仅新增文件）
│   ├── views/FaceVideoClassify.vue   # 独立页面
│   └── face_video_sidebar.js          # 侧边栏路由模块
├── scripts/
│   ├── start_all_windows.bat     # 一键启动：萤核 + Python 服务
│   ├── install_extension_windows.bat # 首次安装：集成前端 + 装依赖
│   └── patch_sidebar.py          # sidebar.js 幂等补丁（自动备份）
└── docs/验收清单.md               # 自测验收清单
```

## 快速开始（Windows）

### 1. 首次安装
双击 `scripts/install_extension_windows.bat`：
- 复制前端扩展到 `{萤核根}/face_video_ext/`（独立文件夹，不入 `src-frontend/views`）
- 自动补丁 `src-frontend/src/layout/sidebar.js`（自动备份 `.bak`，幂等）
- `pip install -r face_service/requirements.txt`
- 生成 `face_service/.env`
- 提示放置 InsightFace 模型

> 路径变量在两个 `.bat` 顶部，按实际萤核目录修改：
> `FIREFLY_ROOT`（萤核根）、`FACE_SERVICE_ROOT`、`EXT_SRC`、`EXT_DST`。

### 2. 放置人脸模型（避免联网下载）
下载 `buffalo_l.zip`（InsightFace 官方模型包），解压到：
```
face_service/models/models/buffalo_l/
```
应包含：`det_10g.onnx`、`genderage.onnx`、`w600k_r50.onnx` 等。
否则在 `.env` 设 `DISABLE_MODEL_DOWNLOAD=false` 允许首次联网下载。

### 3. 配置 .env
关键项：
- `FIREFLY_DB_PATH`：萤核 SQLite 路径（复用其数据库，仅新增表）
- `FACE_SIMILARITY_THRESHOLD`：人脸相似度阈值（前端滑块可运行时调）
- `FACE_GENDER_FEMALE_VALUE`：性别判定值（默认 0=女；若过滤反了改 1）
- `TEST_PREVIEW_MODE`：测试预览模式默认开启
- `BLOCKED_PATH_ROOTS`：禁止输出路径

### 4. 一键启动
双击 `scripts/start_all_windows.bat`：
- 启动 Python 服务（端口 5002，新窗口）
- 启动萤核本体（新窗口）
- 萤核侧边栏点击【人脸视频分类】即可使用

## 核心能力

### 人脸视频聚类
- InsightFace 本地模型提取女性正脸特征（男性/侧脸/大角度/模糊/遮挡丢弃）
- ffmpeg 抽帧 + FAISS 增量无监督聚类
- 强制串行，单视频处理完清空人脸/帧缓存再处理下一个
- 进度入库，支持断点续跑

### 人名识别与冲突处理
- 同一聚类分组解析出**单一**人名 → 自动重命名为该演员名
- 同一聚类分组解析出**多个不同**人名 → 标记 `name_conflict`，禁止自动改名/合并，交人工
- 同一演员部分视频带名部分不带名 → 允许自动重命名（带名的视频触发后覆盖全组）

### 多人视频
- 单视频检测出 ≥2 个有效女性正脸 → 归入【多人】分组，不复制拆分源文件

### FileNeatAI 补齐能力
- 内存解析 zip/7z 直读包内视频，加密包跳过并记录日志
- 测试预览模式默认开启，绝不移动磁盘文件
- 移动/改名前存原始路径与原名，支持一键回滚
- 人工纠错：合并分组、删除分组、重新处理单个视频、手动改名

## API 接口（端口 5002，返回 JSON）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/scan_folder | 启动人脸分析任务（支持压缩包） |
| GET  | /api/task_status | 进度/队列/运行日志 |
| GET  | /api/person_groups | 全部分组+视频+状态标签 |
| POST | /api/move_files | 执行移动（受测试模式管控） |
| POST | /api/merge_group | 合并两个分组 |
| POST | /api/revert_all_files | 一键回滚 |
| POST | /api/reprocess_single | 重新处理单个视频 |
| PUT  | /api/group_rename | 手动改名 |
| GET  | /api/group_extract_names | 分组提取的全部人名+冲突标记 |

辅助：`GET /api/health`、`GET /api/config`、`POST /api/stop_task`、`POST /api/delete_group`、`POST /api/move_single`。

## 数据库新增表（复用萤核 SQLite）

- `video_task`：任务队列/状态/进度/日志/输出路径
- `face_person_group`：分组 id/名称/状态标记（auto_numbered/renamed/name_conflict/multi_person）/质心
- `face_video_mapping`：视频-分组关联/原始路径/原始分组名/来源/移动状态（回滚依据）

## 健壮性设计

- **懒加载重依赖**：numpy/cv2/faiss/insightface 在实际使用时才导入，服务无模型也能启动并服务 health/config/groups 等接口
- **优雅降级**：任务在缺依赖时记录错误并标记 failed，不崩溃服务
- **路径安全**：拒绝系统受限根、非法字符、路径穿越；输出路径二次校验

## 约束合规

- ✅ 不修改萤核原版业务代码/页面/接口（仅 sidebar.js 一条路由）
- ✅ 人脸/抽帧/聚类/压缩包/排队全在独立 Python 进程
- ✅ InsightFace 禁入 Node；禁用 Ollama 聚类
- ✅ Vue 页面存放外部独立文件夹
- ✅ 输出双模式（源目录内 / 自定义输出目录）
- ✅ 路径安全校验
- ✅ 强制串行，单视频处理完清缓存
- ✅ 多人视频不拆分源文件
- ✅ 多不同人名不自动合并/改名

详见 `docs/验收清单.md`。
