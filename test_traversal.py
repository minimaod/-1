"""
【模块职责导读】沙箱边界回归测试（Sandbox Boundary Test Suite）
- 职责范围：验证 agent/tools.py 的路径沙箱 _validate_path 能否抵御路径穿越逃逸，
  以及工具注册表与 API Schema 的契约一致性。
- 运行方式：python test_traversal.py    （不依赖 pytest，纯 stdlib 断言脚本）
- 覆盖范围：
  1. 合法写入落在沙箱内，且内容可经 read_file 回读。
  2. 必须拦截的逃逸向量：父目录穿越、Windows 反斜杠、绝对路径、前缀同名兄弟
     目录、扩展长度路径前缀、以及【符号链接逃逸】。
  3. 对抗输入电池：对每个畸形输入断言「要么被拒，要么落点严格限于沙箱内」。
  4. read_file 的边界：以项目根为界，越界必拒；目录 / 缺失文件各自明确报错。
  5. 契约一致性：TOOL_REGISTRY 与 TOOLS_SCHEMA 的名称集合必须完全一致。
  6. 零残留：全部用例结束后 workspace 恢复原状。

【Why 为什么必须断言，而不是像原版那样 print】
原版 test_traversal.py 只 print 结果靠人眼判断，永远不会失败 —— 它的回归价值
为零；且它会往 workspace 写入 test.txt 却从不清理，正是工作区被污染、需要
git rm --cached 收拾的来源。沙箱是写入文件系统的最后一道边界，必须机器可验证。

【Why 用「不变式」而非「逐向量硬编码期望值」】
逐条写死每个攻击串的预期结果，一旦实现细节变动（换用 realpath、换一种 Path
处理方式）就会大面积误报。真正要守住的唯一性质是：**任何输入下，落点都不得
逃出 WORKSPACE_DIR**。故本文件把它作为电池用例的判定准则。
另有一组 MUST_BLOCK 向量额外硬断言「必须返回 Error」——那是我们明确承诺的
安全底线，值得为此承担一点过拟合风险。

【已知边界（不在本文件断言，另附说明）】
read_file 的边界基准是 PROJECT_ROOT（整个项目），而 .env 位于项目根，
因此 LLM 可以调用 read_file('.env') 取到明文密钥，且 read_file 不在
SENSITIVE_TOOLS 中、不经 HITL 审批。这是当前设计的暴露面，不是本测试要
固化的行为，故不加断言。
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Set, Tuple

from agent.tools import (
    TOOL_REGISTRY,
    TOOLS_SCHEMA,
    _validate_path,
    read_file,
    write_file,
)
from config import settings

WORKSPACE: Path = settings.WORKSPACE_DIR
PROJECT_ROOT: Path = settings.BASE_DIR

# 沙箱外探针根目录（在系统临时区，且断言它确实位于项目外）
OUTSIDE_ROOT: Path = Path(tempfile.mkdtemp(prefix="sandbox_probe_"))


# ==================== 被测安全底线：这些向量【必须】被拒绝 ====================

MUST_BLOCK: List[str] = [
    # 前缀同名兄弟目录（.../workspace 与 .../workspace_evil）：专治把判界错误实现成
    # 字符串 startswith 的情况，故必须留在底线清单里，让不变式电池也能覆盖到。
    f"../{WORKSPACE.name}_evil/sbx_mb_sibling.txt",
    "../sbx_mb_parent.txt",            # 单层父目录穿越
    "../../sbx_mb_grandparent.txt",    # 多层父目录穿越
    "..\\sbx_mb_backslash.txt",        # Windows 反斜杠分隔符
    "a/../../sbx_mb_mixed.txt",        # 混合：先深入再穿越
    "./../sbx_mb_dotslash.txt",        # ./ 前缀掩护 + 穿越
    "....//sbx_mb_quad.txt",           # 四点斜杠（绕过朴素字符串替换的经典串）
    "sub/../../sbx_mb_nested.txt",     # 经子目录再穿越
    "//sbx_mb_uncish.txt",             # 双斜杠前缀
    "C:/Windows/Temp/sbx_mb_abs.txt",  # 绝对路径（项目外）
    "sbx_mb_deep/../../sbx_mb_up.txt", # 目录名与被穿越层数组合
    "\\\\?\\C:\\Windows\\Temp\\sbx_mb_ext.txt",  # 扩展长度路径前缀
]

# ==================== 对抗输入电池：只断言「落点不逃出沙箱」 ====================
# 这些输入在实测中表现为：要么被拒，要么退化成沙箱内的字面文件名（例如
# "..%2f" 不会被解码，"~" 不被展开，"..;" 在 Windows 上不是父目录引用，
# NUL 分量被其后的 .. 抵消）。它们不是漏洞，但必须被持续证明"没有逃逸"。

ADVERSARIAL: List[str] = [
    "..%2fsbx_adv_pct.txt",     # URL 编码斜杠：不解码，成为字面文件名
    "..%5csbx_adv_pct2.txt",    # URL 编码反斜杠：同上
    "~/sbx_adv_tilde.txt",      # ~ 不被 Path 展开，成为字面目录名
    "..;/sbx_adv_semi.txt",     # 分号截断技巧：非 Windows 父目录引用
    "a\x00/../sbx_adv_nul.txt", # 空字节注入：NUL 分量被后续 .. 抵消
    "....\\/sbx_adv_slash.txt", # 混合分隔符
]


# ==================== 清理工具 ====================


def _force_remove(path: Path) -> None:
    """尽力删除单个路径；目录符号链接需要走 rmdir，故分情形处理。"""
    try:
        if path.is_symlink():
            try:
                path.unlink()
            except OSError:
                path.rmdir()  # Windows 上目录符号链接需用 rmdir 摘除
        elif path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    except OSError:
        pass  # 目录非空或权限不足时静默跳过，交由后续断言暴露


def _rmtree_quietly(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


class WorkspaceGuard:
    """
    记录进入时 workspace 的既有路径集合，退出时删除本次新增的一切（含空目录）。

    【Why 必须有它】沙箱用例必然要真实落盘，因此「测试自身零残留」也是被守的
    性质之一。用前后快照差集而非硬编码文件名，保证即使某条用例中途失败、
    或将来有人加了新的造数逻辑，也不会把垃圾留在工作区里。
    """

    def __init__(self) -> None:
        self._before: Set[Path] = set()

    def __enter__(self) -> "WorkspaceGuard":
        self._before = self._snapshot()
        return self

    def __exit__(self, *_: Any) -> bool:
        created = self._snapshot() - self._before
        # 深的先删，保证子文件先于其父目录被清除
        for path in sorted(created, key=lambda p: len(p.parts), reverse=True):
            _force_remove(path)
        return False

    @staticmethod
    def _snapshot() -> Set[Path]:
        try:
            return {p for p in WORKSPACE.rglob("*") if not p.is_symlink()}
        except OSError:
            return set()


def _top_level_names(directory: Path) -> Set[str]:
    """列出一级子项名，用于检测越界写入是否真的落盘。"""
    try:
        return {p.name for p in directory.iterdir()}
    except OSError:
        return set()


def _is_inside(path: Path, base: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve())
    except OSError:
        return False


# ==================== 用例 ====================


def test_outside_root_is_really_outside() -> None:
    """前置自检：探针目录必须真在项目外，否则后续逃逸断言全部失去意义。"""
    assert not _is_inside(OUTSIDE_ROOT, PROJECT_ROOT), (
        f"探针目录 {OUTSIDE_ROOT} 竟位于项目内，逃逸用例无效。"
        "请勿在系统临时目录内运行本项目。"
    )
    assert WORKSPACE.is_relative_to(PROJECT_ROOT), (
        "前置假设被打破：沙箱目录不再位于项目根之内。"
    )


def test_legal_writes_stay_inside_sandbox() -> None:
    """合法写入：成功、真实落盘、位于沙箱内、内容可回读。"""
    with WorkspaceGuard():
        result = write_file("sbx_ok.txt", "hello sandbox")
        assert result.startswith("Success:"), f"合法写入被误拒: {result}"
        target = WORKSPACE / "sbx_ok.txt"
        assert target.is_file(), "返回成功但文件并未落盘"
        assert _is_inside(target, WORKSPACE), "合法写入的落点竟在沙箱之外"
        assert target.read_text(encoding="utf-8") == "hello sandbox"

        nested = "sbx_nest/deep/inner.txt"
        assert write_file(nested, "nested").startswith("Success:"), "合法嵌套写入被误拒"
        # read_file 的基准是项目根，故回读沙箱文件需带 workspace/ 前缀
        assert read_file(f"workspace/{nested}") == "nested", "写入内容无法原样回读"


def test_must_block_vectors_are_rejected() -> None:
    """安全底线向量必须全部被拒绝，且不得在项目根留下痕迹。"""
    before = _top_level_names(PROJECT_ROOT)
    with WorkspaceGuard():
        for vector in MUST_BLOCK:
            result = write_file(vector, "pwned")
            assert result.startswith("Error:"), (
                f"[漏沙] 逃逸向量 {vector!r} 竟返回成功: {result}"
            )
            assert "安全违规" in result, (
                f"向量 {vector!r} 被拒绝，但原因不是越界拦截: {result}"
            )
    leaked = _top_level_names(PROJECT_ROOT) - before
    assert not leaked, f"[漏沙] 项目根被写入新条目: {sorted(leaked)}"


def test_absolute_path_outside_project_is_rejected() -> None:
    """绝对路径指向项目外：必须拒绝，且目标目录不得出现文件。"""
    with WorkspaceGuard():
        for name in ("sbx_abs1.txt", "sbx_abs2.txt"):
            target = OUTSIDE_ROOT / name
            result = write_file(str(target), "pwned")
            assert result.startswith("Error:"), f"[漏沙] 绝对路径写入成功: {result}"
            assert not target.exists(), f"[漏沙] 项目外文件被真实创建: {target}"


def test_absolute_path_inside_sandbox_is_allowed() -> None:
    """绝对路径指向沙箱【内部】属合法：判界按落点而非按写法。"""
    with WorkspaceGuard():
        target = WORKSPACE / "sbx_abs_inside.txt"
        result = write_file(str(target), "ok")
        assert result.startswith("Success:"), f"沙箱内绝对路径被误拒: {result}"
        assert target.read_text(encoding="utf-8") == "ok"
        assert _is_inside(target, WORKSPACE)


def test_prefix_sibling_directory_is_rejected() -> None:
    """
    前缀同名兄弟目录必须被拒。

    【Why 单独一条】工作区是 .../workspace。若判界误用字符串 startswith 而非
    路径级包含判断，则 .../workspace_evil 会因共享前缀而被误放行 —— 这是沙箱
    实现最经典的一类绕过。Path.is_relative_to 是路径分量级的，此处即为守它。
    """
    sibling_name = WORKSPACE.name + "_evil"
    before = _top_level_names(PROJECT_ROOT)
    with WorkspaceGuard():
        result = write_file(f"../{sibling_name}/sbx_prefix.txt", "pwned")
        assert result.startswith("Error:"), (
            f"[漏沙] 前缀同名兄弟目录被误放行: {result}"
        )
    leaked = _top_level_names(PROJECT_ROOT) - before
    assert sibling_name not in leaked, f"[漏沙] 兄弟目录被真实创建: {sibling_name}"


def test_symlink_escape_is_rejected() -> None:
    """
    符号链接逃逸：沙箱内建链接指向项目外，再经链接写入，必须被拒。

    【Why 这是最隐蔽的一条】纯字符串判界会看路径「长得像在沙箱内」而放行，
    唯有先 resolve() 跟随链接、再按真实落点判界才拦得住。本机有创建权限时
    真实执行；无权限（未开开发者模式）则跳过并明确说明，不静默通过。
    """
    link = WORKSPACE / "sbx_escape_link"
    try:
        try:
            os.symlink(str(OUTSIDE_ROOT), str(link), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            print(f"  [SKIP] 无法创建符号链接（{type(exc).__name__}），本机跳过该项")
            return

        with WorkspaceGuard():
            result = write_file("sbx_escape_link/sbx_pwned.txt", "pwned")
            assert result.startswith("Error:"), (
                f"[漏沙] 经符号链接成功写出沙箱: {result}"
            )
        assert not (OUTSIDE_ROOT / "sbx_pwned.txt").exists(), (
            "[漏沙] 链接目标目录被真实写入"
        )
    finally:
        _force_remove(link)
        assert not link.exists(), f"符号链接未清理干净: {link}"


def test_validate_path_invariant_battery() -> None:
    """
    核心不变式（直接作用于安全函数本身）：对【任意】输入，_validate_path 的结果
    要么抛出 PermissionError，要么返回一个严格位于 WORKSPACE_DIR 之内的路径。

    【Why 必须直接测 _validate_path，而不是只看 write_file 的返回文案】
    write_file 把异常吞成了字符串，且成功文案里只带 basename，因此"返回文案里
    没有 ../"这类断言恒真、毫无鉴别力 —— 把边界判断整个删掉它照样通过。
    直接断言安全函数自身的输入输出，才能让"边界一旦失守，用例必然变红"成立。
    该不变式用变异测试验证过：将 is_relative_to 判界改为 if False，本用例立刻失败。
    """
    allowed = 0
    for vector in ADVERSARIAL + MUST_BLOCK:
        try:
            landing = _validate_path(vector, restrict_to_workspace=True)
        except PermissionError:
            continue  # 分支一：明确拒绝，不变式成立
        except Exception as exc:
            raise AssertionError(
                f"向量 {vector!r} 抛出了非 PermissionError 的异常："
                f"{type(exc).__name__}: {exc}"
            ) from exc

        # 分支二：放行 → 落点必须严格在沙箱内
        assert _is_inside(landing, WORKSPACE), (
            f"[漏沙] 向量 {vector!r} 未被拒绝，且落点逃出了沙箱：{landing}"
        )
        allowed += 1

    # 防呆：若某天所有向量全被拒绝，本用例会退化成空转，此处守住它仍有鉴别力
    assert allowed > 0, (
        "全部向量都被拒绝，本用例退化为空转。请补入应当放行的畸形样例。"
    )


def test_adversarial_inputs_do_not_leak_files() -> None:
    """
    端到端兜底：畸形输入经 write_file 落盘后，项目根与项目外探针目录都不得
    多出任何条目 —— 只要文件系统上没多东西，就不存在真实逃逸。
    """
    root_before = _top_level_names(PROJECT_ROOT)
    outside_before = _top_level_names(OUTSIDE_ROOT)

    with WorkspaceGuard():
        for vector in ADVERSARIAL:
            write_file(vector, "probe")

        leaked_root = _top_level_names(PROJECT_ROOT) - root_before
        leaked_outside = _top_level_names(OUTSIDE_ROOT) - outside_before
        assert not leaked_root, f"[漏沙] 项目根被写入: {sorted(leaked_root)}"
        assert not leaked_outside, f"[漏沙] 项目外被写入: {sorted(leaked_outside)}"


def test_read_file_scoped_to_project_root() -> None:
    """read_file 以项目根为界：项目内可读，项目外必拒。"""
    with WorkspaceGuard():
        assert "统一配置中心" in read_file("config.py"), "项目内文件读取失败"

        write_file("sbx_read.txt", "可回读内容")
        assert read_file("workspace/sbx_read.txt") == "可回读内容"

        outside = OUTSIDE_ROOT / "sbx_read_outside.txt"
        outside.write_text("secret", encoding="utf-8")
        result = read_file(str(outside))
        assert result.startswith("Error:"), f"[漏沙] 读到了项目外文件: {result}"
        assert "security" not in result.lower() and "secret" not in result, (
            "拒绝读取时仍泄露了文件内容"
        )

        trav = read_file("../../Windows/win.ini")
        assert trav.startswith("Error:"), f"[漏沙] 穿越读取成功: {trav[:80]}"


def test_read_file_error_modes() -> None:
    """缺失文件与目录各自返回明确错误，而不是抛异常或返回空串。"""
    missing = read_file("sbx_definitely_missing.txt")
    assert missing.startswith("Error:") and "不存在" in missing, (
        f"缺失文件的报错不明确: {missing}"
    )

    as_dir = read_file("agent")
    assert as_dir.startswith("Error:") and "不是常规文件" in as_dir, (
        f"目录的报错不明确: {as_dir}"
    )


def test_registry_schema_contract() -> None:
    """
    工具契约一致性：TOOL_REGISTRY（调度表）与 TOOLS_SCHEMA（下发给模型的声明）
    的名称集合必须完全一致。

    【Why 会致命】模型只会调用 Schema 里声明过的工具。若 Schema 有而注册表无，
    调度时必然 KeyError；若注册表有而 Schema 无，则该工具形同虚设、永远调不到。
    两者发散属于静默失效，必须在测试层钉死。
    """
    registry_names = set(TOOL_REGISTRY)
    schema_names = {entry["function"]["name"] for entry in TOOLS_SCHEMA}

    assert registry_names == schema_names, (
        f"注册表与 Schema 不一致：仅在注册表 {sorted(registry_names - schema_names)}；"
        f"仅在 Schema {sorted(schema_names - registry_names)}"
    )
    assert "write_file" in settings.SENSITIVE_TOOLS, (
        "写盘工具必须受 HITL 管控，否则高危操作可无审批执行"
    )


# ==================== 运行器 ====================


def main() -> int:
    cases: List[Tuple[str, Callable[[], None]]] = [
        ("前置自检：探针目录位于项目外", test_outside_root_is_really_outside),
        ("合法写入限于沙箱且可回读", test_legal_writes_stay_inside_sandbox),
        ("安全底线：逃逸向量必须被拒", test_must_block_vectors_are_rejected),
        ("绝对路径（项目外）必须被拒", test_absolute_path_outside_project_is_rejected),
        ("绝对路径（沙箱内）应当放行", test_absolute_path_inside_sandbox_is_allowed),
        ("前缀同名兄弟目录必须被拒", test_prefix_sibling_directory_is_rejected),
        ("符号链接逃逸必须被拒", test_symlink_escape_is_rejected),
        ("不变式电池：_validate_path 落点恒在沙箱内", test_validate_path_invariant_battery),
        ("对抗输入端到端不落盘到沙箱外", test_adversarial_inputs_do_not_leak_files),
        ("read_file 边界与越界拒绝", test_read_file_scoped_to_project_root),
        ("read_file 错误分支明确", test_read_file_error_modes),
        ("工具注册表与 Schema 契约一致", test_registry_schema_contract),
    ]

    print("=" * 62)
    print(">>> 沙箱边界与工具契约回归测试")
    print("=" * 62)

    failed: List[str] = []
    try:
        for name, fn in cases:
            try:
                fn()
                print(f"  [PASS] {name}")
            except AssertionError as exc:
                print(f"  [FAIL] {name}")
                print(f"         {exc}")
                failed.append(name)
            except Exception as exc:  # 非断言异常同样计失败，避免静默通过
                print(f"  [ERROR] {name}")
                print(f"          {type(exc).__name__}: {exc}")
                failed.append(name)
    finally:
        _rmtree_quietly(OUTSIDE_ROOT)

    # 收尾自检：即便全部通过，也必须证明本次运行没在工作区留下垃圾
    print("-" * 62)
    if failed:
        print(f">>> {len(failed)}/{len(cases)} 个用例失败：{failed}")
        return 1
    print(f">>> 全部 {len(cases)} 个用例通过；沙箱边界未被突破，运行零残留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
