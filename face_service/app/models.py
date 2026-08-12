"""API 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    success: bool
    message: str = ""
    data: Optional[dict] = None


# ---- 扫描 ----
class ScanRequest(BaseModel):
    scan_folder: str = Field(..., description="源扫描目录 / 工作文件夹")
    output_dir: Optional[str] = Field(None, description="自定义输出根；为空则在源目录下建子文件夹")
    test_mode: bool = Field(True, description="测试预览模式（只预览不移动磁盘文件）")
    similarity: float = Field(0.55, ge=0.0, le=1.0, description="人脸相似度阈值")
    # 新增：使用角色库进行先匹配再聚类（默认为True）
    use_character_library: bool = Field(True, description="是否优先用已有角色库特征匹配")


# ---- 文件操作 ----
class MoveRequest(BaseModel):
    task_id: Optional[int] = None
    test_mode: bool = Field(True, description="True 只预览不写磁盘")


class ReprocessSingleRequest(BaseModel):
    mapping_id: int


# ---- 分组操作 ----
class MergeGroupRequest(BaseModel):
    source_group_id: int
    target_group_id: int


# ---- 角色（新）----
class CharacterRenameRequest(BaseModel):
    character_id: int
    new_name: str = Field(..., min_length=1, max_length=128)


class CharacterDeleteRequest(BaseModel):
    character_id: int


# ---- 工作文件夹设置（新）----
class WorkFolderSettingRequest(BaseModel):
    work_folder: str = Field(..., description="用户自定义工作文件夹路径")
    auto_scan_on_start: bool = Field(False, description="服务启动时是否自动扫描工作文件夹")
