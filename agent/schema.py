"""
文件定位: mini_agent/agent/schema.py
功能: 定义全链路统一的 AgentEvent 数据协议
"""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class AgentEvent(BaseModel):
    type: Literal["text", "approval_required", "tool_status", "error", "done"] = Field(
        ..., description="事件类型"
    )
    # 纯文本片段或状态简述
    delta: Optional[str] = None
    # 审批相关载荷
    approval_id: Optional[str] = None
    tool_name: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    # 状态扩展字段
    status: Optional[str] = None
    error: Optional[str] = None