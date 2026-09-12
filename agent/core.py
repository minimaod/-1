import os
from typing import Optional
from openai import OpenAI
from dotenv import load_dotenv

# 加载 .env 环境变量
load_dotenv()


class AgentEngine:
    """
    Agent 核心引擎：负责持有客户端、维护会话上下文状态、驱动对话生成。
    """

    def __init__(self, model: str = "gpt-4o-mini", system_prompt: Optional[str] = None):
        self.client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL")
        )
        self.model = model
        self.history = []

        if system_prompt:
            self.history.append({
                "role": "system",
                "content": system_prompt
            })

    def chat_stream(self, prompt: str):
        """
        接收用户提问，更新历史，发起流式调用，并通过 yield 逐字吐出内容。
        """
        # 1. 将当前用户提问追加到上下文中
        self.history.append({"role": "user", "content": prompt})

        # 2. 发起流式请求
        response_stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            stream=True
        )

        full_response = ""

        # 3. 迭代每一个分片（Chunk）
        for chunk in response_stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full_response += delta
                yield delta

        # 4. 完整的回复存入上下文，形成多轮闭环
        self.history.append({"role": "assistant", "content": full_response})