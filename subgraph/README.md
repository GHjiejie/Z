# LangGraph Subgraph 真实模型 Demo

这个 demo 参考 [LangGraph 官方 Subgraphs 文档](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)，使用项目已有的真实模型：

```python
from chat_models.chat import chat_model
```

## 工作流

```text
父图：START -> normalize_topic -> research_subgraph -> END
                                  |
子图：                  research_topic
                                  |
                         write_learning_note
                                  |
                                 END
```

- `normalize_topic`：父图节点，清理输入主题。
- `research_topic`：子图节点，第一次调用模型，提炼 3 个知识点。
- `write_learning_note`：子图节点，第二次调用模型，将结果整理为 Markdown 笔记。
- `research_result`：只在子图内部流转的私有字段。
- `topic`、`learning_note`：父图与子图的共享字段。

父图与子图存在共享状态字段，因此编译后的子图可以直接作为父图节点：

```python
parent_builder.add_node("research_subgraph", research_subgraph)
```

## 运行

确保项目根目录的 `.env` 已配置：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=接口地址
MODEL=模型名称
```

然后在项目根目录执行：

```bash
uv run python -m subgraph.main
```

程序会真实调用模型两次，因此会产生相应的 API 用量。
