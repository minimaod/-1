"""
test_concurrency_offline.py - 离线并发与事件循环活性回归套件

【设计目的（Why）】
在零 Token 消耗、零网络依赖的确定性环境中，验证多 Session 并发时：
1. 耗时工具经 asyncio.to_thread 卸载后，主事件循环零阻塞，其他 Session 可自由抢占交错；
2. inspect.isawaitable 对同步/异步工具的双模派发在并发竞争下无静默漏 await；
3. 多引擎实例的 history 状态与事件流绝对物理隔离（Zero-Crosstalk）。

【系统防错点（Caveats）】
1. 严禁触达真实 DeepSeek API —— 全部通过替换 chat.completions.create 注入确定性帧流，
   故本套件在【物理上】不可能产生外部调用（不依赖"记得别删 mock"的自觉）；
2. 耗时度量必须用 time.perf_counter()，严禁 time.time()（墙钟可被 NTP 回拨，且精度不足）；
3. 所有 patch 与临时注册表必须原地复原，保证零污染（见 _TempTools 的防错点说明）。

【实测发现：进程级惰性初始化成本，必须预热】
开发本套件时首轮跑出 max_gap ≈ 319ms 的"伪阻塞"，经 asyncio 慢回调探测器定位后确认：
真正的阻塞源是 openai SDK 的惰性资源导入 —— client.chat / chat.completions 都是会 import
的 cached_property，【首次访问】要同步导入整棵 resources.chat 模块树。实测：
    首次访问 .chat.completions : 296.1ms
    换一个全新 engine 再访问    :   0.0ms
    同一 engine 缓存命中        :   0.0ms
由于该导入发生在 _call_llm_stream 内部，它必然落在事件循环里 —— 于是"首次 run_turn"
看上去像一次阻塞型工具。同类一次性成本还有 core.py:123 的延迟导入（破环点，
import agent.session 冷启动实测 757ms）。

【Why 必须预热而不是放宽阈值】这是进程级一次性成本，不是稳态行为。若把它计入被测区间，
断言结果将取决于"本进程此前有没有别的代码碰过 openai"：同一份代码在 CI 上失败、在本机
第二次运行时通过 —— 那不是回归测试，是缓存探测。故 main() 显式预热，使四个用例度量的
都是稳态行为。（该成本对生产的影响另见交付说明：首个请求会阻塞循环约 300ms。）

【本套件相对"仅断言时序偏序"的强化：心跳看门狗】
只断言 Start(A) < Start(B) < Done(B) < Done(A) 是【必要但不充分】的：它证明了 B 挤进了
A 的窗口，却没直接证明"循环在 A 等待期间是活的" —— 若该窗口内恰好没有别的协程想跑，
偏序关系照样成立。故本套件额外挂一个 5ms 心跳协程，测量相邻心跳的**最大间隔**。
A 若把阻塞挪进线程池，最大间隔停留在 OS 定时器粒度附近；A 若是阻塞型 async 工具，
最大间隔直接跳到与阻塞时长同量级。这是对"循环活性"的**直接测量**，而非由偏序推断。

更进一步：test_watchdog_detects_blocking_async_tool 反向证明该看门狗**确实有能力**发现
阻塞工具。没有这条自校验，前一条用例的阈值断言可能在阈值被误设得过宽时退化为恒真 ——
只断言"没发现问题"的测试，无法区分"真的没问题"与"探测器坏了"。
"""

import asyncio
import gc
import json
import time
import warnings
from contextlib import ExitStack
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

from agent.core import TOOL_REGISTRY as CORE_TOOL_REGISTRY
from agent.events import AgentEvent
from agent.tools import TOOL_REGISTRY
from config import settings

