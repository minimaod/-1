"""文件定位: mini_agent/server.py 功能: FastAPI 服务端，提供多会话管理、SSE 流式事件下发与异步 HITL

审批回调接口
"""
from dotenv import load_dotenv

load_dotenv()  # 必须在所有业务 import 之前执行
import json
import traceback
from typing import AsyncGenerator, Optional
import uuid

from agent.approval import approval_manager
from agent.core import AgentEngine
from agent.schema import AgentEvent
from agent.session import session_manager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Mini-Agent SSE & HITL Service")

# 允许跨域（方便前端集成）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 请求与响应数据模型 ====================


class ChatStreamRequest(BaseModel):
  prompt: str = Field(..., description="用户输入的自然语言指令")
  session_id: Optional[str] = Field(
      None, description="会话 ID，缺省时系统自动生成"
  )


class ApprovalActionRequest(BaseModel):
  approval_id: str = Field(..., description="待处理的审批流水号")
  action: str = Field(
      ..., pattern="^(approve|reject)$", description="操作动作：approve 或 reject"
  )


# ==================== 协议适配器 ====================


async def sse_event_generator(
    user_input: str,
    agent: AgentEngine,
    session_id: str,
) -> AsyncGenerator[str, None]:
  """协议转换适配器：

  消费 agent.run_turn 产出的标准化 AgentEvent，格式化为 SSE 文本帧
  """
  try:
    # 1. 首帧下发会话 ID
    session_payload = json.dumps({"session_id": session_id}, ensure_ascii=False)
    yield f"event: session\ndata: {session_payload}\n\n"

    # 2. 迭代消费顶层引擎下发的结构化事件
    async for event in agent.run_turn(user_input):
      if event.type == "text":
        data = json.dumps({"delta": event.delta}, ensure_ascii=False)
        yield f"data: {data}\n\n"

      elif event.type == "approval_required":
        payload = {
            "approval_id": event.approval_id,
            "tool_name": event.tool_name,
            "args": event.args,
        }
        yield (
            f"event: approval_required\ndata:"
            f" {json.dumps(payload, ensure_ascii=False)}\n\n"
        )

      elif event.type == "tool_status":
        data = json.dumps({"status": event.status}, ensure_ascii=False)
        yield f"event: status\ndata: {data}\n\n"

      elif event.type == "done":
        # 【关键修复点】：在 yield [DONE] 之前，先完成 SQLite 落盘！
        try:
          await session_manager.save_session(session_id)
          print(f">>> [落盘成功] 会话 {session_id} 的历史已写入 SQLite！")
        except Exception as err:
          print(f">>> [落盘失败] {err}")

        yield "data: [DONE]\n\n"

  except Exception as e:
    err_data = json.dumps({"error": str(e)}, ensure_ascii=False)
    yield f"event: error\ndata: {err_data}\n\n"
@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatStreamRequest):
  """流式对话接口：接收输入并返回 SSE 数据流"""
  session_id = req.session_id or str(uuid.uuid4())
  # 获取或并发安全地创建 Agent 实例（支持磁盘冷加载）
  agent = await session_manager.get_or_create(session_id)

  return StreamingResponse(
      sse_event_generator(req.prompt, agent, session_id),
      media_type="text/event-stream",
      headers={
          "Cache-Control": "no-cache",
          "Connection": "keep-alive",
          "X-Accel-Buffering": "no",  # 防止 Nginx 缓存 SSE 数据
      },
  )


@app.post("/api/approval/action")
async def approval_action_endpoint(req: ApprovalActionRequest):
  """审批回调接口：接收人工批准或拒绝决定，唤醒对应的挂起协程"""
  success = await approval_manager.resolve_approval(req.approval_id, req.action)
  if not success:
    raise HTTPException(
        status_code=404,
        detail="审批流水号无效、已过期或已被处理",
    )
  return {
      "status": "success",
      "action": req.action,
      "approval_id": req.approval_id,
  }


if __name__ == "__main__":
  import uvicorn

  uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)