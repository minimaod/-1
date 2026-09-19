"""
【模块职责导读】上下文治理回归测试（Context Manager Test Suite）
- 职责范围：对 ContextManager.truncate_history 的滑动窗口裁剪做协议级回归验证。
- 运行方式：python test_context.py    （不依赖 pytest，纯 stdlib 断言脚本）
- 覆盖范围：
  1. 协议不变式（自 main.py 迁移并修正）：System Pinning 与 Tool Pair Atomicity。
  2. 裁剪结构性：窗口必须是原历史的「从尾部起算的连续子序列」，绝不跳跃取样。
  3. 预算约束：裁剪结果必须落在 max_tokens 之内。
  4. 浅拷贝语义：返回新列表，交出的视窗不被后续 history 变更污染。
  5. 随机压力：随机生成含工具对的对话 + 随机预算，反复裁剪并逐次校验不变式。

【Why 为什么值得单测】ContextManager 只依赖 config，无网络无数据库依赖；
但它的裁剪边界一旦出错，表现是 LLM 侧 400 Bad Request，且只在长对话下偶发，
人工极难复现。这类「静默的协议错误」必须靠不变式断言兜住。
"""

import random
from typing import Any, Callable, Dict, List, Tuple

from agent.context import ContextManager


# ==================== 协议不变式校验器（自 main.py 迁移并修正）====================


def assert_protocol_invariants(history: List[Dict[str, Any]]) -> None:
    """
    协议不变式强校验（防 LLM 侧 400 校验器）。

    【相对 main.py 原版的两处必要修正】

    修正一 · System 断言的前提被收窄
      原版无条件断言 history[0] 必须是 system，但「无 system 的纯对话历史」是
      ContextManager 的合法输入形态。真正的 System Pinning 不变式是：**若原历史
      有 system，裁剪后必须仍在首位**。该断言已拆到 assert_system_pinned 中。

    修正二 · 原版的「前驱必须是 assistant」是错误判据
      一个 assistant 消息可以并发发起**多个** tool_calls，其回执必然是连续多条
      tool；此时第 2 条及以后的 tool，其前驱是 tool 而非 assistant，却完全合法。
      原版在 main.py 的 run_stress_test 里只用了 tool_count=1，故该缺陷从未暴露。
      正确判据是「已开启但未闭环的 tool_calls 块」：见下方 open_ids 队列。

    校验四条：
    1. 不允许出现孤儿 tool 回执（其 tool_call_id 不在任何未闭环块中）。
    2. 一个 tool_calls 块未闭环前，不得出现新的 assistant.tool_calls。
    3. 非 tool 消息出现时，前一个 tool_calls 块必须已完全闭环。
    4. 末尾不得残留未闭环块（被截断的窗口会直接导致 400）。

    注：回执与 tool_calls 的对应关系按 id 判定，**不要求顺序一致** ——
    OpenAI/DeepSeek 只要求每个 tool_call_id 都被应答，不约束应答顺序。
    """
    if not history:
        return

    open_ids: List[str] = []  # 已开启、尚未收到回执的 tool_call_id 队列

    for i, msg in enumerate(history):
        role = msg.get("role")

        if role == "tool":
            tcid = msg.get("tool_call_id")
            assert tcid in open_ids, (
                f"[FATAL] 孤儿 tool 回执 (Index {i}, tool_call_id={tcid})："
                f"它不属于任何未闭环的 tool_calls 块。当前待闭环 id = {open_ids}。"
                "LLM 侧会返回 400 Bad Request。"
            )
            open_ids.remove(tcid)
            continue

        # 非 tool 消息：上一个 tool_calls 块必须已完全闭环
        assert not open_ids, (
            f"[FATAL] tool_calls 块未闭环 (Index {i})：出现了 role={role} 消息，"
            f"但仍缺 {open_ids} 的回执。工具调用链断裂。"
        )

        if role == "assistant" and msg.get("tool_calls"):
            ids = [tc.get("id") for tc in msg["tool_calls"]]
            assert len(ids) == len(set(ids)), (
                f"[FATAL] Index {i} 的 tool_calls 存在重复 id: {ids}"
            )
            open_ids = list(ids)

    assert not open_ids, (
        f"[FATAL] 末尾残留未闭环的 tool_calls 块：缺 {open_ids} 的回执。"
        "裁剪窗口若以未应答的 assistant.tool_calls 结尾，LLM 侧必然 400。"
    )


