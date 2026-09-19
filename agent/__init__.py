"""
文件定位: mini_agent/agent/__init__.py
功能: 包级导出声明 —— 统一 agent 包的对外入口，收敛为 9 个符号。

【Why 采用 PEP 562 惰性导出，而不是常规的模块级 `from agent.x import Y`】
本文件的导出全部推迟到「真正被访问的那一刻」才解析，因此文件内**不存在任何**
`import agent.*` / `from agent.* import ...` 语句。若改回常规模块级导入：

1. `import agent` 会从近乎零成本膨胀为拉起整条运行期栈（core → openai，
   events → pydantic）。包导入不该有这种代价。
2. 更隐蔽的是：`session_manager` / `approval_manager` 位于 agent.session，
   而该模块在被导入时会实例化 SessionManager → SessionStorage → mkdir +
   CREATE TABLE。只要有人把 session 的东西写进这里，**任何** `import agent.x`
   都会静默建库 —— 不报错、不警告，只是每次导入写一次磁盘。惰性化让这种退化
   在结构上无法发生。

【导出面的取舍】只导出「外部真正需要」的两类符号：运行入口与类型契约。
- 工具的具体实现函数（read_file / write_file / get_current_time）不在此列，
  外部应经 TOOL_REGISTRY 分发，避免绕过注册表直接调用。
- SessionStorage 不在此列，它是 SessionManager 的内部协作者（持久层实现细节），
  需要时按 `from agent.storage import SessionStorage` 显式引用，以表明依赖意图。
"""

from importlib import import_module
from typing import Any, Dict, List, Tuple

__all__: List[str] = [
    # 运行入口
    "AgentEngine",
    "ContextManager",
    "SessionManager",
    "ApprovalManager",
    # 类型契约
    "AgentEvent",
    "TOOL_REGISTRY",
    "TOOLS_SCHEMA",
    # 全局单例
    "session_manager",
    "approval_manager",
]

# 导出名 -> (子模块路径, 模块内属性名)
_LAZY_EXPORTS: Dict[str, Tuple[str, str]] = {
    "AgentEngine": ("agent.core", "AgentEngine"),
    "ContextManager": ("agent.context", "ContextManager"),
    "SessionManager": ("agent.session", "SessionManager"),
    "ApprovalManager": ("agent.session", "ApprovalManager"),
    "AgentEvent": ("agent.events", "AgentEvent"),
    "TOOL_REGISTRY": ("agent.tools", "TOOL_REGISTRY"),
    "TOOLS_SCHEMA": ("agent.tools", "TOOLS_SCHEMA"),
    "session_manager": ("agent.session", "session_manager"),
    "approval_manager": ("agent.session", "approval_manager"),
}


def __getattr__(name: str) -> Any:
    """
    PEP 562 模块级 __getattr__：首次访问导出名时才真正解析对应子模块。

    【防错点】未知名字必须 raise AttributeError，不能返回 None 或抛别的异常 ——
    这是 `hasattr()`、`from agent import X` 的报错语义、以及 IDE 补全能够正确
    工作的前提。
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, attr_name = target
    value = getattr(import_module(module_path), attr_name)
    # 解析一次后写回模块字典：后续访问走常规属性查找，__getattr__ 不再被触发
    globals()[name] = value
    return value


def __dir__() -> List[str]:
    """让 dir(agent) 与 IDE 补全能看见惰性导出名。"""
    return sorted(__all__)
