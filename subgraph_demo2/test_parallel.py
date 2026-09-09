"""Test two private subgraphs running from the same parent fan-out."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class PrivateState(TypedDict, total=False):
    input_text: str
    private_result: str


class ParentState(TypedDict, total=False):
    input_text: str
    result_a: str
    result_b: str
    final: str


def make_private_subgraph(label: str):
    def process(state: PrivateState) -> dict[str, str]:
        return {"private_result": f"{label}:{state['input_text']}"}

    builder = StateGraph(PrivateState)
    builder.add_node("process", process)
    builder.add_edge(START, "process")
    builder.add_edge("process", END)
    return builder.compile()


subgraph_a = make_private_subgraph("A")
subgraph_b = make_private_subgraph("B")


def run_a(state: ParentState) -> dict[str, str]:
    result = subgraph_a.invoke({"input_text": state["input_text"]})
    return {"result_a": result["private_result"]}


def run_b(state: ParentState) -> dict[str, str]:
    result = subgraph_b.invoke({"input_text": state["input_text"]})
    return {"result_b": result["private_result"]}


def combine(state: ParentState) -> dict[str, str]:
    return {"final": f"{state['result_a']} + {state['result_b']}"}


def build_test_graph():
    builder = StateGraph(ParentState)
    builder.add_node("subgraph_a", run_a)
    builder.add_node("subgraph_b", run_b)
    builder.add_node("combine", combine)
    # 同一个 START 扇出，LangGraph 会在同一 superstep 中调度两个节点。
    builder.add_edge(START, "subgraph_a")
    builder.add_edge(START, "subgraph_b")
    builder.add_edge("subgraph_a", "combine")
    builder.add_edge("subgraph_b", "combine")
    builder.add_edge("combine", END)
    return builder.compile()


def test_parallel_private_subgraphs_do_not_leak_state():
    result = build_test_graph().invoke({"input_text": "dinner"})

    assert result["final"] == "A:dinner + B:dinner"
    assert result["result_a"] == "A:dinner"
    assert result["result_b"] == "B:dinner"
    assert "private_result" not in result


if __name__ == "__main__":
    test_parallel_private_subgraphs_do_not_leak_state()
    print("parallel private subgraph test: passed")
