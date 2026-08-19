"""LangGraph checkpoint time-travel demo: replay and fork."""

from typing_extensions import NotRequired, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class TravelState(TypedDict):
    topic: NotRequired[str]
    joke: NotRequired[str]


# 这个计数器不属于图状态，仅用于证明节点是否真的被重新执行。
call_counts: dict[str, int] = {
    "generate_topic": 0,
    "write_joke": 0,
}


def generate_topic(state: TravelState) -> dict[str, str]:
    del state
    call_counts["generate_topic"] += 1
    print(f"  执行 generate_topic，第 {call_counts['generate_topic']} 次")
    return {"topic": "程序员"}


def write_joke(state: TravelState) -> dict[str, str]:
    topic = state.get("topic")
    if topic is None:
        raise ValueError("write_joke 执行前必须先生成 topic")

    call_counts["write_joke"] += 1
    print(f"  执行 write_joke，第 {call_counts['write_joke']} 次")
    return {
        "joke": (
            f"[{topic}] 为什么分不清万圣节和圣诞节？"
            "因为 Oct 31 == Dec 25。"
        )
    }


def build_graph():
    builder = StateGraph(TravelState)
    builder.add_node("generate_topic", generate_topic)
    builder.add_node("write_joke", write_joke)
    builder.add_edge(START, "generate_topic")
    builder.add_edge("generate_topic", "write_joke")
    builder.add_edge("write_joke", END)

    checkpointer = InMemorySaver()
    return builder.compile(checkpointer=checkpointer)


def main() -> None:
    graph = build_graph()
    config: RunnableConfig = {
        "configurable": {
            "thread_id": "time-travel-demo",
        }
    }

    print("\n1. 首次执行完整图")
    original = graph.invoke({}, config=config)
    print("  最终状态：", original)

    print("\n2. 查看 checkpoint 历史（最新的在前）")
    history = list(graph.get_state_history(config))
    for snapshot in history:
        configurable = snapshot.config.get("configurable", {})
        checkpoint_id = configurable.get("checkpoint_id", "<missing>")
        metadata = snapshot.metadata or {}
        print(
            f"  step={str(metadata.get('step')):>2}",
            f"next={snapshot.next!s:<25}",
            f"checkpoint_id={checkpoint_id}",
        )
        print("    values=", snapshot.values)

    # 选择“下一步将执行 write_joke”的那个 checkpoint。
    before_write_joke = next(
        snapshot
        for snapshot in history
        if snapshot.next == ("write_joke",)
    )

    print("\n3. Replay：从 write_joke 执行前重新运行")
    replayed = graph.invoke(None, config=before_write_joke.config)
    print("  Replay 结果：", replayed)
    print("  调用次数：", call_counts)
    print("  generate_topic 没有重跑，write_joke 重新执行了一次。")

    print("\n4. Fork：从旧 checkpoint 修改 topic，创建新分支")
    fork_config = graph.update_state(
        before_write_joke.config,
        values={"topic": "产品经理"},
    )
    forked = graph.invoke(None, config=fork_config)
    print("  Fork 结果：", forked)
    print("  调用次数：", call_counts)

    print("\n5. 原始结果仍然存在")
    print("  Original：", original)
    print("  Fork：    ", forked)


if __name__ == "__main__":
    main()
