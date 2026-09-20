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
    """
    【业务安全与凭证网关】路径安全校验的统一入口。

    【分层定位（Why 拆成两层）】
    本函数**不做**路径拓扑运算，只负责三件业务决策，物理判界一律委托
    _assert_path_safe：
      1. 路由 base_dir：沙箱模式取 WORKSPACE_DIR，放宽模式取 PROJECT_ROOT；
      2. 把原语抛出的 ValueError 翻译为本函数对外的 PermissionError 契约；
      3. 追加【凭证与运行态数据拒绝清单】这道业务闸门。
    如此可保证全系统只有一份穿越漏洞的实现，修一处即全体生效。

    【异常契约为什么必须翻译】_assert_path_safe 的契约是 ValueError（参数非法），
    而本函数的契约是 PermissionError（沙箱授权失败）—— test_traversal.py 的
    不变式电池按后者断言，非 PermissionError 一律判失败。分层语义也恰好正确：
    ValueError 描述"这个参数不合法"，PermissionError 描述"这次访问不被授权"。
    """
    base_dir = WORKSPACE_DIR if restrict_to_workspace else PROJECT_ROOT

    try:
        resolved_path = _assert_path_safe(filepath, base_dir)
    except ValueError as exc:
        raise PermissionError(
            f"安全违规：路径 '{filepath}' 超出安全边界 [{base_dir}]！"
        ) from exc

    # 【第二道闸门】只作用于"放宽到全项目读取"这一模式：沙箱内写入不受影响
    # （沙箱里出现名为 .env 的文件只是诱饵，不构成凭证泄露）。
    if not restrict_to_workspace and _is_denied_secret(resolved_path):
        raise PermissionError(
            f"安全违规：路径 '{filepath}' 属于凭证或运行态数据，禁止读取！"
        )

    return resolved_path


def _assert_path_safe(file_path_str: str, base_dir: Path) -> Path:
    """
    【设计目的（Why）】
    防范路径穿越攻击（Path Traversal），确保目标路径严格约束在合法作用域内。

    【系统防错点（Caveats）】
    1. 必须使用 .resolve() 处理相对路径符号（..）与软链接；
    2. 必须使用 Path.is_relative_to() 进行路径分量匹配，严禁使用字符串 startswith；
    3. 注意处理绝对路径输入导致的基准目录脱逸风险。

    ── Caveat 3 的机制：`/` 运算符的静默脱锚 ──
    pathlib 的 `/` 不是字符串拼接，而是 join 语义：当右操作数【带根】时，
    它会丢弃左操作数。实测：
        PurePosixPath("/home/project")  / "/etc/passwd"  ->  /etc/passwd
        PureWindowsPath("D:/proj")      / "/etc/passwd"  ->  D:\\etc\\passwd
    注意 Windows 这一例：ntpath 的语义是「保留盘符、替换根」，base_dir 并非
    整体消失，而是被替换成同盘根目录 —— 这种「半脱锚」比 POSIX 的全脱锚更
    隐蔽，因为它看起来仍像在本项目所在盘内。更反直觉的是，Windows 上
    PureWindowsPath("/etc/passwd").is_absolute() 为 **False**（无盘符的
    rooted 路径不算绝对路径），因此「只对绝对路径做特殊处理」的三元式写法
    会走进 join 分支，得到 D:\\etc\\passwd。

    ── 因此本函数不把安全性托付给 is_absolute() 的分支判断 ──
    三元式只是与 _validate_path 保持写法一致的可读性护栏；真正承重的是
    resolve() 之后的 is_relative_to 判界。上面三种脱锚形态最终都会落在
    base_dir 之外而被同一处断言拦住，故分支判断写错与否都不构成漏洞。

    ── 为什么判界必须在 resolve() 【之后】（顺序不可交换）──
    resolve() 只做规范化（消解 .. 与跟随软链接），它【不会】把已丢弃的锚点
    还原。所以「先查前缀、再 resolve」是无效防守：沙箱内一个指向项目外的
    软链接，其字面路径完全位于 base_dir 之内，唯有 resolve() 跟随链接之后
    才能发现真实落点在外面（见 test_traversal.py 的符号链接逃逸用例）。
    另：resolve() 默认 strict=False，故不存在的路径（例如 diff 一个已删除
    的文件）也能安全规范化并判界，不会因 FileNotFoundError 而中断诊断。

    ── 为什么用 is_relative_to 而不是 startswith(STR(base_dir)) ──
    startswith 是字符级比较，会放行前缀同名的兄弟目录：base_dir 为
    .../workspace 时，.../workspace_evil 因共享前缀被误判为「在内」。
    is_relative_to 是路径【分量】级比较，workspace_evil != workspace。
    """
    # 基准目录自身也必须 resolve：否则 base_dir 若含软链接（典型如 macOS 的
    # /tmp -> /private/tmp），已 resolve 的落点与未 resolve 的基准将永远不可比。
    base = base_dir.resolve()
    target_path = Path(file_path_str)

    resolved_path = (
        (base / target_path).resolve()
        if not target_path.is_absolute()
        else target_path.resolve()
    )

    # 【唯一承重断言】按【真实落点】判界，而非按输入写法判界。
    if not resolved_path.is_relative_to(base):
        raise ValueError(f"越权访问拦截: 路径 {file_path_str} 超出合法范围")

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
