import sys
import os
import json

print("[Debug 1] 正在启动 main.py...")

try:
    from agent.core import AgentEngine
    from agent.tools import AVAILABLE_TOOLS
    print("[Debug 2] 成功导入 AgentEngine 与工具模块！")
except Exception as e:
    print(f"[Error 导包失败] {e}")
    sys.exit(1)

def main():
    print("[Debug 3] 正在实例化 AgentEngine...")
    try:
        system_prompt = "你是一个专业且精通底层原理的 AI 架构导师。"
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

            # 消费首轮流式
            need_tool_execution = False
            pending_tool_calls = []

            for event_type, data in engine.chat_stream(user_input):
                if event_type == "text":
                    print(data, end="", flush=True)
                elif event_type == "tool_calls":
                    need_tool_execution = True
                    pending_tool_calls = data

            # 如果触发了工具调用，开启阶段二：本地执行并回传
            if need_tool_execution:
                for tool_call in pending_tool_calls:
                    call_id = tool_call["id"]
                    func_name = tool_call["function"]["name"]
                    raw_args = tool_call["function"]["arguments"]

                    print(f"\n[系统调度] 检测到工具调用请求: {func_name}")
                    print(f"[系统调度] 参数解析: {raw_args}")

                    # 1. 动态查找并安全执行本地函数
                    if func_name in AVAILABLE_TOOLS:
                        func = AVAILABLE_TOOLS[func_name]
                        # 容错反序列化参数字典
                        args_dict = json.loads(raw_args) if raw_args else {}
                        
                        # 真正执行本地 Python 代码
                        tool_result = func(**args_dict)
                        print(f"[本地探针执行结果] -> {tool_result}")

                        # 2. 将结果交差回传，驱动二次流式输出
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