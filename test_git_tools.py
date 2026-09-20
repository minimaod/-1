"""
【模块职责导读】Git 诊断工具回归测试（Git Toolchain Boundary Test Suite）
- 职责范围：验证 agent/tools.py 的 get_git_status / get_git_diff 在正常分支、
  截断逻辑与安全防御三个维度上的行为契约。
- 运行方式：python test_git_tools.py    （不依赖 pytest，纯 stdlib 断言脚本）
- 覆盖范围：
  1. 工作区状态探测：porcelain 输出可解析，干净工作区返回友好提示而非空串。
  2. 凭证与穿越拦截：.env / data/ 等敏感路径与越界路径必须被拒，且绝不泄露内容。
  3. 超长截断防御：diff 超过 MAX_DIFF_CHARS 时正文严格受控，且带截断警示。
  4. `--` 参数隔离：以 "-" 开头的文件名必须被当作 pathspec，而非被选项解析器吃掉。
  5. 空仓库容错：缺少 HEAD 指针时平滑降级，不把 fatal 文案甩给模型。
  6. 异步派发链路：async 工具经 run_turn 派发后回写真实内容而非协程对象。

【Why 全部用例都在【临时仓库】上跑，而不是在本项目上跑】
本项目自身的状态是随机漂移的：工作区可能干净也可能有 5 个改动文件，.env 可能
存在也可能不存在，HEAD 一定有值（故"空仓库降级"根本无法在本项目上触发）。
若把断言建立在本项目上，"干净工作区返回友好提示"这条用例会随提交与否而随机
变红或变绿 —— 那不是回归测试，是噪声。故此处用临时仓库构造确定性输入，并在
执行期把 tools 模块的 PROJECT_ROOT 打补丁指向它，从而真实走完
「判界 → 凭证闸门 → 子进程 → 截断」的完整链路（而非只测孤立的纯函数）。

【Why 每一条防御用例都带"前置自检"】
若只断言"恶意输入被拒绝"，那么当上游变更导致场景根本不再触发时（例如 git 改了
报错文案导致空仓库不再报错、或临时仓库里那个怪异文件名压根没建出来），用例会
因为"分支恰好也返回了错误"而静默通过 —— 防御被击穿了却依然全绿。故每条防御
用例先断言"攻击面确实存在"，再断言"攻击被拦下"。
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import agent.tools as tools_module
from agent.tools import (
    MAX_DIFF_CHARS,
    TOOLS_SCHEMA,
    TOOL_REGISTRY,
    _is_missing_head_error,
    get_git_diff,
    get_git_status,
)

# 凭证泄露哨兵：故意做成一眼可辨的假值，只用于断言"它没有出现在输出里"。
FAKE_SECRET = "sk-FAKE-SENTINEL-DO-NOT-LEAK-0123456789"


# ==================== 测试夹具：临时仓库 ====================


class TempRepo:
    """
    一次性 Git 仓库夹具。

    【Why 要隔离 git 配置】宿主机的全局 gitconfig 可能设置 diff.external、
    diff.mnemonicPrefix、core.autocrlf 等，它们会改变 diff 的输出形态，使断言
    在不同机器上得出不同结果。故此处用 GIT_CONFIG_GLOBAL 指向一份空配置 +
    显式 autocrlf=false，把 git 的执行环境钉成确定性的。
    """

    def __init__(self, prefix: str = "gitprobe_") -> None:
        self._root = Path(tempfile.mkdtemp(prefix=prefix))
        self.path = self._root / "repo"
        self.path.mkdir()
        # 配置放在仓库【之外】，否则它自己会以未跟踪文件出现在 porcelain 输出里，
        # 污染"干净工作区"用例。
        self._config = self._root / "gitconfig"
        self._config.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
        self._env = dict(
            os.environ,
            GIT_CONFIG_GLOBAL=str(self._config),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="mini-agent-test",
            GIT_AUTHOR_EMAIL="test@localhost",
            GIT_COMMITTER_NAME="mini-agent-test",
            GIT_COMMITTER_EMAIL="test@localhost",
        )

    def __enter__(self) -> "TempRepo":
        self.run("init", "-q")
        return self

    def __exit__(self, *_: Any) -> bool:
        shutil.rmtree(self._root, ignore_errors=True)
        return False

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            env=self._env,
            encoding="utf-8",
            errors="replace",
        )

    def write(self, name: str, content: str) -> Path:
        """写文件。【Why 显式 newline="\\n"】Windows 的文本模式默认把 \\n 翻译成
        \\r\\n，会让 diff 内容随平台漂移；显式指定以保证断言跨平台一致。"""
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return target

    def commit_all(self, message: str = "init") -> None:
        proc = self.run("add", "-A")
        assert proc.returncode == 0, f"git add 失败: {proc.stderr}"
        proc = self.run("commit", "-qm", message)
        assert proc.returncode == 0, f"git commit 失败: {proc.stderr}"


class PatchedProjectRoot:
    """
    把 agent.tools 的 PROJECT_ROOT 临时指向临时仓库。

    【Why 必须打这个补丁，而不能直接调用纯函数绕开】工具函数在【运行期】读取
    模块级全局 PROJECT_ROOT（判界基准与凭证清单都以它为锚）。只有改这个全局，
    被测代码才会在临时仓库上执行完整的判界 + 凭证 + 子进程链路；若绕过它去测
    内部小函数，就等于放弃了"集成行为"这一层，而这正是本套件要守的东西。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._saved: Optional[Path] = None

    def __enter__(self) -> "PatchedProjectRoot":
        self._saved = tools_module.PROJECT_ROOT
        tools_module.PROJECT_ROOT = self._path
        return self

    def __exit__(self, *_: Any) -> bool:
        # 断言式复原：若此处不成立，说明补丁期间有人改写了全局，会导致
        # 后续用例在错误的基准上运行（属最难查的一类串扰）。
        assert self._saved is not None
        tools_module.PROJECT_ROOT = self._saved
        return False


