"""使用真实大模型的 LangGraph 子图（subgraph）学习示例。

在项目根目录运行：
    uv run python -m subgraph.main
"""

from typing import TypedDict

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from chat_models.chat import chat_model


class ParentState(TypedDict):
    """父图状态：只关心主题和最终学习笔记。"""

    topic: str
    learning_note: str


class ResearchSubgraphState(ParentState):
    """子图状态：research_result 是子图内部使用的私有字段。"""

    research_result: str


def message_to_text(message: AIMessage) -> str:
    """兼容字符串内容和 Responses API 的内容块。"""
    if isinstance(message.content, str):
        return message.content.strip()

    texts: list[str] = []
    for block in message.content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts).strip()


# ---------------------------------------------------------------------------
# 子图：将“调研”和“整理笔记”封装成一个可复用流程
# ---------------------------------------------------------------------------
def research_topic(state: ResearchSubgraphState) -> dict[str, str]:
    """子图节点 1：调用真实模型提炼主题的关键知识点。"""
    response = chat_model.invoke(
        [
            {
                "role": "system",
                "content": (
                    "你是一位资深 LangGraph 教师。请准确、简洁地提炼知识点，"
                    "使用中文回答，不要编造 API。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"请给出学习“{state['topic']}”最重要的 3 个知识点，"
                    "每点包含一句解释。"
                ),
            },
        ]
    )
    return {"research_result": message_to_text(response)}


def write_learning_note(state: ResearchSubgraphState) -> dict[str, str]:
    """子图节点 2：调用真实模型把调研结果整理成学习笔记。"""
    response = chat_model.invoke(
        [
            {
                "role": "system",
                "content": "你是一位技术写作助手，擅长把资料整理成简洁的中文学习笔记。",
            },
            {
                "role": "user",
                "content": (
                    f"主题：{state['topic']}\n\n"
                    f"参考资料：\n{state['research_result']}\n\n"
                    "请整理成包含标题、要点和一句总结的 Markdown 学习笔记。"
                ),
            },
        ]
    )
    # learning_note 是父图和子图共享的字段，会被写回父图。
    return {"learning_note": message_to_text(response)}


research_builder = StateGraph(ResearchSubgraphState)
research_builder.add_node("research_topic", research_topic)
research_builder.add_node("write_learning_note", write_learning_note)
research_builder.add_edge(START, "research_topic")
research_builder.add_edge("research_topic", "write_learning_note")
research_builder.add_edge("write_learning_note", END)
research_subgraph = research_builder.compile()


# ---------------------------------------------------------------------------
# 父图：准备主题，然后把完整任务交给子图
# ---------------------------------------------------------------------------
def normalize_topic(state: ParentState) -> dict[str, str]:
    """父图节点：清理用户输入。"""
    return {"topic": state["topic"].strip()}


parent_builder = StateGraph(ParentState)
parent_builder.add_node("normalize_topic", normalize_topic)
# 父图与子图共享 topic 和 learning_note，因此可直接将子图添加为节点。
parent_builder.add_node("research_subgraph", research_subgraph)
parent_builder.add_edge(START, "normalize_topic")
parent_builder.add_edge("normalize_topic", "research_subgraph")
parent_builder.add_edge("research_subgraph", END)
graph = parent_builder.compile()


def main() -> None:
    initial_state: ParentState = {
        "topic": "LangGraph 中的 Subgraph",
        "learning_note": "",
    }

    print("正在调用父图和研究子图……\n")
    result = graph.invoke(initial_state)
    print(result["learning_note"])


if __name__ == "__main__":
    main()