# 看门狗阈值（秒）。
# 【Why 取 0.12 而不是贴着阻塞时长取】下界受 OS 定时器粒度约束：Windows 默认定时器精度
# 约 15.6ms，asyncio.sleep(0.005) 实测会退化为 ~15~30ms，高负载时更高；上界必须远离被
# 测的阻塞量级（0.3s）才具鉴别力。0.12s 对两侧都留有约 4 倍余量。
WATCHDOG_GAP_LIMIT_S = 0.12
HEARTBEAT_INTERVAL_S = 0.005
BLOCKING_TOOL_SLEEP_S = 0.3
# 工具"确实耗足了时间"的下界系数，用于防止探针因工具被短路而空转通过
ELAPSED_FLOOR_RATIO = 0.8


# =====================================================================
# 0. 流式帧的"形状替身"
# =====================================================================
# 【Why 不能用 plain dict 充当 chunk】agent/core.py 的 _call_llm_stream 是按【属性】
# 消费原始帧的（delta.content / tc_chunk.index / tc_chunk.function.name），这是 OpenAI
# SDK 的真实形态。若用 dict 当帧，_call_llm_stream 会在属性访问处抛 AttributeError ——
# 于是"mock 形状不对"会伪装成"核心逻辑有 bug"。故此处用最小属性替身，忠实复刻访问面。


class _FunctionFragment:
    """function 增量的分片载体（name / arguments 可各为 None，表示本片未携带）。"""

    def __init__(self, name: Optional[str] = None, arguments: Optional[str] = None) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCallFragment:
    """tool_calls 数组中的单个增量分片。"""

    def __init__(
        self, index: int = 0, id: Optional[str] = None, function: Optional[_FunctionFragment] = None
    ) -> None:
        self.index = index
        self.id = id
        self.function = function


