"""
文件定位: mini_agent/agent/core.py
"""
import json
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from openai import AsyncOpenAI
import os
from agent.approval import approval_manager
from agent.schema import AgentEvent
from agent.tools import TOOL_REGISTRY, TOOLS_SCHEMA

# 敏感工具列表定义
SENSITIVE_TOOLS = {"write_file", "create_file"}


class AgentEngine:
   def __init__(
      self,
      model: str = "deepseek-chat",
      history: Optional[List[Dict[str, Any]]] = None,
  ):
    # 核心检查点：确保这行代码存在，且属性名是 self.client，缩进是严格的 8 个空格
    self.client = AsyncOpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )
    self.model = model
    self.history: List[Dict[str, Any]] = history if history is not None else []
   async def _call_llm_stream(
        self, messages: List[Dict[str, Any]]
    ) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        底层私有方法：仅负责驱动 OpenAI 接口，将 Chunk 缝合成完整结构。
        - 文本分片实时 yield ("text", delta.content)
        - 工具调用由内部组装，最后 yield ("tool_calls", formatted_tool_calls)
        - 普通完成 yield ("text_done", full_content)
        """
        response_stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            stream=True,
        )

        full_content = ""
        tool_calls_dict = {}

        async for chunk in response_stream:
            choice = chunk.choices[0]
            delta = choice.delta

            # 1. 纯文本片段下发
            if delta.content:
                full_content += delta.content
                yield ("text", delta.content)

            # 2. 工具调用碎片收集
            if delta.tool_calls:
                for tc_chunk in delta.tool_calls:
                    idx = tc_chunk.index
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": tc_chunk.id or "",
                            "type": "function",
                            "function": {
                                "name": tc_chunk.function.name or "",
                                "arguments": tc_chunk.function.arguments or "",
                            },
                        }
                    else:
                        if tc_chunk.id:
                            tool_calls_dict[idx]["id"] += tc_chunk.id
                        if tc_chunk.function and tc_chunk.function.name:
                            tool_calls_dict[idx]["function"]["name"] += tc_chunk.function.name
                        if tc_chunk.function and tc_chunk.function.arguments:
                            tool_calls_dict[idx]["function"]["arguments"] += tc_chunk.function.arguments

        # 3. 流式结束归总发射
        if tool_calls_dict:
            assembled = [
                tool_calls_dict[i] for i in sorted(tool_calls_dict.keys())
            ]
            yield ("tool_calls", assembled)
        else:
            yield ("text_done", full_content)

   async def run_turn(self, user_input: str) -> AsyncGenerator[AgentEvent, None]:
        """
        对外暴露的唯一顶层异步生成器：
        调度 ReAct 循环，处理 HITL 拦截，产出标准 AgentEvent。
        """
        self.history.append({"role": "user", "content": user_input})

        while True:
            step_full_text = ""
            pending_tool_calls = None

            # 消费底层碎片拼装
            async for ev_type, payload in self._call_llm_stream(self.history):
                if ev_type == "text":
                    step_full_text += payload
                    yield AgentEvent(type="text", delta=payload)
                elif ev_type == "tool_calls":
                    pending_tool_calls = payload
                elif ev_type == "text_done":
                    step_full_text = payload

            # 情况 A：模型仅产出自然文本，轮次正常结束
            if not pending_tool_calls:
                self.history.append({"role": "assistant", "content": step_full_text})
                yield AgentEvent(type="done")
                return

            # 情况 B：模型决定调用工具，将工具意图归档至历史
            self.history.append({
                "role": "assistant",
                "content": step_full_text if step_full_text else None,
                "tool_calls": pending_tool_calls,
            })

            # 逐个处理工具调用（挂载 HITL 拦截）
            for call in pending_tool_calls:
                tool_id = call["id"]
                tool_name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}

                # === [HITL 拦截判断点] ===
                if tool_name in SENSITIVE_TOOLS:
                    approval_id = str(uuid.uuid4())
                    fut = await approval_manager.create_approval(approval_id)

                    # 1. 必须先向外发事件
                    yield AgentEvent(
                        type="approval_required",
                        approval_id=approval_id,
                        tool_name=tool_name,
                        args=args,
                    )

                    # 2. 协程在此挂起，等待 resolve_approval 唤醒
                    decision = await fut

                    if decision != "approve":
                        tool_output = "Error: 操作被系统操作员拒绝。请向用户说明已终止该操作。"
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": tool_output,
                        })
                        continue

                # 正常执行工具
                yield AgentEvent(
                    type="tool_status",
                    status=f"正在执行工具: {tool_name}"
                )
                executor = TOOL_REGISTRY.get(tool_name)
                if executor:
                    try:
                        tool_output = str(executor(**args))
                    except Exception as e:
                        tool_output = f"Error: 执行异常 - {str(e)}"
                else:
                    tool_output = f"Error: 工具 '{tool_name}' 未注册。"

                self.history.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": tool_output,
                })

            # 工具结果已全部塞入 history，循环自动进行下一轮 LLM 推理与总结
        """
        执行一轮完整对话交互：支持全链路 SSE 流式拼接、多工具调用与 Web 异步 HITL 审批
        """
        # 1. 记录用户输入到上下文历史
        self.history.append({"role": "user", "content": user_input})

        # 2. 触发第一阶段异步流式调用
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            tools=TOOLS_SCHEMA,
            stream=True
        )

        full_content = ""
        tool_calls_dict = {}

        # 异步迭代分片，向前端实时吐出文本
        async for chunk in response:
            delta = chunk.choices[0].delta

            # 文本片段：推给前端渲染打字机
            if delta.content:
                full_content += delta.content
                yield ("text", delta.content)

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
                        if tc_chunk.function and tc_chunk.function.name:
                            tool_calls_dict[idx]["function"]["name"] += tc_chunk.function.name
                        if tc_chunk.function and tc_chunk.function.arguments:
                            tool_calls_dict[idx]["function"]["arguments"] += tc_chunk.function.arguments

        # 3. 如果模型没有发起工具调用，普通对话直接归档结束
        if not tool_calls_dict:
            self.history.append({"role": "assistant", "content": full_content})
            return

        # 4. 构造标准 Assistant 历史报文（记录调用意图）
        assembled_tool_calls = list(tool_calls_dict.values())
        self.history.append({
            "role": "assistant",
            "content": full_content if full_content else None,
            "tool_calls": assembled_tool_calls
        })

        # 5. 遍历并执行所有工具调用（挂载 HITL 拦截）
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
                # 1. 生成唯一审批流水号
                approval_id = str(uuid.uuid4())

                # 2. 挂起 Future 承诺
                fut = await approval_manager.create_approval(approval_id)

                # 3. 向前端下发审批事件帧
                yield ("approval_required", {
                    "approval_id": approval_id,
                    "tool_name": tool_name,
                    "args": args
                })

                # 4. 【核心挂起】：协程暂停，等待外部接口解冻
                decision = await fut

                if decision != "approve":
                    tool_output = "Error: 操作被系统操作员 (Human) 拒绝。请向用户说明已终止该操作，或询问下一步指示。"
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": tool_output
                    })
                    continue  # 用户拒绝，跳过真正执行

            # 正常工具执行
            executor = TOOL_REGISTRY.get(tool_name)
            if executor:
                try:
                    tool_output = str(executor(**args))
                except Exception as e:
                    tool_output = f"Error: 执行异常 - {str(e)}"
            else:
                tool_output = f"Error: 工具 '{tool_name}' 未注册。"

            # 组装标准 tool 响应回填上下文
            self.history.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": tool_output
            })

        # 6. 第二阶段调用：让大模型根据工具执行结果生成最终总结，并流式推给前端
        second_response = await self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            stream=True
        )

        second_content = ""
        async for chunk in second_response:
            delta = chunk.choices[0].delta
            if delta.content:
                second_content += delta.content
                yield ("text", delta.content)

        self.history.append({"role": "assistant", "content": second_content})
        return
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
            return 

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
                # 1. 生成本次高危操作的唯一审批 ID
                approval_id = str(uuid.uuid4())

                # 2. 在审批池中挂起一个 Future 承诺
                fut = await approval_manager.create_approval(approval_id)

                # 3. 产出（yield）一个审批事件通知，供上层推流给前端
                yield ("approval_required", {
    "approval_id": approval_id,
    "tool_name": tool_name,
    "args": args
})

                # 4. 【核心挂起】：协程在此暂停，让出 CPU，直到审批接口填入结果
                decision = await fut  # decision 取值为 "approve" 或 "reject"

                if decision != "approve":
                    tool_output = "Error: 操作被系统操作员 (Human) 拒绝。请向用户说明已终止该操作，或询问下一步指示。"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": tool_output
                    })
                    continue  # 用户拒绝，跳过执行，进入下一个工具处理

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
        return 