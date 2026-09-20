"""
文件定位: mini_agent/agent/session.py
功能: 会话生命周期池 —— 驱动三级缓存（L1 内存 -> L2 磁盘 -> L3 新建）管理持久化生命周期，
      并维护 approval_id -> asyncio.Future 的内存池以支持 HITL 的异步挂起与唤醒
"""
import asyncio
from pathlib import Path
from typing import Dict, Optional
from agent.core import AgentEngine
from agent.storage import SessionStorage
from config import settings
class SessionManager:
    """带 SQLite 持久化支持的会话池管理器"""

    def __init__(self, db_path: Optional[str | Path] = None):
        self._sessions: Dict[str, AgentEngine] = {}
        self._lock = asyncio.Lock()
        self.storage = SessionStorage(db_path=db_path or settings.DB_PATH)

    async def get_or_create(self, session_id: str) -> AgentEngine:
        """三级缓存状态机"""
        async with self._lock:
            # 1. L1 内存命中
            if session_id in self._sessions:
                return self._sessions[session_id]

            # 2. L2 磁盘冷恢复
            # 【Why 也要传 system_prompt】core 侧的注入条件是 `system_prompt and
            # not self.history`：正常恢复（历史非空）时它被守卫拦下，不会重复插入
            # 第二条 system 消息；而若该会话落盘时历史为空（load_session 返回 []），
            # 角色认知仍会被正确钉回首位。即「传了不会双写，不传则可能丢失」。
            persisted_messages = self.storage.load_session(session_id)
            if persisted_messages is not None:
                engine = AgentEngine(
                    history=persisted_messages,
                    system_prompt=settings.DEFAULT_SYSTEM_PROMPT,
                )
                self._sessions[session_id] = engine
                return engine

            # 3. L3 全新构建
            # 【System Pinning 的实际激活点】此处显式注入角色认知，使其成为
            # history[0]。此后无论对话多长，ContextManager.truncate_history 都会
            # 把 index=0 的 system 消息锁在视窗首位（agent/context.py:71），
            # 引擎不会再在长对话中「忘记自己是谁」。
            engine = AgentEngine(system_prompt=settings.DEFAULT_SYSTEM_PROMPT)
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


class ApprovalManager:
    """
    人工审批挂起池（HITL）

    【Why 与 SessionManager 同驻本模块】二者同属「会话生命周期」这一个职责范畴：
    会话池管理消息历史的生老病死，审批池管理单轮对话中被挂起协程的等待与唤醒
    （core 侧 create_approval 挂起，server 侧 resolve_approval 兑现）。合并后
    agent.session 即成为会话生命周期的唯一入口。

    【防错点】approval_manager 必须是全局唯一实例。若本类被复制出第二份定义
    （例如另建兼容模块），core 会在 A 实例的 Future 上永久等待、而 server 往
    B 实例里填结果 —— HTTP 返回 404、SSE 流挂死、服务端日志全干净，全程零异常。
    """

    def __init__(self):
        # 挂起字典：approval_id 映射到正在等待结果的 Future 对象
        self._pending: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def create_approval(self, approval_id: str) -> asyncio.Future:
        """创建一个挂起的承诺（Future），等待外部接口履约"""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        async with self._lock:
            self._pending[approval_id] = fut
        return fut

    async def resolve_approval(self, approval_id: str, action: str) -> bool:
        """
        兑现承诺：填入用户的决定（approve / reject），
        唤醒被挂起的生成器协程
        """
        async with self._lock:
            fut = self._pending.pop(approval_id, None)
            if fut and not fut.done():
                fut.set_result(action)
                return True
            return False


# 全局单例
session_manager = SessionManager()
approval_manager = ApprovalManager()