def assert_system_pinned(original: List[Dict[str, Any]], window: List[Dict[str, Any]]) -> None:
    """若原历史以 system 开头，裁剪后必须仍是同一条 system 打头。"""
    if original and original[0].get("role") == "system":
        assert window and window[0].get("role") == "system", (
            "[FATAL] System Prompt 被挤出上下文！System Pinning 失效。"
        )
        assert window[0]["content"] == original[0]["content"], (
            "[FATAL] 首条 system 的内容被篡改！"
        )


def assert_window_is_suffix(
    original: List[Dict[str, Any]], window: List[Dict[str, Any]]
) -> None:
    """
    裁剪后的窗口必须是「去掉 system 后、从尾部起算的连续子序列」。

    【Why 单独校验这一条】滑动窗口若实现成「跳跃取样」（例如为了凑预算
    跳过中间某条超长消息、却保留更早的短消息），协议不变式可能碰巧成立，
    但对话因果链已断裂 —— 模型会看到结论却看不到前提。连续性必须单独守住。
    """
    chat = original[1:] if original and original[0].get("role") == "system" else original[:]
    body = window[1:] if window and window[0].get("role") == "system" else window[:]
    if not body:
        return
    assert len(body) <= len(chat), "[FATAL] 裁剪后窗口反而比原历史更长！"
    assert body == chat[len(chat) - len(body):], (
        "[FATAL] 裁剪窗口不是原历史从尾部起算的连续子序列（发生了跳跃取样）。"
    )


# ==================== 造数工具 ====================


def make_tool_turn(turn: int, tool_count: int = 2, big: bool = False) -> List[Dict[str, Any]]:
    """构造一轮「用户提问 → assistant 发起 tool_calls → N 条 tool 回执 → assistant 总结」。"""
    calls = [
        {
            "id": f"call_{turn}_{k}",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"filepath": "a.txt"}'},
        }
        for k in range(tool_count)
    ]
    payload = ("ERROR 500 connection reset\n" * 40) if big else "ok"
    return [
        {"role": "user", "content": f"第 {turn} 轮提问"},
        {"role": "assistant", "content": None, "tool_calls": calls},
        *[
            {"role": "tool", "tool_call_id": c["id"], "content": payload}
            for c in calls
        ],
        {"role": "assistant", "content": f"第 {turn} 轮总结"},
    ]


def make_text_turn(turn: int, repeat: int = 1) -> List[Dict[str, Any]]:
    return [
        {"role": "user", "content": f"第 {turn} 轮闲聊" * repeat},
        {"role": "assistant", "content": f"第 {turn} 轮回应" * repeat},
    ]


def make_random_conversation(rng: random.Random, turns: int, with_system: bool) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    if with_system:
        history.append({"role": "system", "content": "You are a senior DevOps SRE Assistant."})
    for t in range(1, turns + 1):
        if rng.random() < 0.45:
            history.extend(
                make_tool_turn(
                    t,
                    tool_count=rng.randint(1, 3),
                    big=rng.random() < 0.3,
                )
            )
        else:
            history.extend(make_text_turn(t, repeat=rng.randint(1, 4)))
    return history


# ==================== 用例 ====================


def test_empty_and_under_budget() -> None:
    """空列表与未超预算：原样语义，且必须返回新列表（浅拷贝）。"""
    cm = ContextManager(max_tokens=8000)

    assert cm.truncate_history([]) == [], "空列表应返回空列表"

    short = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "你好"},
    ]
    out = cm.truncate_history(short)
    assert out == short, "未超预算时应保留全部消息"
    assert out is not short, "未超预算时必须返回浅拷贝，而非原始引用"


