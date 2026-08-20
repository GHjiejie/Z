from typing import Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from typing_extensions import TypedDict


# 改法1：去掉 Annotated 和 add，每次直接覆盖新的 count 值
class State(TypedDict):
    count: int  # 初始值为0


# 注解添加 "lessfive"，因为路由里除了 END 还有跳回自身
# Command后面的 Literal 需要包含所有可能的 goto 值
def lessfive(state: State) -> Command[Literal["__end__", "lessfive"]]:
    count = state.get("count", 0) + 1

    if count < 5:
        print(f"  当前 count={count}，小于5，继续循环")
        return Command(
            update={"count": count},
            goto="lessfive",  # 小于5时，跳回当前节点继续循环
        )
    else:
        print(f"  当前 count={count}，大于等于5，结束图")
        return Command(
            update={"count": count},
            goto="__end__",  # 达到5时，结束图
        )


builder = StateGraph(State)
builder.add_node("lessfive", lessfive)
builder.add_edge(START, "lessfive")
# 去掉了 builder.add_edge("lessfive", "__end__") 因为在 Command 中已经动态控制了走向

graph = builder.compile(checkpointer=InMemorySaver())

config: RunnableConfig = {
    "configurable": {
        "thread_id": "user-001",
    }
}
result1 = graph.invoke(
    {"count": 0},
    config=config,
)

print("\n--- 最终结果 ---")
print(result1)  # 预期输出: {'count': 5}

cp_history = list(graph.get_state_history(config=config))
print("\n--- 历史状态 ---")
for i, state in enumerate(cp_history):
    print(f"Step {i}: {state}")
