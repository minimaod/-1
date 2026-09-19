import asyncio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

async def mock_llm_stream():
    """模拟大模型逐字吐出 Token 的异步生成器"""
    words = ["你好", "，", "我是", "正在", "以", "SSE", "协议", "推流的", "Agent", "！"]
    for word in words:
        await asyncio.sleep(0.3)  # 模拟大模型生成 token 的耗时
        # 严格遵守 SSE 规范：data: <payload>\n\n
        yield f"data: {word}\n\n"
    
    # 行业通用约定：发送 [DONE] 表示流传输彻底结束
    yield "data: [DONE]\n\n"

@app.get("/test/stream")
async def test_stream_endpoint():
    return StreamingResponse(
        mock_llm_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)