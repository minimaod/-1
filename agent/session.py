"""
文件定位: mini_agent/agent/session.py
功能: 驱动三级缓存（L1 内存 -> L2 磁盘 -> L3 新建）并管理持久化生命周期
"""
import asyncio
from typing import Dict
from agent.core import AgentEngine
from agent.storage import SessionStorage

class SessionManager:
    """带 SQLite 持久化支持的会话池管理器"""

    def __init__(self, db_path: str = "data/local_agent.db"):
        self._sessions: Dict[str, AgentEngine] = {}
        self._lock = asyncio.Lock()
        self.storage = SessionStorage(db_path=db_path)

    async def get_or_create(self, session_id: str) -> AgentEngine:
        """三级缓存状态机"""
        async with self._lock:
            # 1. L1 内存命中
            if session_id in self._sessions:
                return self._sessions[session_id]

            # 2. L2 磁盘冷恢复
            persisted_messages = self.storage.load_session(session_id)
            if persisted_messages is not None:
                engine = AgentEngine(history=persisted_messages)
                self._sessions[session_id] = engine
                return engine

            # 3. L3 全新构建
            engine = AgentEngine()
            self._sessions[session_id] = engine
            return engine

    async def save_session(self, session_id: str) -> None:
        """将当前会话历史安全落盘"""
        async with self._lock:
            engine = self._sessions.get(session_id)
            if engine:
                self.storage.save_session(session_id, engine.history)

    async def clear_session(self, session_id: str) -> bool:
        """清空会话缓存"""
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def get_active_count(self) -> int:
        return len(self._sessions)

session_manager = SessionManager()