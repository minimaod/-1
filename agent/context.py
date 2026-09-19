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
    def _estimate_chars(messages: List[Dict[str, Any]]) -> int:
        """
        【唯一度量口径】估算消息队列的原始字符总量。

        【Why 必须单独抽出这一层（底层踩坑防错点）】
        Token 估算最终要把字符总量做一次 // 2 取整。若各处分别对「单条」取整后
        再累加，即 Σ floor(xᵢ/2)，其结果**恒小于等于**对「总量」取整的
        floor(Σxᵢ/2)，且窗口越长低估越多（200 条短消息可低估至实际值的 0%）。
        裁剪循环与预算校验若各用一个口径，max_tokens 这个契约就形同虚设。
        故所有累计都必须在「字符」维度进行，只在最终比较时取整一次。
        """
        total_chars = 0
        for msg in messages:
            content = msg.get("content") or ""
            total_chars += len(content)

            if "tool_calls" in msg and msg["tool_calls"]:
                total_chars += len(json.dumps(msg["tool_calls"], ensure_ascii=False))

        return total_chars

    @staticmethod
    def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
        """
        【粗粒度 Token 快速估算法】

        【Why 为什么不直接用 tiktoken？】
        1. 避免对非官方 tokenizer 库的重依赖与 C 扩展在不同操作系统下的构建坑。
        2. 中英文混排场景下，中文约 1.5~2 字符/token，英文约 3~4 字符/token。
           经验公式：字符串总长度 / 2 可以在极低开销下（O(N) 字符串遍历）提供偏保守的安全边界。
        """
        return ContextManager._estimate_chars(messages) // 2

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
        # 以【字符总量】为累计口径，只在与预算比较的瞬间取整一次。
        # (current_chars + msg_chars) // 2 与 estimate_tokens(system + retained + msg)
        # 完全等价，从而保证裁剪结果严格不超 max_tokens。
        current_chars = self._estimate_chars([system_msg]) if system_msg else 0

        for msg in reversed(chat_history):
            msg_chars = self._estimate_chars([msg])
            if (current_chars + msg_chars) // 2 > self.max_tokens:
                break
            retained.insert(0, msg)
            current_chars += msg_chars

        # 核心防错：若首条消息是孤立的 tool 响应，剔除它以保证协议完整
        while retained and retained[0].get("role") == "tool":
            retained.pop(0)

        if system_msg:
            return [system_msg] + retained
        return retained