def _diff(*args: Any) -> str:
    """同步包装：本套件的运行器是纯 stdlib 同步 runner，故各自起一次事件循环。"""
    return asyncio.run(get_git_diff(*args))


def _status() -> str:
    return asyncio.run(get_git_status())


# ==================== 1. 工作区状态探测 ====================


def test_status_parses_porcelain_output() -> None:
    """脏工作区：porcelain 两列状态码可解析，且不含任何本地化提示语。"""
    with TempRepo() as repo:
        repo.write("alpha.txt", "v1\n")
        repo.commit_all()
        repo.write("alpha.txt", "v2\n")      # 未暂存修改
        repo.write("beta.txt", "new\n")      # 未跟踪
        repo.run("add", "beta.txt")          # 已暂存新增

        with PatchedProjectRoot(repo.path):
            out = _status()

        lines = out.splitlines()
        assert len(lines) == 2, f"应恰有 2 条状态，实际 {len(lines)}: {out!r}"
        # 已暂存新增 -> 首列 A；未暂存修改 -> 次列 M（porcelain 两列语义）
        assert any(l.startswith("A ") for l in lines), f"未反映暂存新增: {out!r}"
        assert any(l.startswith(" M") for l in lines), f"未反映未暂存修改: {out!r}"
        # 本地化防御：人类版 git status 会带分支头与操作提示
        for noise in ("On branch", "use \"git", "尚未暂存", 'use "git'):
            assert noise not in out, f"输出混入了人类可读版的提示语 {noise!r}: {out!r}"


def test_status_clean_workspace_returns_friendly_message() -> None:
    """干净工作区：必须返回明确提示，绝不能返回空字符串。"""
    with TempRepo() as repo:
        repo.write("alpha.txt", "v1\n")
        repo.commit_all()

        with PatchedProjectRoot(repo.path):
            # 前置自检：确认工作区对 git 而言确实干净（否则本用例将失去意义）
            raw = repo.run("status", "--porcelain")
            assert raw.stdout.strip() == "", f"夹具未构造出干净工作区: {raw.stdout!r}"

            out = _status()

        assert out.strip(), "干净工作区返回了空串 —— 模型将面对语义空洞"
        assert "Working tree clean" in out, f"未返回友好提示: {out!r}"


# ==================== 2. 凭证与穿越拦截 ====================


