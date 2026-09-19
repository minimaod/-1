"""
【模块职责导读】
上下文生命周期与滑动窗口治理（Context Manager）
- 职责范围：负责会话消息队列的 Token 估算、历史消息滑动窗口裁剪与协议完整性保护。
- 面试高频考点与核心设计原则：
  1. System Pinning（系统提示词钉住）：确保 index=0 的系统级 Prompt 永远不被挤出上下文。
  2. Tool Pair Atomicity（工具调用原子对保护）：确保 assistant 的 tool_calls 与 tool 返回结果成对保留或成对丢弃，杜绝协议层 400 校验异常。
"""

import json
from typing import Any, Dict, List
from config import settings


class ContextManager:
    """
    上下文裁剪与 Token 预算管理者
    """

    def __init__(self, max_tokens: int = settings.MAX_CONTEXT_TOKENS) -> None:
        self.max_tokens = max_tokens

    @staticmethod
    def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
        """
        【粗粒度 Token 快速估算法】

        【Why 为什么不直接用 tiktoken？】
        1. 避免对非官方 tokenizer 库的重依赖与 C 扩展在不同操作系统下的构建坑。
        2. 中英文混排场景下，中文约 1.5~2 字符/token，英文约 3~4 字符/token。
           经验公式：字符串总长度 / 2 可以在极低开销下（O(N) 字符串遍历）提供偏保守的安全边界。
        """
        total_chars = 0
        for msg in messages:
            content = msg.get("content") or ""
            total_chars += len(content)

            if "tool_calls" in msg and msg["tool_calls"]:
                total_chars += len(json.dumps(msg["tool_calls"], ensure_ascii=False))

        return total_chars // 2

    def truncate_history(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        根据 Token 上限执行安全滑动窗口裁剪。

        【底层踩坑防错点（Caveats）】：
        1. 保留 System Message：如果存在 system prompt，将其锁定在首位。
        2. 保留最新轮次：从后往前保留最新的交互，直到 Token 超标。
        3. 维护 Tool Pair Atomicity：修剪边界若落入 tool 响应或 tool_calls 中间，必须回退至安全边界。
        """
        if not messages:
            return []

        has_system = messages[0].get("role") == "system"
        system_msg = messages[0] if has_system else None
        chat_history = messages[1:] if has_system else messages[:]

        # 【防错点】返回浅拷贝而非原始引用，避免调用方后续对 self.history 的增删
        # 反向污染已交出的视窗（异步生成器场景下极易形成隐蔽的时序耦合）。
        if self.estimate_tokens(messages) <= self.max_tokens:
            return list(messages)

        retained: List[Dict[str, Any]] = []
        current_tokens = self.estimate_tokens([system_msg]) if system_msg else 0

        for msg in reversed(chat_history):
            msg_tokens = self.estimate_tokens([msg])
            if current_tokens + msg_tokens > self.max_tokens:
                break
            retained.insert(0, msg)
            current_tokens += msg_tokens

        # 核心防错：若首条消息是孤立的 tool 响应，剔除它以保证协议完整
        while retained and retained[0].get("role") == "tool":
            retained.pop(0)

        if system_msg:
            return [system_msg] + retained
        return retained
