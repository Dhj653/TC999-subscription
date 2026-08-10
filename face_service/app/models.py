"""API 请求 / 响应模型。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    scan_folder: str = Field(..., description="源扫描目录")
    output_dir: Optional[str] = Field(None, description="自定义输出目录；为空则用源目录")
    test_mode: bool = Field(True, description="测试预览模式（默认开启）")
    similarity: float = Field(0.55, ge=0.0, le=1.0, description="人脸相似度阈值")


class MoveRequest(BaseModel):
    task_id: Optional[int] = None
    test_mode: bool = Field(True, description="是否仍走测试模式（True 只预览）")


class MergeGroupRequest(BaseModel):
    source_group_id: int
    target_group_id: int


class GroupRenameRequest(BaseModel):
    group_id: int
    new_name: str = Field(..., min_length=1, max_length=128)


class ReprocessSingleRequest(BaseModel):
    mapping_id: int


class ApiResponse(BaseModel):
    success: bool
    message: str = ""
    data: Optional[dict] = None