class _Delta:
    def __init__(
        self, content: Optional[str] = None, tool_calls: Optional[List[_ToolCallFragment]] = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta: _Delta, finish_reason: Optional[str] = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    """一个流式分片；_call_llm_stream 只读 chunk.choices[0]。"""

    def __init__(self, delta: _Delta, finish_reason: Optional[str] = None) -> None:
        self.choices = [_Choice(delta, finish_reason)]


# =====================================================================
# 1. 确定性 Mock：模拟大模型两阶段流式生成
# =====================================================================


def mock_llm_stream_tool_call(
    tool_name: str, tool_args: Dict[str, Any]
) -> AsyncGenerator[Any, None]:
    """
    模拟底层 API 流式吐出 tool_calls 的原始帧序列。

    【Why 刻意把 name 与 arguments 拆成两片】真实 DeepSeek/OpenAI 流式返回中，函数名
    与参数 JSON 是分片到达的，_call_llm_stream 需要把碎片【增量拼接】（id += /
    name += / arguments +=）。一次给全的 mock 会绕过整条拼接路径，使那部分逻辑永远
    不在回归覆盖内 —— 故首片只带 name，次片只带 arguments。

    【Why 是普通 def 而非 async def】返回一个 async generator 对象本身不需要运行中的
    事件循环（只有迭代它才需要）。用普通 def 使构造期可在任意上下文同步调用，避免在
    async 测试函数内部出现 asyncio.run()（那会直接抛 RuntimeError）。
    """

    async def _gen() -> AsyncGenerator[Any, None]:
        yield _Chunk(_Delta(tool_calls=[
            _ToolCallFragment(index=0, id="call_mock_1",
                              function=_FunctionFragment(name=tool_name, arguments=None))
        ]))
        yield _Chunk(_Delta(tool_calls=[
            _ToolCallFragment(index=0, id=None,
                              function=_FunctionFragment(name=None,
                                                         arguments=json.dumps(tool_args)))
        ]))
        yield _Chunk(_Delta(), finish_reason="tool_calls")

    return _gen()


def mock_llm_stream_final_text(final_text: str) -> AsyncGenerator[Any, None]:
    """模拟第二阶段：工具回执回灌后，模型输出最终自然语言文本。"""

    async def _gen() -> AsyncGenerator[Any, None]:
        yield _Chunk(_Delta(content=final_text))
        yield _Chunk(_Delta(), finish_reason="stop")

    return _gen()


# =====================================================================
# 2. 打桩夹具：脚本化 LLM 客户端 / 临时工具 / 离线凭证 / 心跳探针
# =====================================================================


class _ScriptedClient:
    """
    把 engine.client.chat.completions.create 换成"按脚本吐帧"的假实现。

    每一轮 create() 消费一组帧；脚本长度即 ReAct 轮数（首轮 tool_calls，次轮 stop），
    从而完整驱动 run_turn 的两阶段循环。
    """

    def __init__(self, engine: Any, rounds: List[AsyncGenerator[Any, None]]) -> None:
        self._engine = engine
        self._rounds = list(rounds)
        self._patcher: Any = None
        self.calls = 0

    async def _create(self, **kwargs: Any) -> AsyncGenerator[Any, None]:
        # 【隐形防线】真实网络调用本会在此发生。此实现不发任何请求，因此本套件在
        # 物理上不可能消耗 Token。
        self.calls += 1
        assert self._rounds, "脚本轮次已耗尽：run_turn 的循环次数超出预期"
        return self._rounds.pop(0)

    def __enter__(self) -> "_ScriptedClient":
        # patch.object 打在 completions 实例上：已探明 client.chat.completions 是
        # 稳定的缓存实例（每次访问返回同一对象），故 _call_llm_stream 内部
        # self.client.chat.completions.create(...) 必然命中此处替换。
        self._patcher = patch.object(
            self._engine.client.chat.completions, "create", self._create
        )
        self._patcher.start()
        return self

    def __exit__(self, *_: Any) -> bool:
        self._patcher.stop()
        return False


class _TempTools:
    """
    临时注册测试工具，退出时【原地】复原。

    【防错点：为什么必须原地 clear+update，而不能整体重绑】
    agent/core.py 在模块级执行 `from agent.tools import TOOL_REGISTRY`，因此
    core.TOOL_REGISTRY 与 tools.TOOL_REGISTRY 是**同一个 dict 的两个名字**。
    若用常见写法 `tools.TOOL_REGISTRY = dict(快照)` 做还原，tools 模块换成了新 dict，
    而 core 仍指向那个被改脏的旧 dict —— 测试工具的残留会静默泄漏到后续用例（甚至
    后续其他测试文件），且没有任何报错。故必须原地增删，并由 __enter__ 的同一性
    断言守住这个前提。
    """

    def __init__(self, extra: Dict[str, Callable[..., Any]]) -> None:
        self._extra = extra
        self._snapshot: Dict[str, Callable[..., Any]] = {}

    def __enter__(self) -> "_TempTools":
        assert TOOL_REGISTRY is CORE_TOOL_REGISTRY, (
            "前置假设被打破：core 与 tools 的 TOOL_REGISTRY 已不是同一对象，"
            "原地还原将不再生效，本夹具的隔离性无从保证。"
        )
        self._snapshot = dict(TOOL_REGISTRY)
        TOOL_REGISTRY.update(self._extra)
        return self

    def __exit__(self, *_: Any) -> bool:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(self._snapshot)
        return False


class _OfflineCredentials:
    """
    临时注入假 API Key，保证套件在任何机器上都能构造 AgentEngine。

    【Why 必要】AgentEngine.__init__ 会 new AsyncOpenAI(api_key=settings.DEEPSEEK_API_KEY)，
    而该 SDK 在 Key 为 None/空串时抛 OpenAIError("Missing credentials")。若套件依赖
    开发者本机 .env 里恰好配了 Key，那么在没有 .env 的干净检出（CI、他人机器、面试官
    的 clone）上，全部用例会在【构造阶段】集体 ERROR —— 那不是回归测试，是环境探测。
    假 Key 不会被使用：create 已被 _ScriptedClient 换掉，物理上不出网。
    """

    FAKE_KEY = "sk-offline-concurrency-probe-not-a-real-key"

    def __init__(self) -> None:
        self._saved: Optional[str] = None

    def __enter__(self) -> "_OfflineCredentials":
        self._saved = settings.DEEPSEEK_API_KEY
        settings.DEEPSEEK_API_KEY = self.FAKE_KEY
        return self

    def __exit__(self, *_: Any) -> bool:
        settings.DEEPSEEK_API_KEY = self._saved  # type: ignore[assignment]
        return False


class _Heartbeat:
    """
    事件循环活性探针：每 HEARTBEAT_INTERVAL_S 记一次时间戳，暴露最大间隔。

    【Why 用"最大间隔"而不是"总耗时"】总耗时混入了阻塞与排队，无法归因；最大间隔直接
    刻画"循环最长有多久没能回来跑别的协程"，这正是阻塞与非阻塞的分水岭。

    【防错点：为什么不能丢掉第一个间隔】心跳在 __aenter__ 里先跑一步落下基准时间戳
    再入睡。若被测的阻塞恰好发生在这一瞬之后（本项目正是如此：A 的工具在 gather 的
    首个切片内就阻塞），那么【第一个间隔】就是唯一捕获该阻塞的样本。丢掉它等于亲手
    抹掉证据 —— 阻塞型工具会以空 gaps 列表的形式"通过"检测。
    """

    def __init__(self) -> None:
        self.gaps: List[float] = []
        self._task: Optional[asyncio.Task] = None
        self._last: float = 0.0

    async def _beat(self) -> None:
        self._last = time.perf_counter()
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            now = time.perf_counter()
            self.gaps.append(now - self._last)
            self._last = now

    async def __aenter__(self) -> "_Heartbeat":
        self._task = asyncio.create_task(self._beat())
        # 让出一次，确保心跳已落下基准时间戳并入睡，否则首个间隔无基准可比
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_: Any) -> bool:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return False

    @property
    def max_gap(self) -> float:
        return max(self.gaps) if self.gaps else 0.0