def test_system_pinning_under_pressure() -> None:
    """超预算时 system 必须钉在首位，且窗口内总量不超预算。"""
    cm = ContextManager(max_tokens=120)
    history = [{"role": "system", "content": "SYSTEM_PROMPT_PINNED"}]
    for t in range(1, 12):
        history.extend(make_text_turn(t, repeat=6))

    assert cm.estimate_tokens(history) > cm.max_tokens, "造数未超预算，用例无效"

    out = cm.truncate_history(history)
    assert_system_pinned(history, out)
    assert_protocol_invariants(out)
    assert_window_is_suffix(history, out)
    assert ContextManager.estimate_tokens(out) <= cm.max_tokens, (
        f"裁剪后仍超预算: {ContextManager.estimate_tokens(out)} > {cm.max_tokens}"
    )
    assert out[-1] == history[-1], "最新一轮必须被保留"


def test_no_system_history_is_legal() -> None:
    """纯对话历史（无 system）是合法形态，裁剪不得凭空造出 system。"""
    cm = ContextManager(max_tokens=100)
    history: List[Dict[str, Any]] = []
    for t in range(1, 12):
        history.extend(make_text_turn(t, repeat=6))

    out = cm.truncate_history(history)
    assert all(m.get("role") != "system" for m in out), "不得凭空注入 system 消息"
    assert_protocol_invariants(out)
    assert_window_is_suffix(history, out)


def test_tool_pair_atomicity_orphan_stripped() -> None:
    """裁剪边界落在 tool 回执中间时，孤立的 tool 必须被剔除。"""
    cm = ContextManager(max_tokens=1)  # 极端预算，尽可能裁到只剩尾部
    history = [
        {"role": "system", "content": "sys"},
        *make_tool_turn(1, tool_count=3),
        {"role": "assistant", "content": "收尾"},
    ]

    out = cm.truncate_history(history)
    assert_system_pinned(history, out)
    assert_protocol_invariants(out)
    assert out[0].get("role") != "tool", "首条不应是孤儿 tool"
    # 极端预算下，除去被钉住的 system，只能再容纳极少消息
    assert ContextManager.estimate_tokens(out) <= cm.max_tokens or len(out) == 1


def test_tool_calls_never_orphaned_by_parent_loss() -> None:
    """assistant.tool_calls 被保留时，其全部 tool 回执必须同时在窗口内（1:1 闭环）。"""
    cm = ContextManager(max_tokens=260)
    history = [{"role": "system", "content": "sys"}]
    for t in range(1, 8):
        history.extend(make_tool_turn(t, tool_count=2, big=True))

    assert cm.estimate_tokens(history) > cm.max_tokens
    out = cm.truncate_history(history)

    assert_system_pinned(history, out)
    assert_protocol_invariants(out)
    assert_window_is_suffix(history, out)

    # 显式再验一次 1:1：每条 assistant.tool_calls 的 id 必须全部出现在窗口内
    window_ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    for idx, msg in enumerate(out):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                assert tc["id"] in window_ids, (
                    f"[FATAL] assistant.tool_calls 的 {tc['id']} 没有对应 tool 回执"
                )


def test_oversized_single_message() -> None:
    """单条消息就超过预算：不得崩溃；system 仍须钉住。"""
    cm = ContextManager(max_tokens=50)
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "超长" * 500},
        {"role": "assistant", "content": "短"},
    ]
    out = cm.truncate_history(history)
    assert_system_pinned(history, out)
    assert_protocol_invariants(out)
    assert len(out) >= 1
    assert out[-1] == history[-1], "最新一条应优先保留"


