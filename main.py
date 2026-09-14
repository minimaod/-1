import sys
import os
import json
from agent.tools import TOOL_REGISTRY

print("[Debug 1] 正在启动 main.py...")

try:
    from agent.core import AgentEngine
    print("[Debug 2] 成功导入 AgentEngine 与工具模块！")
except Exception as e:
    print(f"[Error 导包失败] {e}")
    sys.exit(1)

# 定义高危操作名单（只读工具放行，写/改/删操作强制拦截）
SENSITIVE_TOOLS = {"write_file"}

def request_human_approval(func_name: str, args_dict: dict) -> bool:
    """HITL 拦截闸门：展示风险，等待人工决策"""
    print("\n" + "=" * 50)
    print("🚨 [SECURITY ALERT] 检测到高危操作调用申请！")
    print(f"🔧 申请工具: {func_name}")
    print(f"📦 操作参数: {json.dumps(args_dict, ensure_ascii=False, indent=2)}")
    print("=" * 50)
    while True:
        decision = input("👉 是否批准该操作执行？(y/n): ").strip().lower()
        if decision in ("y", "yes"):
            return True
        if decision in ("n", "no"):
            return False
        print("请输入 'y' 批准 或 'n' 拒绝。")

def main():
    print("[Debug 3] 正在实例化 AgentEngine...")
    try:
        system_prompt = (
            "你是一个精通软件工程的本地开发者助手 (Local Dev & Ops Agent)。"
            "你可以读取项目代码来自省，也可以向沙箱目录 workspace/ 写入文件。"
            "当系统提示操作被用户拒绝时，请得体终止或询问用户替代方案。"
        )
        engine = AgentEngine(model="deepseek-chat", system_prompt=system_prompt)
        print("[Debug 4] 引擎初始化完成！")
    except Exception as e:
        print(f"[Error 引擎初始化失败] {e}")
        return

    print("=" * 50)
    print("Mini Agent CLI 引擎已就绪 (输入 'exit' 或 'quit' 退出)")
    print("=" * 50)

    while True:
        try:
            user_input = input("\nUser > ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["exit", "quit"]:
                print("引擎已关闭。")
                break

            print("Agent > ", end="", flush=True)

            need_tool_execution = False
            pending_tool_calls = []

            for event_type, data in engine.chat_stream(user_input):
                if event_type == "text":
                    print(data, end="", flush=True)
                elif event_type == "tool_calls":
                    need_tool_execution = True
                    pending_tool_calls = data

            if need_tool_execution:
                for tool_call in pending_tool_calls:
                    call_id = tool_call["id"]
                    func_name = tool_call["function"]["name"]
                    raw_args = tool_call["function"]["arguments"]

                    print(f"\n[系统调度] 检测到工具调用请求: {func_name}")
                    print(f"[系统调度] 参数解析: {raw_args}")

                    if func_name in TOOL_REGISTRY:
                        func = TOOL_REGISTRY[func_name]
                        args_dict = json.loads(raw_args) if raw_args else {}

                        # ==================== [HITL 核心拦截点] ====================
                        is_approved = True
                        if func_name in SENSITIVE_TOOLS:
                            is_approved = request_human_approval(func_name, args_dict)

                        if not is_approved:
                            # 关键：被拒绝时绝不抛异常中断，而是组装成标准 tool 报文告知模型
                            tool_result = "Error: 操作已被系统操作员 (Human) 显式拒绝。请立即停止写入，向用户说明情况并询问替代方案。"
                            print(f"[HITL 拦截] 🛑 操作已被人工否决！")
                        else:
                            # 真正执行本地 Python 代码
                            tool_result = func(**args_dict)
                            print(f"[本地探针执行结果] -> {tool_result}")
                        # =========================================================

                        print("Agent > ", end="", flush=True)
                        for chunk in engine.send_tool_result_stream(call_id, func_name, tool_result):
                            print(chunk, end="", flush=True)
                    else:
                        print(f"[Error] 未注册的本地工具: {func_name}")

            print()

        except KeyboardInterrupt:
            print("\n程序被手动中断，正在退出...")
            sys.exit(0)
        except Exception as e:
            print(f"\n[Error 请求异常]: {e}")

if __name__ == "__main__":
    main()