async def _drive(engine: Any, prompt: str) -> List[AgentEvent]:
    """完整消费一轮 run_turn 的事件流。"""
    events: List[AgentEvent] = []
    async for event in engine.run_turn(prompt):
        events.append(event)
    return events


def _engine_for(
    tool_name: str, tool_args: Dict[str, Any], final_text: str
) -> Tuple[Any, _ScriptedClient]:
    """
    造一个"脚本化"引擎：首轮要求调用 tool_name，次轮输出 final_text。

    【Why 返回 client 而非直接启动 patch】由调用方以 with / ExitStack 管理生命周期，
    保证异常路径下也能撤销 patch（零污染原则）。
    """
    from agent.core import AgentEngine

    engine = AgentEngine()
    rounds = [
        mock_llm_stream_tool_call(tool_name, tool_args),
        mock_llm_stream_final_text(final_text),
    ]
    return engine, _ScriptedClient(engine, rounds)


def _tool_contents(engine: Any) -> List[str]:
    """取出某引擎历史中所有 tool 回执的内容。"""
    return [m["content"] for m in engine.history if m.get("role") == "tool"]


def _event_blob(events: List[AgentEvent]) -> str:
    """
    把事件流序列化成可做全文扫描的字符串。

    【Why 必须用全字段序列化而非只看 tool_name】已核实 AgentEvent 的 tool_status 事件
    并不携带 tool_name（core.py 只填了 status 文本），故 `{e.tool_name for e in events}`
    恒为空集 —— 那样的"隔离断言"是恒真的假断言。model_dump() 覆盖全部字段，使本断言
    真的能看见串扰。
    """
    return json.dumps([e.model_dump() for e in events], ensure_ascii=False)


# =====================================================================
# 3. 核心测试用例
# =====================================================================


