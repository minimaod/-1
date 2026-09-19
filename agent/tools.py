import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Set

from config import settings

# ==================== 1. 基础路径与沙箱设定 ====================
PROJECT_ROOT: Path = settings.BASE_DIR
WORKSPACE_DIR: Path = settings.WORKSPACE_DIR

# 凭证与运行态数据的拒绝清单。
# 【Why 光有"不得越出项目根"不够】read_file 的判界基准是【整个项目根】
# (restrict_to_workspace=False)，而 .env 就在根目录下、data/ 里是完整会话历史、
# .git/ 可能含远端凭证 —— 它们本来就在边界之内，越界检查天然拦不住。
# 若不显式拒绝，模型只要调用 read_file('.env') 就能把明文 API Key 读进对话，
# 随后经 SSE 下发、并被 session_manager 落盘；而 read_file 并不在
# SENSITIVE_TOOLS 中，全程无需人工审批。
# 【匹配规则】按【路径分量】精确匹配，不做子串匹配 —— 否则 datastore/ 这类
# 仅名字含 "data" 的合法目录会被误伤。
DENIED_DIR_NAMES: Set[str] = {".git", "data"}


def _is_denied_secret(resolved_path: Path) -> bool:
    """判断项目内某路径是否命中凭证 / 运行态数据拒绝清单。"""
    try:
        rel = resolved_path.relative_to(PROJECT_ROOT)
    except ValueError:
        return False  # 不在项目根之内，交由边界判断处理
    if rel.name == ".env" or rel.name.startswith(".env."):
        return True  # 覆盖 .env / .env.local / .env.production 等变体
    return any(part in DENIED_DIR_NAMES for part in rel.parts)


def _validate_path(filepath: str, restrict_to_workspace: bool = True) -> Path:
    """路径安全校验：利用 resolve() 防御 Path Traversal 穿越攻击"""
    base_dir = WORKSPACE_DIR if restrict_to_workspace else PROJECT_ROOT
    target_path = Path(filepath)

    resolved_path = (
        (base_dir / target_path).resolve()
        if not target_path.is_absolute()
        else target_path.resolve()
    )

    if not resolved_path.is_relative_to(base_dir):
        raise PermissionError(
            f"安全违规：路径 '{filepath}' 超出安全边界 [{base_dir}]！"
        )

    # 【第二道闸门】只作用于"放宽到全项目读取"这一模式：沙箱内写入不受影响
    # （沙箱里出现名为 .env 的文件只是诱饵，不构成凭证泄露）。
    if not restrict_to_workspace and _is_denied_secret(resolved_path):
        raise PermissionError(
            f"安全违规：路径 '{filepath}' 属于凭证或运行态数据，禁止读取！"
        )

    return resolved_path


# ==================== 2. 本地真实可执行函数 ====================
def get_current_time(timezone: str = "local") -> str:
    """获取当前系统时间"""
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


def read_file(filepath: str) -> str:
    """安全读取项目源码或沙箱文本"""
    try:
        target = _validate_path(filepath, restrict_to_workspace=False)
        if not target.exists():
            return f"Error: 文件 '{filepath}' 不存在。"
        if not target.is_file():
            return f"Error: '{filepath}' 不是常规文件。"
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: 文件 '{filepath}' 不是合法的 UTF-8 文本。"
    except Exception as e:
        return f"Error: 读取失败 - {str(e)}"


def write_file(filepath: str, content: str) -> str:
    """向沙箱目录写入文件"""
    try:
        target = _validate_path(filepath, restrict_to_workspace=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Success: 已成功写入文件 '{target.name}'，共 {len(content)} 字符。"
    except Exception as e:
        return f"Error: 写入失败 - {str(e)}"


# ==================== 3. 路由分发字典 (调度执行层) ====================
TOOL_REGISTRY: Dict[str, Callable[..., Any]] = {
    "get_current_time": get_current_time,
    "read_file": read_file,
    "write_file": write_file,
}

# ==================== 4. API 契约协议 (模型理解层) ====================
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取操作系统当前的精确本地时间。当用户询问时间、日期时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "时区名称，默认为 'local'",
                        "enum": ["local"],
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取本地文本文件内容。支持读取工程目录内的代码或沙箱内的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件相对路径，如 'agent/tools.py' 或 'workspace/notes.txt'",
                    }
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "向沙箱目录 (workspace/) 写入文本。禁止写入沙箱之外的目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "要写入的文件相对路径，如 'notes.txt'",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文本内容",
                    },
                },
                "required": ["filepath", "content"],
            },
        },
    },
]
