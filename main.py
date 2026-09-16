import sys
import os
import json
import asyncio
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

async def main():
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

            async for event_type, data in engine.chat_stream(user_input):
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
                            tool_result = await asyncio.to_thread(func, **args_dict)
                            print(f"[本地探针执行结果] -> {tool_result}")
                        # =========================================================

                        print("Agent > ", end="", flush=True)
                        async for chunk in engine.send_tool_result_stream(call_id, func_name, tool_result):
                            print(chunk, end="", flush=True)
                    else:
                        print(f"[Error] 未注册的本地工具: {func_name}")

            print()

        except KeyboardInterrupt:
            print("\n程序被手动中断，正在退出...")
            sys.exit(0)
        except Exception as e:
            print(f"\n[Error 请求异常]: {e}")
def assert_protocol_invariants(history):
    """
    协议不变式强校验（工业级防 400 校验器）：
    1. 首条必须是 system
    2. tool 必须紧随带有对应 tool_calls 的 assistant 之后
    3. assistant(tool_calls) 后的 tool 回传数量和 id 必须 1:1 闭环
    """
    if not history:
        return
    
    assert history[0]["role"] == "system", "[FATAL] System prompt 丢失！未被钉死！"
    
    i = 1
    while i < len(history):
        msg = history[i]
        
        # 检验 1：不能出现裸奔的 tool 消息
        if msg.get("role") == "tool":
            prev = history[i - 1]
            assert prev.get("role") == "assistant" and prev.get("tool_calls"), \
                f"[FATAL] 发现孤儿 tool 结果 (Index {i}): 没有前置的 assistant.tool_calls！"
        
        # 检验 2：若 assistant 发起 tool_calls，紧跟的必须是对应且完整的 tool 结果
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected_ids = [tc["id"] for tc in msg["tool_calls"]]
            actual_ids = []
            
            # 向后扫描所有紧随其后的 tool 消息
            j = i + 1
            while j < len(history) and history[j].get("role") == "tool":
                actual_ids.append(history[j].get("tool_call_id"))
                j += 1
            
            assert expected_ids == actual_ids, (
                f"[FATAL] 工具调用链断裂 (Index {i})！\n"
                f"期望的 call_ids: {expected_ids}\n"
                f"实际匹配的 ids: {actual_ids}"
            )
            i = j - 1  # 步进跳过已检查的 tool 块
            
        i += 1
    print("  └── [Pass] 协议配对不变式检查 100% 合法，无孤儿节点。")                   
def run_stress_test():
    print("==================================================")
    print(">>> 启动 Context 滑动窗口与协议原子性极限压测")
    print("==================================================")
    
    # 实例化引擎（请保持与你当前的初始化参数一致）
    # 设定较紧凑的预算以便高频触发截断：max_tokens=1500, max_turns=2
    engine = AgentEngine()
    
    # 模拟注入系统提示词
    engine.history = [
        {"role": "system", "content": "You are a senior DevOps SRE Assistant. Maintain strictly concise outputs."}
    ]
    
    # 构造测试轮次：轮次 1 ~ 轮次 4
    # 其中第 2 轮注入超大 Tool 结果（模拟读取了 15000 字符的系统日志）
    mock_turns = [
        # Turn 1: 普通对话
        [
            {"role": "user", "content": "Check server uptime."},
            {"role": "assistant", "content": "Server uptime is 45 days, load average: 0.12."}
        ],
        # Turn 2: 触发工具调用的复杂轮次（原子调用块注入）
        [
            {"role": "user", "content": "Read /var/log/nginx/access.log"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_nginx_001",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "/var/log/nginx/access.log"}'}
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_nginx_001",
                "content": "2026-09-15 ERROR 500 Connection reset by peer\n" * 150  # 注入膨胀文本
            },
            {"role": "assistant", "content": "Log shows multiple 500 errors caused by upstream connection drops."}
        ],
        # Turn 3: 另一轮普通对话
        [
            {"role": "user", "content": "What is the primary interface IP?"},
            {"role": "assistant", "content": "The eth0 IP is 192.168.1.100."}
        ],
        # Turn 4: 触发截断的新输入
        [
            {"role": "user", "content": "Restart nginx service now."}
        ]
    ]

    print("\n[Step 1] 逐轮压入消息并触发截断机制...")
    for idx, turn in enumerate(mock_turns, start=1):
        print(f"\n--- 模拟注入 Turn {idx} ---")
        engine.history.extend(turn)
        
        # 触发截断：限制 max_turns=2，max_tokens=800
        engine._truncate_history(max_tokens=800, max_turns=2)
        
        # 监控当前水位
        roles = [m["role"] for m in engine.history]
        est_tokens = engine._estimate_total_tokens(engine.history)
        print(f"  ├── 当前保留消息链: {roles}")
        print(f"  ├── 估算 Token 水位: {est_tokens}")
        
        # 强行断言校验
        assert_protocol_invariants(engine.history)

    # 验证最终保留的是不是最新轮次以及 System 是否在
    print("\n[Step 2] 验证边界与最终状态...")
    assert engine.history[0]["role"] == "system", "System Prompt 被错误丢弃！"
    assert "Restart nginx service now." in engine.history[-1]["content"], "最新一条指令未被保留！"
    
    print("\n>>> 所有极限注入断言成功通过！滑动窗口裁剪器架构稳健。")
# main.py 最底部
if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        run_stress_test()
    else:
        asyncio.run(main())