async def _run_interleaving_probe(
    *, slow_is_async: bool
) -> Tuple[List[Tuple[str, float]], float, Any, Any]:
    """
    共用探针：A 会话跑 slow 工具（0.3s），B 会话在 20ms 后跑 fast_sync 工具。

    slow_is_async=True  -> slow 工具用 await asyncio.to_thread(time.sleep, ...)，合规
    slow_is_async=False -> slow 工具是 async def + time.sleep(...)，阻塞型反面教材
    """
    timeline: List[Tuple[str, float]] = []
    t0 = time.perf_counter()

    def mark(event_name: str) -> None:
        timeline.append((event_name, round((time.perf_counter() - t0) * 1000, 2)))

    async def slow_offloaded_tool() -> str:
        mark("slow_enter")
        await asyncio.to_thread(time.sleep, BLOCKING_TOOL_SLEEP_S)
        mark("slow_exit")
        return "slow done"

    async def slow_blocking_async_tool() -> str:
        """反面教材：async def 外形 + 阻塞调用 —— 事件循环的真凶。"""
        mark("slow_enter")
        time.sleep(BLOCKING_TOOL_SLEEP_S)
        mark("slow_exit")
        return "slow done"

    def fast_sync_tool() -> str:
        mark("fast_run")
        return "fast done"

    engine_a, client_a = _engine_for("probe_slow", {}, "A 完成")
    engine_b, client_b = _engine_for("probe_fast", {}, "B 完成")

    registered: Dict[str, Callable[..., Any]] = {
        "probe_slow": slow_offloaded_tool if slow_is_async else slow_blocking_async_tool,
        "probe_fast": fast_sync_tool,
    }

    async def side_b() -> List[AgentEvent]:
        await asyncio.sleep(0.02)
        return await _drive(engine_b, "B 提问")

    with _TempTools(registered):
        async with _Heartbeat() as hb:
            with client_a, client_b:
                await asyncio.gather(_drive(engine_a, "A 提问"), side_b())

    return timeline, hb.max_gap, engine_a, engine_b


async def test_event_loop_non_blocking_interleaving() -> None:
    """
    【核心断言】：to_thread 卸载后主事件循环非阻塞交错。

    Session A 调用 slow_offloaded_tool（to_thread 卸载 0.3s，模拟 get_git_diff 的
    subprocess.run），Session B 在 20ms 后调用 fast_sync_tool（~0ms，模拟 read_file）。
    两条独立证据：
      1. 时序偏序：Start(A) < Start(B) < Done(B) < Done(A)；
      2. 循环活性：心跳最大间隔远小于阻塞时长（直接测量，非由偏序推断）。
    """
    timeline, max_gap, engine_a, engine_b = await _run_interleaving_probe(slow_is_async=True)
    marks = dict(timeline)

    for key in ("slow_enter", "fast_run", "slow_exit"):
        assert key in marks, f"时序标记缺失 {key!r}，实际记录：{timeline}"

    elapsed = marks["slow_exit"] - marks["slow_enter"]
    assert elapsed >= BLOCKING_TOOL_SLEEP_S * 1000 * ELAPSED_FLOOR_RATIO, (
        f"slow 工具未真正耗时（仅 {elapsed:.1f}ms），探针空转，本用例不具鉴别力：{timeline}"
    )

    # 证据 1：时序偏序
    assert marks["slow_enter"] < marks["fast_run"], f"B 未在 A 之后启动：{timeline}"
    assert marks["fast_run"] < marks["slow_exit"], (
        f"B 未能抢占到 A 的等待窗口（被串行排队了）：{timeline}"
    )
    # B 不仅要插进来，而且要【及时】插进来：远早于 A 结束，而非等到 A 收尾才补跑
    assert marks["fast_run"] - marks["slow_enter"] < elapsed * 0.5, (
        f"B 的启动被拖到 A 的中后段，不像是真并发：{timeline}"
    )

    # 证据 2：循环活性
    assert max_gap < WATCHDOG_GAP_LIMIT_S, (
        f"[事件循环被阻塞] 心跳最大间隔 {max_gap * 1000:.1f}ms 超过阈值 "
        f"{WATCHDOG_GAP_LIMIT_S * 1000:.0f}ms —— to_thread 卸载未能让出主循环。"
        f"全程时标：{timeline}"
    )

    # 顺带覆盖：A、B 各自只持有自己的回执（同轮并发下的基本隔离）
    assert _tool_contents(engine_a) == ["slow done"], _tool_contents(engine_a)
    assert _tool_contents(engine_b) == ["fast done"], _tool_contents(engine_b)


