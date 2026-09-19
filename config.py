"""
【模块职责导读】
统一配置中心（Central Configuration Module）
- 职责范围：全系统的环境感知、模型接入参数、沙箱隔离边界、服务监听配置的唯一定义与读取。
- 设计模式：基于单例模式导出全局 `settings` 实例，提供统一的强类型访问入口。
- 遵循准则：符合 12-Factor App 第 3 条（Config in Environment），实现业务逻辑与环境配置彻底解耦。
"""

import os
from pathlib import Path
from typing import Set
from dotenv import load_dotenv

# 确保在读取任何配置前加载 .env
load_dotenv()


class Settings:
    """
    系统全局配置类
    
    【Why 为什么要用类型标注与属性封装？】
    1. 避免各模块内部反复出现 `os.getenv("XXX", "default")` 导致的默认值不一致（DRY 原则）。
    2. 提供显式 Type Hints，在 IDE 编码阶段享受自动补全，杜绝手写拼写错误。
    """

    def __init__(self) -> None:
        # ==================== 项目路径与沙箱 ====================
        self.BASE_DIR: Path = Path(__file__).resolve().parent
        self.WORKSPACE_DIR: Path = self.BASE_DIR / "workspace"
        self.DATA_DIR: Path = self.BASE_DIR / "data"
        self.DB_PATH: Path = self.DATA_DIR / "local_agent.db"

        # 确保存储与沙箱目录存在
        self.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)

        # ==================== LLM 基础配置 ====================
        self.DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
        self.DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "deepseek-chat")

        # ==================== 上下文与工程安全限制 ====================
        # 滑动窗口最大 Token 预算（超出时将触发上下文裁剪）
        self.MAX_CONTEXT_TOKENS: int = int(os.getenv("MAX_CONTEXT_TOKENS", "8000"))
        # 请求 LLM 的超时时间（秒）
        self.REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "60.0"))

        # ==================== 人机协同（HITL）高危拦截规则 ====================
        # 命中此集合的工具，必须向前端下发 approval_required 挂起等待人工确认
        self.SENSITIVE_TOOLS: Set[str] = {"write_file", "create_file"}

        # ==================== 服务端配置 ====================
        self.SERVER_HOST: str = os.getenv("SERVER_HOST", "127.0.0.1")
        self.SERVER_PORT: int = int(os.getenv("SERVER_PORT", "8000"))

    def validate(self) -> None:
        """
        【防错点（Caveat）：Fail-Fast 启动前置校验】
        服务启动时强制调用此方法。若缺失关键凭证（如 API_KEY），第一时间抛出异常阻断启动，
        防止将错误拖延到运行时首个用户发消息时才崩溃。
        """
        if not self.DEEPSEEK_API_KEY:
            raise ValueError(
                "[Config Error] 未检测到 DEEPSEEK_API_KEY 环境变量！"
                "请检查根目录 .env 文件是否已正确配置该密钥。"
            )


# 全局单例
settings = Settings()