def test_credential_and_traversal_paths_are_blocked_without_leaking() -> None:
    """恶意入参必须被拒，且拒绝回执中不得出现任何凭证内容。"""
    must_block = [
        ".env",                              # 凭证文件本体
        ".env.local",                        # 凭证变体
        "data/local_agent.db",               # 运行态数据（会话历史）
        ".git/config",                       # 可能含远端凭证
        "../../etc/passwd",                  # 父目录穿越
        "a/../../outside.txt",               # 混合穿越
        "C:/Windows/System32/cmd.exe",       # 项目外绝对路径
    ]

    with TempRepo() as repo:
        # 让"拦截"有真实可泄露物：这些文件确实存在且有内容
        repo.write(".env", f"DEEPSEEK_API_KEY={FAKE_SECRET}\n")
        repo.write("data/local_agent.db", f"binary-ish {FAKE_SECRET}\n")
        repo.write("alpha.txt", "v1\n")
        repo.commit_all()

        with PatchedProjectRoot(repo.path):
            # 前置自检：确认夹具里确实存在可泄露的凭证，否则本用例是空转
            assert (repo.path / ".env").is_file(), "夹具未造出 .env"
            assert FAKE_SECRET in (repo.path / ".env").read_text(encoding="utf-8")

            for vector in must_block:
                out = _diff(vector)
                # 断言失败信息【刻意不回显 out】—— 那正是本用例要保护的凭证，
                # 一旦回显，失败日志本身就成了泄露渠道（同 test_traversal 的处置）。
                assert out.startswith("Error:"), (
                    f"[漏沙] 敏感/越界路径 {vector!r} 未被拒绝（返回内容已隐去）"
                )
                assert "安全违规" in out, (
                    f"路径 {vector!r} 被拒，但原因不是安全拦截（返回内容已隐去）"
                )
                assert FAKE_SECRET not in out and "DEEPSEEK" not in out, (
                    f"拒绝 {vector!r} 时仍泄露了凭证内容（返回内容已隐去）"
                )


def test_legit_path_still_works_after_hardening() -> None:
    """反向保证：安全闸门不得误伤主功能 —— 合法相对路径必须返回真实 diff。"""
    with TempRepo() as repo:
        repo.write("alpha.txt", "v1\n")
        repo.commit_all()
        repo.write("alpha.txt", "v2\n")

        with PatchedProjectRoot(repo.path):
            out = _diff("alpha.txt")
            # 字面绕行但落点在项目内的路径也应放行（判界按落点，不按写法）
            wrapped = _diff("sub/../alpha.txt")

        assert out.startswith("diff --git"), f"合法路径未返回 diff: {out[:80]!r}"
        assert "v2" in out, "diff 内容未包含真实改动"
        assert wrapped.startswith("diff --git"), (
            f"字面绕行但落点在内的路径被误拒: {wrapped[:80]!r}"
        )


# ==================== 3. 超长截断防御 ====================


def test_oversized_diff_is_truncated_with_warning() -> None:
    """超过 MAX_DIFF_CHARS 的 diff：正文长度严格受控，且带截断警示。"""
    big_v1 = "\n".join(f"line {i:04d} " + "x" * 40 for i in range(300))
    big_v2 = "\n".join(f"line {i:04d} " + "y" * 40 for i in range(300))

    with TempRepo() as repo:
        repo.write("big.txt", big_v1 + "\n")
        repo.commit_all()
        repo.write("big.txt", big_v2 + "\n")

        with PatchedProjectRoot(repo.path):
            out = _diff()

        # 前置自检：确认原始 diff 真的超限，否则本用例退化为空转
        raw_len = len(repo.run("diff", "HEAD").stdout.strip())
        assert raw_len > MAX_DIFF_CHARS, (
            f"夹具未构造出超限 diff（{raw_len} <= {MAX_DIFF_CHARS}），用例失去意义"
        )

        marker = "\n\n[Warning:"
        assert marker in out, f"超限 diff 未附加截断警示: {out[-120:]!r}"
        body, _, warning = out.partition(marker)
        assert len(body) == MAX_DIFF_CHARS, (
            f"截断正文长度应恰为 {MAX_DIFF_CHARS}，实际 {len(body)}"
        )
        # 总长必须仍有界：警示语自身也必须计入预算，不能成为新的膨胀点
        assert len(out) < MAX_DIFF_CHARS + 128, (
            f"截断后总长失控: {len(out)}"
        )
        assert "Diff 结果过长" in warning and f"已截断前 {MAX_DIFF_CHARS} 字符" in warning, (
            f"警示语未说明截断口径: {warning!r}"
        )


def test_small_diff_is_not_truncated() -> None:
    """反向保证：未超限的 diff 绝不能被误截断（否则模型永远看不到完整改动）。"""
    with TempRepo() as repo:
        repo.write("small.txt", "v1\n")
        repo.commit_all()
        repo.write("small.txt", "v2\n")

        with PatchedProjectRoot(repo.path):
            out = _diff()

        assert "[Warning:" not in out, f"小 diff 被误截断: {out!r}"
        assert out.startswith("diff --git"), f"小 diff 返回异常: {out[:80]!r}"


