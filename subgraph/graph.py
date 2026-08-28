"""Shared parent-graph and subgraph definitions for the streaming demos."""

from typing import TypedDict

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from chat_models.chat import chat_model


class ParentState(TypedDict):
    """State shared by the parent graph and the research subgraph."""

    topic: str
    learning_note: str


class ResearchSubgraphState(ParentState):
    """Subgraph state with one private intermediate research field."""

    research_result: str


def message_to_text(message: AIMessage) -> str:
    """Extract text from either string content or Responses API blocks."""

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
    return "".join(texts).strip()


def research_topic(state: ResearchSubgraphState) -> dict[str, str]:
    """Research three essential facts about the requested topic."""

    response = chat_model.invoke(
        [
            {
                "role": "system",
                "content": (
                    "You are an experienced LangGraph teacher. Explain concepts "
                    "accurately and concisely, and do not invent APIs."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Give the three most important facts for learning "
                    f"'{state['topic']}'. Explain each fact in one sentence."
                ),
            },
        ]
    )
    return {"research_result": message_to_text(response)}


def write_learning_note(state: ResearchSubgraphState) -> dict[str, str]:
    """Turn the research result into a concise Markdown learning note."""

    response = chat_model.invoke(
        [
            {
                "role": "system",
                "content": (
                    "You are a technical writing assistant. Produce concise "
                    "English learning notes."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Topic: {state['topic']}\n\n"
                    f"Research:\n{state['research_result']}\n\n"
                    "Create a Markdown learning note with a title, key points, "
                    "and a one-sentence summary."
                ),
            },
        ]
    )
    return {"learning_note": message_to_text(response)}


research_builder = StateGraph(ResearchSubgraphState)
research_builder.add_node("research_topic", research_topic)
research_builder.add_node("write_learning_note", write_learning_note)
research_builder.add_edge(START, "research_topic")
research_builder.add_edge("research_topic", "write_learning_note")
research_builder.add_edge("write_learning_note", END)
research_subgraph = research_builder.compile(name="research_subgraph")


def normalize_topic(state: ParentState) -> dict[str, str]:
    """Remove surrounding whitespace from the user's topic."""

    return {"topic": state["topic"].strip()}


parent_builder = StateGraph(ParentState)
parent_builder.add_node("normalize_topic", normalize_topic)
parent_builder.add_node("research_subgraph", research_subgraph)
parent_builder.add_edge(START, "normalize_topic")
parent_builder.add_edge("normalize_topic", "research_subgraph")
parent_builder.add_edge("research_subgraph", END)
graph = parent_builder.compile(name="learning_workflow")


def print_workflows() -> None:
    """Print the parent and child workflows before graph execution."""

    print("\nParent graph workflow")
    print("=====================")
    print(graph.get_graph().draw_ascii())
    print("Subgraph workflow")
    print("=================")
    print(research_subgraph.get_graph().draw_ascii(), flush=True)