async def test_watchdog_detects_blocking_async_tool() -> None:
    """
    自校验 + 危害实测：把 slow 工具换成【阻塞型 async 工具】，看门狗必须报警。

    【Why 这条必须存在】否则上一条用例的 max_gap 断言无法证明自己有鉴别力 —— 若阈值
    被误设得过宽（例如 10s），它会对任何实现都恒真地通过。本用例用同一个探测器去测
    一个【已知有害】的实现，把"探测器能发现阻塞"变成可执行断言。

    这同时是考核题 1（async def + time.sleep）的实测证据：它在源码外形上与合规工具
    完全一致，唯一的区别只有探测器看得见。
    """
    timeline, max_gap, _, _ = await _run_interleaving_probe(slow_is_async=False)
    marks = dict(timeline)

    elapsed = marks["slow_exit"] - marks["slow_enter"]
    assert elapsed >= BLOCKING_TOOL_SLEEP_S * 1000 * ELAPSED_FLOOR_RATIO, (
        f"阻塞工具未真正阻塞（{elapsed:.1f}ms），本用例失去意义：{timeline}"
    )

    # (a) 探测器有效性：它确实看见了阻塞
    assert max_gap >= WATCHDOG_GAP_LIMIT_S, (
        f"[探测器失效] 阻塞型 async 工具的 max_gap 仅 {max_gap * 1000:.1f}ms，未超过阈值 "
        f"{WATCHDOG_GAP_LIMIT_S * 1000:.0f}ms —— 看门狗识别不出阻塞，"
        f"故 test_event_loop_non_blocking_interleaving 的断言不具鉴别力。"
    )

    # (b) 危害实测：B 的 20ms 定时器被 A 的阻塞绑架，只能等 A 让出循环后才被处理。
    #     这才是"单个 async def + time.sleep"对整套并发服务的真实杀伤。
    assert marks["fast_run"] >= marks["slow_enter"] + elapsed * ELAPSED_FLOOR_RATIO, (
        f"[危害确认] B 竟在 A 阻塞期间就跑起来了（fast_run={marks['fast_run']}ms，"
        f"slow_enter={marks['slow_enter']}ms，阻塞时长={elapsed:.1f}ms）："
        f"说明 loop 未被独占，本用例前提不成立。{timeline}"
    )


async def test_mixed_shape_dispatch_under_gather() -> None:
    """
    双模派发并发压测：同步与异步工具在同一 gather 里竞争，且无协程漏 await。

    【覆盖什么】inspect.isawaitable 分发在并发竞争下不得漏 await —— 若漏了，Python 只在
    协程被 GC 时抛 RuntimeWarning("coroutine ... was never awaited")，既不影响返回值也
    不影响退出码，是典型的静默退化。故此处显式捕获警告并断言其不存在。
    """

    def _never_awaited(caught: List[Any]) -> List[str]:
        return [str(w.message) for w in caught if "never awaited" in str(w.message)]

    # (a) 前置自检：先确认本机 Python 确实会为漏 await 的协程发警告，否则下面的断言是
    #     恒真的（探测器坏掉时，"没检测到泄漏"毫无信息量）。
    with warnings.catch_warnings(record=True) as self_check:
        warnings.simplefilter("always")

        async def _deliberately_leaked() -> None:
            return None

        leaked = _deliberately_leaked()
        del leaked
        gc.collect()

    assert _never_awaited(self_check), (
        "前置自检失败：本环境不会为未 await 的协程发出 RuntimeWarning，"
        "故本用例的泄漏断言在此环境下恒真、不具鉴别力。"
    )

    # (b) 正式测量：4 组（async 工具 + sync 工具）在同一 gather 中并发
    cases: List[Tuple[Any, _ScriptedClient, str]] = []
    registered: Dict[str, Callable[..., Any]] = {}

    for i in range(4):
        async_name, sync_name = f"mix_async_{i}", f"mix_sync_{i}"

        def make_async(tag: int) -> Callable[..., Any]:
            async def _tool() -> str:
                await asyncio.to_thread(time.sleep, 0.05)
                return f"async-{tag}"
            return _tool

        def make_sync(tag: int) -> Callable[..., Any]:
            def _tool() -> str:
                return f"sync-{tag}"
            return _tool

        registered[async_name] = make_async(i)
        registered[sync_name] = make_sync(i)
        engine_a, client_a = _engine_for(async_name, {}, f"fin-{i}")
        engine_s, client_s = _engine_for(sync_name, {}, f"fin-{i}")
        cases.append((engine_a, client_a, f"async-{i}"))
        cases.append((engine_s, client_s, f"sync-{i}"))

    async def drive(engine: Any) -> List[AgentEvent]:
        return await _drive(engine, "提问")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with _TempTools(registered), ExitStack() as stack:
            for _, client, _ in cases:
                stack.enter_context(client)
            await asyncio.gather(*[drive(engine) for engine, _, _ in cases])
        gc.collect()  # 促使未 await 的协程被回收并触发 RuntimeWarning

    leaked = _never_awaited(caught)
    assert not leaked, f"[协程泄漏] 派发层漏掉了 await，出现 {len(leaked)} 处未等待的协程：{leaked}"

    # (c) 每个引擎都必须持有【真实返回值的字符串】，而非协程的内存地址投影
    for idx, (engine, _, expected) in enumerate(cases):
        contents = _tool_contents(engine)
        assert contents == [expected], (
            f"引擎 #{idx} 回执异常（期望 {expected!r}，实际 {contents!r}）——"
            f"若值形如 '<coroutine object ...>' 即为静默失效。"
        )