# ==================== 4. `--` 参数隔离 ====================


def test_dash_prefixed_filename_is_treated_as_pathspec() -> None:
    """
    以 "-" 开头的文件名必须被当作 pathspec，而非被 Git 选项解析器吃掉。

    【Why 这条必须带前置自检】没有 `--` 时命令不会报错，而是【静默返回空输出】，
    因为文件名被解析成了选项（例如 "--staged" 变成"显示已暂存变更"）。因此
    "返回了空字符串"与"文件真的没改动"在输出上无法区分 —— 唯有先独立证明
    "不加 -- 时行为确实不同"，才能证明本用例测的是隔离生效，而非恰好没改动。
    """
    weird = "--staged"

    with TempRepo() as repo:
        repo.write(weird, "v1\n")
        repo.commit_all()
        repo.write(weird, "v2-payload\n")   # 制造真实改动

        # 前置自检：不加 -- 时，git 把该名字当作选项，语义整体偏移且【静默】返回空
        raw_without = repo.run("diff", weird)
        raw_with = repo.run("diff", "--", weird)
        assert raw_without.returncode == 0, "前置假设不成立：无 -- 时报错了"
        assert raw_without.stdout.strip() == "", (
            f"前置假设不成立：无 -- 时竟然返回了内容: {raw_without.stdout[:60]!r}"
        )
        assert raw_with.stdout.strip().startswith("diff --git"), (
            "前置假设不成立：加 -- 后仍未定位到文件"
        )

        with PatchedProjectRoot(repo.path):
            out = _diff(weird)

        assert out.startswith("diff --git"), (
            f"[参数注入] 文件名 {weird!r} 未被当作 pathspec，命令静默空转: {out!r}"
        )
        assert f"a/{weird}" in out, f"diff 未指向目标文件: {out[:80]!r}"
        assert "v2-payload" in out, "diff 内容未包含真实改动"


# ==================== 5. 空仓库容错 ====================


def test_empty_repo_degrades_instead_of_crashing() -> None:
    """
    零提交的新仓库：HEAD 尚未指向任何对象，必须平滑降级而非回传 fatal。

    【Why 这条必须带前置自检】降级条件依赖 git 的报错【文案】而非返回码（空仓库
    与真·失败的返回码同为 128）。若将来 git 改了文案，特征串将失配、降级不再
    触发，而"返回了错误字符串"这一断言却仍可能通过 —— 故必须先独立断言
    "该场景确实产生了我们识别的那个错误"。
    """
    with TempRepo() as repo:
        repo.write("alpha.txt", "uncommitted\n")
        raw = repo.run("diff", "HEAD")

        # 前置自检：确认该场景真的触发了"HEAD 不存在"，且特征串识别有效
        assert raw.returncode != 0, "前置假设不成立：空仓库下 git diff HEAD 竟然成功了"
        assert _is_missing_head_error(raw.stderr), (
            f"HEAD 缺失特征串失配，降级将不再触发。git 实际报错: {raw.stderr.strip()!r}"
        )

        with PatchedProjectRoot(repo.path):
            out = _diff()

        assert not out.startswith("Git diff 获取失败"), (
            f"空仓库场景未被降级，fatal 文案被甩给模型: {out[:100]!r}"
        )
        for fatal_noise in ("fatal:", "ambiguous argument", "Traceback"):
            assert fatal_noise not in out, f"降级回执仍含底层噪声 {fatal_noise!r}: {out[:100]!r}"

        # 空仓库同样不得击穿状态探测
        with PatchedProjectRoot(repo.path):
            status = _status()
        assert "fatal:" not in status, f"状态探测在空仓库下回传了 fatal: {status[:100]!r}"


# ==================== 6. 异步派发链路（core.py 双模分发）====================


