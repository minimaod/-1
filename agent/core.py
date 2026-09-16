import os
import json
from typing import Optional
from openai import AsyncOpenAI
from dotenv import load_dotenv
from typing import List, Dict, Any

from agent.tools import TOOLS_SCHEMA

load_dotenv()


class AgentEngine:
    """
    Agent 核心引擎：持有客户端、维护会话上下文、驱动工具调用流式判定。
    """
    def _estimate_message_tokens(self, message: Dict[str, Any]) -> int:
        """
        极轻量 Token 估算：
        - 针对 text content: 中英混合字符粗估折算
        - 针对 tool_calls: 计入 function name 与 arguments JSON 长度
        """
        token_count = 4  # 每条消息的基础元数据开销 (role 等)
        
        content = message.get("content")
        if content:
            # 工业界保守粗估：1 token ≈ 1.5 字符 (即 char_len * 0.7)
            token_count += int(len(str(content)) * 0.7)

        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                token_count += int(len(func.get("name", "")) * 0.7)
                token_count += int(len(func.get("arguments", "")) * 0.7)
                token_count += 10  # tool call 结构开销

        return max(token_count, 1)

    def _estimate_total_tokens(self, messages: List[Dict[str, Any]]) -> int:
        return sum(self._estimate_message_tokens(m) for m in messages)

    # --------------------------------------------------------------------------
    # [新增方法 2] 核心原子截断器：确保 Tool Call 绝不孤立
    # --------------------------------------------------------------------------
    def _truncate_history(self, max_tokens: int = 4000, max_turns: int = 5) -> None:
        """
        滑动窗口裁剪铁律：
        1. system 消息绝对钉死 (Pinning)
        2. 以 user 为起点的 Turn 作为最小原子单元，成对清除 tool_calls 与 tool 回传
        3. 优先满足 max_turns 约束，再收敛至 max_tokens 预算内
        """
        if not self.history:
            return

        # 1. 拆离 System 与 Dialogue 历史
        system_msgs = [m for m in self.history if m.get("role") == "system"]
        dialogue_msgs = [m for m in self.history if m.get("role") != "system"]

        if not dialogue_msgs:
            return

        # 2. 将交互聚合为原子 Turns
        turns: List[List[Dict[str, Any]]] = []
        current_turn: List[Dict[str, Any]] = []

        for msg in dialogue_msgs:
            if msg.get("role") == "user" and current_turn:
                turns.append(current_turn)
                current_turn = [msg]
            else:
                current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)

        # 3. 按照 max_turns 硬性保底截断 (保留最新的 max_turns 轮)
        if len(turns) > max_turns:
            turns = turns[-max_turns:]

        # 4. 基于 Token 预算继续回退裁剪最旧轮次
        while turns:
            # 组装当前待发全部消息进行算力估算
            flattened = [m for turn in turns for m in turn]
            projected = system_msgs + flattened
            total_est = self._estimate_total_tokens(projected)

            if total_est <= max_tokens or len(turns) <= 1:
                # 即使超出 max_tokens，也至少保留最新的 1 轮以保证单次任务闭环
                break

            # 丢弃最老的一个完整 Turn
            turns.pop(0)

        # 5. 回写状态：重组安全历史
        reconstructed = system_msgs + [m for turn in turns for m in turn]
        self.history = reconstructed

    # --------------------------------------------------------------------------
    # [修改方法] 定位 run() 或 chat() 中调用 API 前的执行点
    # --------------------------------------------------------------------------
    # def run(self, user_input: str):
    #     ...
    #     self.history.append({"role": "user", "content": user_input})
    #
    #     # <<< 在这里插入截断锚点 >>>
    #     self._truncate_history(max_tokens=3000, max_turns=3)
    #
    #     # 后续直接使用合法安全的水位发送请求：
    #     # response = self.client.chat.completions.create(
    #     #     model=self.model,
    #     #     messages=self.history,
    #     #     tools=self.tools_schema,
    #     #     ...
    #     # )
    def __init__(self, model: str = "deepseek-chat", system_prompt: Optional[str] = None):
        # 锁定使用 DeepSeek 配置
        self.client = AsyncOpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com"
        )
        self.model = model
        self.history = []

        if system_prompt:
            self.history.append({
                "role": "system",
                "content": system_prompt
            })

    async def chat_stream(self, prompt: str):
        """
        接收用户提问，更新历史，发起流式调用。
        - 若模型输出纯文本：yield ("text", 文本片段)
        - 若模型触发工具调用：静默组装碎片，循环结束后 yield ("tool_calls", 完整工具调用列表)
        """
        # 1. 记录用户输入
        self.history.append({"role": "user", "content": prompt})

        # 2. 发起流式请求，挂载 tools 菜单
        response_stream = await self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            stream=True
        )

        full_response = ""
        tool_calls_buffer = {}  # 存放碎片拼装：{index: {"id": "...", "name": "...", "arguments": "..."}}
        final_finish_reason = None

        # 3. 迭代每一个分片（Chunk）
        async for chunk in response_stream:
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                final_finish_reason = choice.finish_reason

            # 分支 A：普通文本流式
            if delta.content:
                full_response += delta.content
                yield ("text", delta.content)

            # 分支 B：捕获 tool_calls 碎片并缝合
            if delta.tool_calls:
                for tc_chunk in delta.tool_calls:
                    idx = tc_chunk.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {
                            "id": tc_chunk.id or "",
                            "name": tc_chunk.function.name if tc_chunk.function else "",
                            "arguments": ""
                        }
                    else:
                        if tc_chunk.id:
                            tool_calls_buffer[idx]["id"] += tc_chunk.id
                        if tc_chunk.function and tc_chunk.function.name:
                            tool_calls_buffer[idx]["name"] += tc_chunk.function.name

                    if tc_chunk.function and tc_chunk.function.arguments:
                        tool_calls_buffer[idx]["arguments"] += tc_chunk.function.arguments

        # 4. 流式结束，按协议归档历史上下文
        if final_finish_reason == "tool_calls":
            formatted_tool_calls = []
            for idx in sorted(tool_calls_buffer.keys()):
                item = tool_calls_buffer[idx]
                formatted_tool_calls.append({
                    "id": item["id"],
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item["arguments"]
                    }
                })

            # 核心协议：必须将带有 tool_calls 的 assistant 消息写入上下文
            self.history.append({
                "role": "assistant",
                "content": None,
                "tool_calls": formatted_tool_calls
            })

            # 告诉外层调用方：检测到工具调用意图，交出完整的执行名单
            yield ("tool_calls", formatted_tool_calls)

        elif full_response:
            # 普通文本聊天存入历史
            self.history.append({"role": "assistant", "content": full_response})
    async def send_tool_result_stream(self, tool_call_id: str, function_name: str, result_str: str):
        """
        阶段二闭环：将本地执行结果以 role: 'tool' 存入上下文，并再次发起流式生成。
        """
        # 1. 严格遵守协议格式：必须带 tool_call_id 证明对应的是哪次调用
        self.history.append({
            "role": "tool",
            "tool_call_id":tool_call_id,
            "name": function_name,
            "content": result_str
        })

        # 2. 携带完整历史（包括刚刚塞入的工具执行结果），二次请求模型
        response_stream = await self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            stream=True
        )

        full_response = ""
        async for chunk in response_stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full_response += delta
                yield delta

        if full_response:
            self.history.append({"role": "assistant", "content": full_response})
   

    def run_turn(self, messages: List[Dict[str, Any]]) -> str:
        """
        执行一轮完整对话交互（支持流式拼接、多工具调用与 HITL 拦截）
        """
        # 1. 触发第一阶段流式调用
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOLS_SCHEMA,
            stream=True
        )

        full_content = ""
        tool_calls_dict = {}

        print("🤖 Agent: ", end="", flush=True)

        for chunk in response:
            delta = chunk.choices[0].delta

            # 拼接文本输出
            if delta.content:
                full_content += delta.content
                print(delta.content, end="", flush=True)

            # 流式累积 tool_calls 碎片
            if delta.tool_calls:
                for tc_chunk in delta.tool_calls:
                    idx = tc_chunk.index
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": tc_chunk.id or "",
                            "type": "function",
                            "function": {
                                "name": tc_chunk.function.name or "",
                                "arguments": tc_chunk.function.arguments or ""
                            }
                        }
                    else:
                        if tc_chunk.id:
                            tool_calls_dict[idx]["id"] += tc_chunk.id
                        if tc_chunk.function.name:
                            tool_calls_dict[idx]["function"]["name"] += tc_chunk.function.name
                        if tc_chunk.function.arguments:
                            tool_calls_dict[idx]["function"]["arguments"] += tc_chunk.function.arguments

        print()  # 换行

        # 2. 如果没有工具调用，纯文本直接结束
        if not tool_calls_dict:
            messages.append({"role": "assistant", "content": full_content})
            return full_content

        # 3. 构造第一阶段的标准 Assistant 回传报文
        assembled_tool_calls = list(tool_calls_dict.values())
        messages.append({
            "role": "assistant",
            "content": full_content if full_content else None,
            "tool_calls": assembled_tool_calls
        })

        # 4. 遍历并执行所有工具调用（挂载 HITL）
        for tool_call in assembled_tool_calls:
            tool_id = tool_call["id"]
            tool_name = tool_call["function"]["name"]
            
            try:
                raw_args = tool_call["function"]["arguments"]
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}

            # --- [HITL 拦截判断点] ---
            if tool_name in SENSITIVE_TOOLS:
                approved = self._request_human_approval(tool_name, args)
                if not approved:
                    tool_output = "Error: 操作被系统操作员 (Human) 拒绝。请向用户说明已终止该操作，或询问下一步指示。"
                    print(f"🛑 [HITL] 已拒绝调用: {tool_name}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": tool_output
                    })
                    continue  # 跳过实际执行，进入下一个工具处理

            # 正常执行分发
            executor = TOOL_REGISTRY.get(tool_name)
            if executor:
                try:
                    tool_output = str(executor(**args))
                except Exception as e:
                    tool_output = f"Error: 执行异常 - {str(e)}"
            else:
                tool_output = f"Error: 工具 '{tool_name}' 未注册。"

            # 组装标准 tool 响应
            messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": tool_output
            })

        # 5. 第二阶段二次回传给模型，由大模型根据工具执行结果生成最终自然语言答复
        second_response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True
        )

        second_content = ""
        print("🤖 Agent (二次总结): ", end="", flush=True)
        for chunk in second_response:
            delta = chunk.choices[0].delta
            if delta.content:
                second_content += delta.content
                print(delta.content, end="", flush=True)
        print()

        messages.append({"role": "assistant", "content": second_content})
        return second_content