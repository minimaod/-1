"""
文件定位: mini_agent/agent/session.py
功能: 管理 session_id -> AgentEngine 的映射与生命周期
"""
import asyncio
from typing import Dict
from agent.core import AgentEngine


class SessionManager:
    """内存会话池管理器"""
    def __init__(self):
        # 核心隔离字典：每个 session_id 独占一个 AgentEngine 实例
        self._sessions: Dict[str, AgentEngine] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, session_id: str) -> AgentEngine:
        """
        根据 session_id 获取对应会话，不存在则初始化。
        加锁避免高并发下同一 session_id 重复实例化竞争。
        """
        async with self._lock:
            if session_id not in self._sessions:
                # 独立实例化，确保每个用户拥有独立的 messages 数组与状态机
                self._sessions[session_id] = AgentEngine()
            return self._sessions[session_id]

    async def clear_session(self, session_id: str) -> bool:
        """手动清空指定会话的上下文"""
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def get_active_count(self) -> int:
        """获取当前活跃会话数"""
        return len(self._sessions)


# 导出全局管理器单例
session_manager = SessionManager()