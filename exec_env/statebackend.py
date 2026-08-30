from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.messages import HumanMessage

from chat_models.chat import chat_model

agent = create_deep_agent(
    model=chat_model,
    backend=StateBackend(),
)


def main() -> None:
    while True:
        question = input("请输入问题(输入 exit 退出): ")
        if question.lower() == "exit":
            break
        result = agent.invoke({"messages": [HumanMessage(content=question)]})

        # 对模型的输出结果进行处理，提取出文本内容
        if isinstance(result, dict) and "text" in result:
            print(result["text"])
        else:
            print(result)


if __name__ == "__main__":
    main()
