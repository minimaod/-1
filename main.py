import sys
import os

print("[Debug 1] 正在启动 main.py...")

try:
    from agent.core import AgentEngine
    print("[Debug 2] 成功导入 AgentEngine 模块！")
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

            print("Agent > [正在等待网络首包响应...]", end="", flush=True)

            # 消费流式生成器
            first_token = True
            for chunk in engine.chat_stream(user_input):
                if first_token:
                    # 首包到达，把等待提示擦除或继续输出
                    first_token = False
                print(chunk, end="", flush=True)
            
            print()

        except KeyboardInterrupt:
            print("\n程序被手动中断，正在退出...")
            sys.exit(0)
        except Exception as e:
            print(f"\n[Error 请求异常]: {e}")

if __name__ == "__main__":
    main()