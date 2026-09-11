import os
from dotenv import load_dotenv

# 第一步：把 .env 里的键值对加载进操作系统的进程内存中
load_dotenv()

# 第二步：从系统环境变量中把这个值捞出来
api_key = os.getenv("DEEPSEEK_API_KEY")

# 第三步：打印前 6 位进行验证（切片遮蔽，防泄露）
if api_key:
    print(f"成功读取到 Key: {api_key[:6]}******")
else:
    print("读取失败：未找到 DEEPSEEK_API_KEY，请检查 .env 文件命名和路径！")