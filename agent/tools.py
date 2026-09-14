import datetime
from pathlib import Path
from typing import Any, Callable, Dict

# ==================== 1. 基础路径与沙箱设定 ====================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = (PROJECT_ROOT / "workspace").resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


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
