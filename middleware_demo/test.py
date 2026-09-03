from langchain.agents import AgentState


class testState(AgentState):
    """
    A TypedDict representing the state of a test.
    """

    name: str
    status: str
    duration: float
