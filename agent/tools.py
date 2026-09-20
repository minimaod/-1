import asyncio
import datetime
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from config import settings

# Git diff 的物理截断阈值，杜绝 Git diff 垃圾字符撑爆上下文窗口。
# 消费方：get_git_diff —— 超长 diff 在此物理切片后追加截断警示。
MAX_DIFF_CHARS: int = 4000

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


# ==================== 2. 底层执行原语：Git 子进程 ====================


def _run_git_command(
    args: list[str], cwd: Path, timeout: float = 15.0
) -> tuple[int, str, str]:
    """
    【设计目的（Why）】
    封装底层的同步子进程执行，作为 asyncio.to_thread 的线程执行载体。

    【系统防错点（Caveats）】
    1. 强制 shell=False，传参必须为 list，物理根绝 Shell 注入；
    2. 显式设置 timeout 防止子进程僵死耗尽线程池；
    3. 使用 try-except 捕获 FileNotFoundError（系统未装 git）与 subprocess.TimeoutExpired。

    【为什么要做成「返回元组」而不是「抛异常」】
    本函数将被 asyncio.to_thread 投递到工作线程执行。异常若穿透线程边界，会
    经 Future 重抛进事件循环，把一次「环境缺 git」这种可预期的失败，升级成
    整轮对话的崩溃 —— 而模型完全有能力根据错误文案自行调整策略。故此处把
    非典型失败收敛为稳定的三态返回，与 TOOL_REGISTRY 各工具「返回 Error 字符串
    而不抛出」的既有约定保持一致。

    【返回码约定】0 = 成功；124 = 超时（对齐 GNU coreutils timeout 的惯例）；
    127 = 命令未找到（对齐 POSIX shell 的惯例）。非 0 时调用方应优先读 stderr。

    【已知边界（Caveat）】FileNotFoundError 有两处来源：可执行文件不存在，
    或 cwd 目录不存在（Windows 报 WinError 2 的同型错误）。此处统一归为 127，
    文案以「未安装 git」为主因。当前唯一调用方传入的是 PROJECT_ROOT 这一
    必然存在的目录，故该歧义暂不构成实际风险；若将来 cwd 可能来自外部输入，
    需先独立校验其存在性再归因。
    """
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        # capture_output=True 保证两个管道均已建立，故 stdout/stderr 必为 str
        # （不会是 None），此处可安全对齐 tuple[int, str, str] 契约。
        # errors="replace" 则为防御 Git 输出中的非 UTF-8 字节（如 GBK 编码的
        # 中文文件名）：宁可出现替换字符，也不让一次解码失败炸掉整个调用。
        return (result.returncode, result.stdout, result.stderr)
    except FileNotFoundError:
        return (127, "", "系统未安装 git 命令或未加入 PATH 环境变量")
    except subprocess.TimeoutExpired:
        return (124, "", f"Git 命令执行超时（超过 {timeout} 秒）")


# git 在「仓库尚无任何提交」时对 HEAD 的报错特征串（小写比对）。
# 【Why 要按文案识别而不是按返回码】该情形的返回码与「真·git 失败」同为 128，
# 仅凭 returncode 无法区分「空仓库需降级」与「路径不存在应报错」。文案是此处
# 唯一可用的判别信号，故收敛为常量集中维护，避免降级条件散落在调用点。
_MISSING_HEAD_MARKERS: tuple[str, ...] = (
    "unknown revision",
    "ambiguous argument",
    "bad revision",
    "does not have any commits yet",
)


def _is_missing_head_error(stderr: str) -> bool:
    """判断 git 的报错是否源于「HEAD 尚不存在」（全新初始化、零提交的仓库）。"""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _MISSING_HEAD_MARKERS)


# ==================== 3. 本地真实可执行函数 ====================
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


# ---- Git 诊断工具（异步：经 asyncio.to_thread 卸载阻塞子进程）----
#
# 【Why 这两个是 async，而上面三个是 sync】
# get_git_status / get_git_diff 的底层是 subprocess.run，属【阻塞型系统调用】，
# 单次最长可占用 timeout（15s）。若做成本模块其余工具那样的同步函数，它会在
# 事件循环线程上直接执行 —— SSE 推流、其他会话的 ReAct 循环、审批 Future 的
# 唤醒全部被冻结 15 秒，且冻结期间没有任何异常或日志，表现为"服务端假死"。
# 故此处必须 async + asyncio.to_thread，把阻塞面挪进默认线程池。
#
# 【登记前置条件（Caveat）：注册进 TOOL_REGISTRY 前必须先改 core.py】
# agent/core.py 目前以【同步】方式派发 executor（tool_output = str(executor(**args))）。
# 同步调用 async 函数拿到的是协程对象而非结果，str() 会把它变成
# "<coroutine object get_git_status at 0x...>" 回写进 history —— 模型收到一串
# 内存地址，同时抛 "coroutine was never awaited"。这是无异常、无报错的静默失效。
# 故 Step 4 注册这两个工具时，必须同步把 core.py 派发点改为按
# inspect.iscoroutinefunction(executor) 分流并 await。


