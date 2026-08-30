from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.messages import HumanMessage

from chat_models.chat import chat_model

path = "./deepagent/filesystem_data"

agent = create_deep_agent(
    model=chat_model, backend=FilesystemBackend(root_dir=path, virtual_mode=True)
)


def main() -> None:
    while True:
        question = input("请输入问题(输入 exit 退出): ")
        if question.lower() == "exit":
            break
        result = agent.invoke({"messages": [HumanMessage(content=question)]})

        print(result)


if __name__ == "__main__":
    main()
