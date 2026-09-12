import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent / ".env"
print(f"1. .env 目标物理路径: {env_path}")
print(f"2. 文件是否存在: {env_path.exists()}")

if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()
    print("3. 文件内部实际文本行数:", len(content.splitlines()))
    # 打印前5个字符做脱敏验证
    load_dotenv(dotenv_path=env_path)
    key = os.getenv("OPENAI_API_KEY")
    if key:
        print(f"4. 成功读取到 Key！开头为: {key[:8]}******")
    else:
        print("4. 【异常】文件存在，但未能解析出 OPENAI_API_KEY，请检查文件内部书写格式！")