async def get_git_status() -> str:
    """
    【设计目的（Why）】
    获取当前代码仓库工作区与暂存区的文件修改状态。

    【系统防错点（Caveats）】
    1. 必须使用 asyncio.to_thread 卸载阻塞的子进程执行；
    2. 必须以 PROJECT_ROOT 为工作目录；
    3. 命令失败（如非 git 仓库）需返回错误描述，严禁抛异常击穿上层调度。

    【Why 用 --porcelain 而不用人类可读的 git status】
    1. 上下文消耗：人类版输出含分支提示、换行提示（"use git add..."）、
       着色与对齐填充，同样信息量下体积可达 porcelain 的数倍，且这些
       提示语对模型判断"有哪些改动"零信息增益，纯属挤占 Token 预算。
    2. 输出确定性：人类版会随 locale 变化（中文 Git 输出"尚未暂存以备提交
       的变更"），同一份仓库状态在不同语言环境下产生不同文本 —— 模型的
       解析与后续判据会被迫依赖语言，而 porcelain 是 Git 承诺的**稳定机器
       接口**（格式跨版本、跨 locale 不变），是唯一可作契约的形态。
    """
    returncode, stdout, stderr = await asyncio.to_thread(
        _run_git_command, ["git", "status", "--porcelain"], PROJECT_ROOT
    )

    if returncode != 0:
        return f"Git 状态获取失败: {stderr.strip()}"

    # 【Why 空输出必须翻译成一句话】空字符串对模型是一个语义空洞：它可能
    # 被理解为"命令没跑成"、"工具坏了"或"输出丢了"，进而诱发编造改动或
    # 反复重试。显式回执把"确实没有变更"这一事实钉死，消灭幻觉空间。
    # 判空用 strip()（任何空白都算空）是正确的，但【绝不能】用它产出返回值：
    if not stdout.strip():
        return "Working tree clean (工作区干净，无任何变更)"

    # 【必须 rstrip 而非 strip：首行首列的空格是语义字符】
    # porcelain 每行形如 "XY PATH"，X 是暂存区状态、Y 是工作区状态：
    #     "M  alpha.txt"  -> 已暂存修改（X=M）
    #     " M alpha.txt"  -> 仅工作区修改（Y=M，首列是空格）
    # 若此处用 strip()，当【首条】记录是"仅工作区修改"时，那个前导空格会被
    # 吃掉，" M alpha.txt" 变成 "M alpha.txt" —— 状态码从"未暂存"静默篡改为
    # "已暂存"，模型会据此误判改动是否已经 git add，进而调错后续命令（例如
    # 以为无需再 add，或反过来说"你还没暂存"）。rstrip 只去尾部换行，保住首列。
    return stdout.rstrip()