async def test_multi_session_state_isolation() -> None:
    """
    【核心断言】：5 个 Session 高并发状态隔离（Zero-Crosstalk）。

    断言每个会话的历史深度、Tool Pair 原子性、消息序列自洽，且其历史与事件流的
    全字段序列化中不含任何其他会话的载荷。
    """
    registered: Dict[str, Callable[..., Any]] = {}
    triples: List[Tuple[Any, _ScriptedClient]] = []
    names = [f"iso_tool_{i}" for i in range(5)]

    for i, tool_name in enumerate(names):
        def make_tool(tag: int) -> Callable[..., Any]:
            def _tool() -> str:
                return f"payload-{tag}"
            return _tool

        registered[tool_name] = make_tool(i)
        # 最终文本也带专属标记，用于检测文本层面的串扰
        triples.append(_engine_for(tool_name, {}, f"final-{i}"))

    async def drive(engine: Any) -> List[AgentEvent]:
        return await _drive(engine, "提问")

    with _TempTools(registered), ExitStack() as stack:
        for _, client in triples:
            stack.enter_context(client)
        results = await asyncio.gather(*[drive(engine) for engine, _ in triples])

    for i, (engine, _) in enumerate(triples):
        history = engine.history

        # 1. 消息序列契约：user -> assistant(tool_calls) -> tool -> assistant(final)
        roles = [m["role"] for m in history]
        assert roles == ["user", "assistant", "tool", "assistant"], (
            f"会话 #{i} 的消息序列不符合 ReAct 契约：{roles}"
        )

        # 2. Tool Pair 原子性：assistant 的 tool_calls 与其 tool 回执必须成对
        tool_calls = history[1].get("tool_calls")
        assert tool_calls, f"会话 #{i} 的 assistant 帧缺少 tool_calls"
        call_id = tool_calls[0]["id"]
        assert history[2]["tool_call_id"] == call_id, (
            f"会话 #{i} 的 tool 回执未与 tool_calls 配对："
            f"{history[2]['tool_call_id']!r} != {call_id!r}"
        )

        # 3. 载荷隔离
        contents = _tool_contents(engine)
        assert contents == [f"payload-{i}"], f"会话 #{i} 收到他人回执：{contents}"
        assert history[3]["content"] == f"final-{i}", (
            f"会话 #{i} 的最终文本串扰：{history[3]['content']!r}"
        )

        # 4. 事件流隔离：全字段序列化中不得出现本会话以外的任何专属标记
        blob = _event_blob(results[i])
        #    4a. 非空性自检：自己的痕迹必须找得到，否则"找不到他人"是恒真的
        assert names[i] in blob, (
            f"会话 #{i} 的事件流中找不到自己的工具名 {names[i]!r} —— "
            f"隔离断言将退化为恒真。实际事件：{blob}"
        )
        for j, other in enumerate(names):
            if j == i:
                continue
            assert other not in blob, f"会话 #{i} 的事件流混入 {other!r}：{blob}"
            assert f"payload-{j}" not in blob, f"会话 #{i} 的事件流混入 payload-{j}：{blob}"
            assert f"final-{j}" not in blob, f"会话 #{i} 的事件流混入 final-{j}：{blob}"

        assert results[i][-1].type == "done", f"会话 #{i} 未以 done 收尾"


