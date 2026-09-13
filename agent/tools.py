import datetime

# 1. 本地真实可执行函数
def get_current_time(timezone: str = "local") -> str:
    """获取本地或指定格式的当前系统时间"""
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")

# 2. 告诉大模型的 JSON Schema 契约字典
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取操作系统当前的精确本地时间（年月日 时分秒）。当用户询问当前时间、日期或今天几号时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "时区名称，默认为 'local'",
                        "enum": ["local"]
                    }
                },
                "required": []
            }
        }
    }
]

# 3. 本地函数路由分发映射表
AVAILABLE_TOOLS = {
    "get_current_time": get_current_time
}