async def get_git_diff(filepath: Optional[str] = None) -> str:
    """
    【设计目的（Why）】
    获取特定文件或全局工作区的代码 diff，供审查代码与定位改动。

    【系统防错点（Caveats）】
    1. 路径必须经由 _validate_path(..., restrict_to_workspace=False) 校验，
       杜绝跨目录穿越与敏感凭证（如 .env）泄露；
    2. 传参必须携带 "--"，防止文件名注入为 git 参数；
    3. 必须实施 MAX_DIFF_CHARS 物理截断防御，防止上下文爆炸；
    4. 基准取 HEAD（含暂存区），并对「零提交的新仓库」降级容错。

    【Why 校验路径必须走 _validate_path，而不能裸调 _assert_path_safe】
    diff 是只读操作，容易被误认为"无需凭证防护"——恰恰相反，它是本项目里
    最危险的读取面：get_git_diff(".env") 会把密钥文件的内容以 diff 形式直接
    注入模型上下文，再经 SSE 下发并被落盘。而 .env / data/ / .git/ 全都位于
    PROJECT_ROOT 之内，纯粹的分量判界天然拦不住它们。_validate_path 在委托
    拓扑判界之外还挂着凭证拒绝清单，这层业务闸门才是拦住 .env 的那一道。
    """
    # 【Why 基准是 HEAD 而不是裸 git diff】
    # 裸 `git diff` 只比对「暂存区 vs 工作区」，因此【已 git add 的文件会消失】。
    # 而开发者的实际习惯恰恰是先 add 再让 Agent 审查，于是会出现这条自相矛盾的
    # 链：模型从 get_git_status 读到 "M  x.py"（首列为 M 即已暂存）→ 调本工具
    # → 收到「未检测到任何代码变更」。状态回执与 diff 回执直接打架，正是诱发
    # 模型编造 diff 的土壤。改比对 HEAD 后，工作区与暂存区的改动一并可见。
    cmd = ["git", "diff", "HEAD"]
    rel_path: Optional[str] = None

    if filepath is not None:
        try:
            # 判界基准是整个项目根（评审对象是工程源码，不限于沙箱）
            safe_path = _validate_path(filepath, restrict_to_workspace=False)
        except PermissionError as e:
            # 与 read_file / write_file 的既有约定一致：安全拒绝以 Error 字符串
            # 回执，让模型能读到原因并自行调整，而不是把异常抛穿调度层。
            return f"Error: {e}"

        # 【Why as_posix() 而不是 str()】
        # Windows 上 str(相对路径) 得到 "agent\tools.py"（反斜杠）。Git 的
        # pathspec 语法把反斜杠当作【转义字符】（如 "\*" 转义通配符），故
        # 反斜杠路径存在被解析成转义序列的风险；正斜杠在 Windows 版 Git 上
        # 同样被接受且无转义歧义，故统一规范化为 POSIX 形式再交给 Git。
        rel_path = safe_path.relative_to(PROJECT_ROOT).as_posix()

        # 【Why 必须有 --】没有它，一个名为 "--staged" 的文件会被 Git 的选项
        # 解析器吃掉：命令语义从「diff 这个文件」静默偏移为「显示已暂存变更」，
        # 且 rc=0、输出为空 —— 模型据此判断「没有改动」，而文件其实被改了。
        cmd.extend(["--", rel_path])

    returncode, stdout, stderr = await asyncio.to_thread(
        _run_git_command, cmd, PROJECT_ROOT
    )

    # 【空仓库降级】全新 git init 且零提交时，HEAD 尚未指向任何对象，
    # `git diff HEAD` 会以 rc=128 报 "fatal: ambiguous argument 'HEAD': unknown
    # revision..."。这是「仓库尚未诞生」这一正常状态，不是错误，故降级为裸
    # `git diff` 重试一次，而不是把 fatal 文案甩给模型。
    # 【残留边界】裸 git diff 不覆盖暂存区，故空仓库中「已 add 但无提交」的
    # 文件仍可能返回空回执。此时 get_git_status 会报出 "A  x.py"，模型可据此
    # 推断改动存在 —— 属可接受的降级，不为此引入 --cached 并集。
    if returncode != 0 and _is_missing_head_error(stderr):
        cmd = ["git", "diff"]
        if rel_path is not None:
            cmd.extend(["--", rel_path])
        returncode, stdout, stderr = await asyncio.to_thread(
            _run_git_command, cmd, PROJECT_ROOT
        )

    if returncode != 0:
        return f"Git diff 获取失败: {stderr.strip()}"

    diff = stdout.strip()
    if not diff:
        return "No changes detected (未检测到任何代码变更)"

    # 【物理截断】先切片再加警示，保证返回体总长可控（警示语本身很短，
    # 但仍严格计入总预算；此处以"前 MAX_DIFF_CHARS 字符"为承诺口径）。
    if len(diff) > MAX_DIFF_CHARS:
        return (
            diff[:MAX_DIFF_CHARS]
            + f"\n\n[Warning: Diff 结果过长，已截断前 {MAX_DIFF_CHARS} 字符，请分文件查看]"
        )

    return diff


# ==================== 4. 路由分发字典 (调度执行层) ====================
TOOL_REGISTRY: Dict[str, Callable[..., Any]] = {
    "get_current_time": get_current_time,
    "read_file": read_file,
    "write_file": write_file,
    "get_git_status": get_git_status,
    "get_git_diff": get_git_diff,
}

# ==================== 5. API 契约协议 (模型理解层) ====================
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
    {
        "type": "function",
        "function": {
            "name": "get_git_status",
            "description": (
                "获取当前 Git 仓库工作区与暂存区的文件修改状态（porcelain 格式，"
                "每行形如 'M  agent/core.py'：首列为暂存区状态、次列为工作区状态，"
                "'??' 表示未跟踪文件）。"
                "当用户询问『有哪些改动 / 改了哪些文件』，或需要在审查前先确定变更"
                "范围时调用本工具。"
                "本工具无参数。若返回 'Working tree clean' 则表示确实没有任何变更。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_git_diff",
            "description": (
                "获取代码变更的 diff。基准是最后一次提交（HEAD），"
                "因此【同时覆盖未暂存的工作区改动与已 git add 的暂存改动】。"
                "审查代码改动前必须先调用本工具取得真实 diff，严禁凭空臆测或编造 Diff。"
                "filepath 参数为【相对项目根目录】的路径，必须使用正斜杠"
                "（如 'agent/core.py'、'config.py'）；"
                "严禁传入绝对路径（如 'C:/...' 或 '/etc/...'）；"
                "严禁传入 '.env'、'data/'、'.git/' 等敏感路径 —— 此类请求会被"
                "安全策略直接拒绝，不要反复重试。"
                "省略 filepath 则返回全量变更的 diff。"
                "返回内容超过 4000 字符时会被截断并附带 '[Warning: ...]' 提示，"
                "此时应改为逐个文件查看以获取完整内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": (
                            "可选。相对项目根目录的文件路径，使用正斜杠分隔，"
                            "例如 'agent/tools.py'。省略则以全量模式返回所有变更。"
                        ),
                    }
                },
                # 【必须为空列表】filepath 是可选参数，不得出现在 required 中。
                "required": [],
            },
        },
    },
]
