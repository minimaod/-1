import json
from typing import AsyncGenerator, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import uuid
from agent.session import session_manager
# 导入现有 Agent 内核，保持分层解耦
from agent.core import AgentEngine

app = FastAPI(
    title="Mini Agent Server",
    description="基于 FastAPI + SSE 的异步 Agent 推流服务"
)

# 允许跨域（前端开发常用）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)




class ChatRequest(BaseModel):
    """
    Pydantic 请求体校验机理：
    - 校验 prompt 字段必须存在且为字符串
    - 约束字符串最小长度为 1，防止空文本击穿到 LLM
    """
    prompt: str = Field(..., min_length=1, description="用户提问内容")
    session_id: Optional[str] = Field(default=None, description="会话ID，若不传则由服务端自动分配")


async def sse_event_generator(
    user_input: str, 
    agent: AgentEngine, 
    session_id: str
) -> AsyncGenerator[str, None]:
    """
    协议转换适配器：
    绑定当前 session 的独立 agent 实例推流
    """
    try:
        # 首帧通知客户端当前生效的 session_id（方便客户端后续轮次带上）
        meta_data = json.dumps({"session_id": session_id}, ensure_ascii=False)
        yield f"event: session\ndata: {meta_data}\n\n"

        # 调用独占实例的流式方法，messages 互不干扰
        async for token in agent.chat_stream(user_input):
            data = json.dumps({"delta": token}, ensure_ascii=False)
            yield f"data: {data}\n\n"
            
        # 标准流结束标
        yield "data: [DONE]\n\n"
    except Exception as e:
        err_data = json.dumps({"error": str(e)}, ensure_ascii=False)
        yield f"event: error\ndata: {err_data}\n\n"


@app.post("/api/chat/stream")
async def chat_stream_endpoint(request: ChatRequest):
    # 1. 解析或生成 session_id
    session_id = request.session_id.strip() if request.session_id else str(uuid.uuid4())

    # 2. 从会话管理器中获取该会话专属的 Agent 实例（不存在会自动创建）
    agent = await session_manager.get_or_create(session_id)

    return StreamingResponse(
        sse_event_generator(request.prompt, agent, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-ID": session_id
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)