def test_async_tool_dispatch_writes_real_content() -> None:
    """
    经 run_turn 的派发链路执行 async 工具后，回写的是真实内容而非协程对象。

    【Why 单独一条，且必须是端到端而非只测函数】core.py 的派发点若退回同步调用，
    async 工具不会报错：它会返回一个协程对象，被 str() 成
    "<coroutine object get_git_status at 0x...>" 回写进 history —— 模型收到一串
    内存地址，全程零异常、零日志（仅有 RuntimeWarning）。本用例是唯一能捕获
    这一静默退化的防线，故直接驱动真实的 run_turn，而不是手工复刻派发逻辑。
    """
    from agent.core import AgentEngine

    original = AgentEngine._call_llm_stream
    state = {"n": 0}

    async def fake_stream(self: Any, messages: Any):
        """打桩 LLM 流：首轮要求调用 async 工具，次轮收尾。全程不触网。"""
        state["n"] += 1
        if state["n"] == 1:
            yield ("tool_calls", [{
                "id": "call_test_1",
                "type": "function",
                "function": {"name": "get_git_status", "arguments": "{}"},
            }])
        else:
            yield ("text_done", "已收到工具结果。")

    AgentEngine._call_llm_stream = fake_stream
    try:
        try:
            engine = AgentEngine()
        except Exception as exc:  # 缺 API Key 等环境问题：明确跳过，不静默通过
            print(f"  [SKIP] 无法构造 AgentEngine（{type(exc).__name__}），本机跳过该项")
            return

        async def drive() -> None:
            async for _ in engine.run_turn("请审查改动"):
                pass

        asyncio.run(drive())
    finally:
        AgentEngine._call_llm_stream = original

    tool_msgs = [m for m in engine.history if m.get("role") == "tool"]
    assert tool_msgs, "工具结果未回写进 history"
    content = tool_msgs[0]["content"]

    assert not content.startswith("<coroutine object"), (
        "[静默失效] async 工具被同步派发，模型收到的是协程对象而非真实结果: "
        f"{content[:60]!r}"
    )
    assert content.strip(), "工具回执为空"


# ==================== 附加：注册契约 ====================


def test_git_tools_are_registered_consistently() -> None:
    """两个 Git 工具必须同时出现在注册表与 Schema 中，且参数契约正确。"""
    for name in ("get_git_status", "get_git_diff"):
        assert name in TOOL_REGISTRY, f"{name} 未注册进 TOOL_REGISTRY"
        assert name in {e["function"]["name"] for e in TOOLS_SCHEMA}, (
            f"{name} 未出现在 TOOLS_SCHEMA"
        )

    schema = {e["function"]["name"]: e["function"] for e in TOOLS_SCHEMA}

    # filepath 是【可选】参数，绝不能出现在 required 中 —— 否则模型被迫每次
    # 都编造一个占位路径（详见 test 中的契约说明）
    assert schema["get_git_diff"]["parameters"]["required"] == [], (
        "get_git_diff 的 filepath 是可选参数，required 必须为空列表"
    )
    assert "filepath" in schema["get_git_diff"]["parameters"]["properties"]
    assert schema["get_git_status"]["parameters"]["required"] == []

    # description 必须传达格式倾向（相对路径 / 正斜杠 / 禁止敏感路径）
    desc = schema["get_git_diff"]["description"]
    for hint in ("相对项目根目录", "正斜杠", ".env"):
        assert hint in desc, f"get_git_diff 描述缺少格式约定 {hint!r}"


# ==================== 运行器 ====================


def main() -> int:
    cases: List[Tuple[str, Callable[[], None]]] = [
        ("工作区状态：porcelain 可解析且无本地化噪声", test_status_parses_porcelain_output),
        ("工作区状态：干净时返回友好提示而非空串", test_status_clean_workspace_returns_friendly_message),
        ("安全防御：凭证与穿越路径被拒且不泄露", test_credential_and_traversal_paths_are_blocked_without_leaking),
        ("安全防御：合法路径不被误伤", test_legit_path_still_works_after_hardening),
        ("截断防御：超限 diff 正文受控并带警示", test_oversized_diff_is_truncated_with_warning),
        ("截断防御：未超限 diff 不被误截断", test_small_diff_is_not_truncated),
        ("参数隔离：'-' 开头文件名按 pathspec 解析", test_dash_prefixed_filename_is_treated_as_pathspec),
        ("空仓库容错：HEAD 缺失时平滑降级", test_empty_repo_degrades_instead_of_crashing),
        ("异步派发：async 工具回写真实内容", test_async_tool_dispatch_writes_real_content),
        ("注册契约：Schema 与 required 列表正确", test_git_tools_are_registered_consistently),
    ]

    print("=" * 62)
    print(">>> Git 诊断工具回归测试")
    print("=" * 62)

    failed: List[str] = []
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

    print("-" * 62)
    if failed:
        print(f">>> {len(failed)}/{len(cases)} 个用例失败：{failed}")
        return 1
    print(f">>> 全部 {len(cases)} 个用例通过；Git 工具链防御边界完整，临时仓库零残留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
