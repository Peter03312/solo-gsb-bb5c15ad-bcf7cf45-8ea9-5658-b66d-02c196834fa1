"""请求体的 Pydantic 模型（业务校验在路由内完成以给出明确错误码）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RectIn(BaseModel):
    x: int
    y: int
    w: int
    h: int
    reason: str = Field(max_length=500)


class AnnotationsPut(BaseModel):
    """整体替换当前标注；revision 必须等于服务端当前修订号（乐观锁）。"""

    revision: int = Field(ge=0)
    rects: list[RectIn] = Field(default_factory=list, max_length=1000)
