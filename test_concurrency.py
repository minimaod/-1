# test_concurrency.py
import asyncio
import sys
from agent.core import AgentEngine


async def run_agent_task(task_id: str, prompt: str, engine: AgentEngine) -> None:
    print(f"\n[任务 {task_id} 启动] -> {prompt}\n")
    async for event in engine.run_turn(prompt):
        if event.type == "text":
            sys.stdout.write(f"[{task_id}:{event.delta}]")
            sys.stdout.flush()
    print(f"\n[任务 {task_id} 结束]\n")


async def main() -> None:
    engine_a = AgentEngine(model="deepseek-chat")
    engine_b = AgentEngine(model="deepseek-chat")

    print(">>> 启动双 Agent 并发交错吐字测试 <<<")
    await asyncio.gather(
        run_agent_task("A", "写一首关于秋天落叶的4句七言绝句", engine_a),
        run_agent_task("B", "列出5个Python关键字并用逗号隔开", engine_b),
    )


if __name__ == "__main__":
    asyncio.run(main())
