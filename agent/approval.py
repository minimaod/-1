"""
文件定位: mini_agent/agent/approval.py
功能: 维护 approval_id -> asyncio.Future 的内存池，支持 Web 异步挂起与唤醒
"""
import asyncio
from typing import Dict, Optional


class ApprovalManager:
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
approval_manager = ApprovalManager()