def test_shallow_copy_isolates_window() -> None:
    """
    交出的视窗必须是列表层面的快照：后续对原历史的增删不得影响已交出的窗口。

    【边界说明】list(messages) 是**浅拷贝**——它隔离的是列表结构，不是元素内部。
    视窗与 history 共享同一批 message dict 对象，因此对某条消息做**就地改写**
    （如 history[0]["content"] = ...）仍会穿透到视窗。当前全链路没有就地改写
    history 元素的代码路径，故不构成缺陷；若日后需要完全隔离，须改用 deepcopy。
    """
    cm = ContextManager(max_tokens=8000)
    history = [{"role": "user", "content": "你好"}]
    window = cm.truncate_history(history)

    # 列表层面：追加 / 弹出不得影响已交出的视窗
    history.append({"role": "assistant", "content": "后续追加"})
    assert len(window) == 1, f"视窗被后续追加污染，长度变为 {len(window)}"
    history.pop()
    history.clear()
    assert len(window) == 1, f"视窗被后续清空污染，长度变为 {len(window)}"

    # 超预算分支同样必须返回新列表
    cm_small = ContextManager(max_tokens=1)
    big = [{"role": "user", "content": "很长" * 100}]
    assert cm_small.truncate_history(big) is not big, "超预算分支也必须返回新列表"


def test_randomized_stress() -> None:
    """随机压力：随机对话结构 + 随机预算，反复裁剪，逐次校验全部不变式。"""
    rng = random.Random(20260919)  # 固定种子，失败可复现
    iterations = 400

    for n in range(iterations):
        with_system = rng.random() < 0.7
        history = make_random_conversation(rng, turns=rng.randint(3, 14), with_system=with_system)
        if not history:
            continue
        budget = rng.choice([30, 60, 120, 250, 500, 1000, 4000, 100000])
        cm = ContextManager(max_tokens=budget)

        out = cm.truncate_history(history)

        try:
            assert_protocol_invariants(out)
            assert_system_pinned(history, out)
            assert_window_is_suffix(history, out)
        except AssertionError as e:
            raise AssertionError(
                f"[随机压测第 {n} 次失败] budget={budget}, with_system={with_system}, "
                f"原历史 {len(history)} 条\n{e}"
            ) from e

        # 预算约束：除「单条自身即超预算」的不可避免情形外，必须落在预算内
        if len(out) > 1 or not with_system:
            est = ContextManager.estimate_tokens(out)
            assert est <= budget or len(out) <= 2, (
                f"[随机压测第 {n} 次失败] budget={budget} 但裁剪后 est={est}, 窗口={len(out)} 条"
            )


# ==================== 运行器 ====================


def main() -> int:
    cases: List[Tuple[str, Callable[[], None]]] = [
        ("空列表与未超预算（浅拷贝语义）", test_empty_and_under_budget),
        ("System Pinning 承压", test_system_pinning_under_pressure),
        ("无 system 历史为合法形态", test_no_system_history_is_legal),
        ("Tool Pair Atomicity：孤儿 tool 剔除", test_tool_pair_atomicity_orphan_stripped),
        ("Tool Pair Atomicity：1:1 闭环不破", test_tool_calls_never_orphaned_by_parent_loss),
        ("单条即超预算的极端情形", test_oversized_single_message),
        ("浅拷贝隔离：视窗不被污染", test_shallow_copy_isolates_window),
        ("随机压力 400 轮", test_randomized_stress),
    ]

    print("=" * 62)
    print(">>> ContextManager 上下文治理回归测试")
    print("=" * 62)

    failed: List[str] = []
    for name, fn in cases:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}")
            print(f"         {e}")
            failed.append(name)
        except Exception as e:  # 非断言异常同样视为失败，避免静默通过
            print(f"  [ERROR] {name}")
            print(f"          {type(e).__name__}: {e}")
            failed.append(name)

    print("-" * 62)
    if failed:
        print(f">>> {len(failed)}/{len(cases)} 个用例失败：{failed}")
        return 1
    print(f">>> 全部 {len(cases)} 个用例通过；协议不变式在 400 轮随机裁剪下保持成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
