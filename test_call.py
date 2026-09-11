import os
from dotenv import load_dotenv
from openai import OpenAI

# 1. 加载 .env 文件的环境变量到系统内存
load_dotenv()

# 2. 从系统环境变量读取 API Key
api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise ValueError("环境变量中未找到 DEEPSEEK_API_KEY，请检查 .env 文件！")

# 3. 实例化客户端对象
# base_url 指向 DeepSeek 的 API 入口
client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com"
)

# 4. 向大模型发起单次同步调用
# 为什么叫 create？因为这是在向服务端“创建一个对话补全（Chat Completion）任务”
response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "user", "content": "Hello, 请用一句话证明你正常联网并收到了消息。"}
    ],
    stream=False  # 暂时不用流式传输（非打字机效果），等全部生成完一次性返回
)

# 5. 打印原始响应对象（看看底层到底返回了什么大字典）
print("========== 1. 完整的原始 Response 对象 ==========")
print(response)

# 6. 层层解构提取我们要的文本
print("\n========== 2. 解构提取出的核心内容 ==========")
content = response.choices[0].message.content
print(f"模型回复内容: {content}")