# =====================================================================
# 4. 自运行测试主入口
# =====================================================================


def _warm_up() -> None:
    """
    预热进程级一次性初始化成本，使各用例度量的是稳态行为。

    【Why 必须显式预热，而不是靠"跑到第二个用例时自然就热了"】
    见模块头部的实测发现：openai 的 .chat.completions 首次访问要同步导入约 296ms 的
    模块树，而该导入发生在事件循环内部。若不预热，第一个用例必然测到这段进程级成本，
    得到一个与被测代码无关的失败；而第二个用例又会通过 —— 测试结果取决于执行顺序，
    这是最坏的一种不确定性。显式预热把这份成本【挪出被测区间】并让它可见，而不是
    悄悄放宽阈值把它掩盖掉。
    """
    from agent.core import AgentEngine

    engine = AgentEngine()
    # 1. openai SDK 惰性资源导入（首次 ~296ms）+ 填充两个 cached_property
    _ = engine.client.chat.completions
    # 2. core.py:123 的延迟导入（破环点，冷启动实测 ~757ms）
    from agent.session import approval_manager  # noqa: F401
    # 3. 上下文裁剪器与 pydantic 事件模型的首次调用路径
    engine.context_manager.truncate_history([{"role": "user", "content": "warm-up"}])
    AgentEvent(type="done")


def main() -> int:
    print("=" * 62)
    print(">>> 并发与事件循环调度离线回归测试 (Zero-API Cost)")
    print("=" * 62)

    cases: List[Tuple[str, Callable[[], Any]]] = [
        ("事件循环非阻塞交错（心跳活性 + 时序偏序）", test_event_loop_non_blocking_interleaving),
        ("看门狗自校验：可识别阻塞型 async 工具", test_watchdog_detects_blocking_async_tool),
        ("双模派发并发压测：无协程漏 await", test_mixed_shape_dispatch_under_gather),
        ("5 会话并发状态与事件流物理隔离", test_multi_session_state_isolation),
    ]

    failed: List[str] = []
    with _OfflineCredentials():
        _warm_up()
        print("  [WARM] 已预热进程级惰性导入，以下度量均为稳态行为")
        for name, fn in cases:
            try:
                asyncio.run(fn())
                print(f"  [PASS] {name}")
            except AssertionError as exc:
                print(f"  [FAIL] {name}")
                print(f"         {exc}")
                failed.append(name)
            except Exception as exc:  # 非断言异常同样计失败，避免静默通过
                print(f"  [ERROR] {name}")
                print(f"          {type(exc).__name__}: {exc}")
                failed.append(name)

    # 零污染自检：套件跑完后注册表必须已复原
    assert TOOL_REGISTRY is CORE_TOOL_REGISTRY, "注册表别名被破坏"
    residue = [n for n in TOOL_REGISTRY if n.startswith(("probe_", "mix_", "iso_tool_"))]
    assert not residue, f"[污染] 测试工具残留在 TOOL_REGISTRY 中：{residue}"

    print("-" * 62)
    if failed:
        print(f">>> {len(failed)}/{len(cases)} 个用例失败：{failed}")
        return 1
    print(f">>> 全部 {len(cases)} 个用例通过：事件循环零阻塞，多会话状态物理隔离。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
