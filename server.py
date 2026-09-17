import json
from typing import AsyncGenerator
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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

# 全局单例持有者
agent_instance: AgentEngine | None = None


@app.on_event("startup")
async def startup_event():
    """应用启动生命周期：挂载 Agent 引擎单例"""
    global agent_instance
    agent_instance = AgentEngine()


class ChatRequest(BaseModel):
    """
    Pydantic 请求体校验机理：
    - 校验 prompt 字段必须存在且为字符串
    - 约束字符串最小长度为 1，防止空文本击穿到 LLM
    """
    prompt: str = Field(..., min_length=1, description="用户提问内容")


async def sse_event_generator(user_input: str) -> AsyncGenerator[str, None]:
    """
    协议转换适配器：
    将底层 agent.chat_stream 输出的纯字符流，转变为标准 SSE JSON 帧
    """
    try:
        async for token in agent_instance.chat_stream(user_input):
            # 必须序列化为 JSON 字符串，安全转义内部换行符
            data = json.dumps({"delta": token}, ensure_ascii=False)
            yield f"data: {data}\n\n"
            
        # 标准流结束标
        yield "data: [DONE]\n\n"
    except Exception as e:
        err_data = json.dumps({"error": str(e)}, ensure_ascii=False)
        yield f"event: error\ndata: {err_data}\n\n"


@app.post("/api/chat/stream")
async def chat_stream_endpoint(request: ChatRequest):
    if agent_instance is None:
        raise HTTPException(status_code=503, detail="Agent 引擎未就绪")

    return StreamingResponse(
        sse_event_generator(request.prompt),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)