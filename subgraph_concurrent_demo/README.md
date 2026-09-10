# Concurrent LangGraph Subgraph Demo

这是一个真实调用大模型的并发子图示例。父图直接挂载五个编译后的子图，交通、住宿和活动三个专家子图通过异步扇出并行执行；每个专家取得候选项后都会通过 `from chat_models.chat import chat_model` 调用项目已配置的模型选择方案，随后由汇总子图显式 join。

详细架构、状态边界和设计理由见 [design.md](design.md)，完整 Mermaid 图见 [graph.mmd](graph.mmd)。

## 运行

```bash
uv run python -m subgraph_concurrent_demo.main
```

模拟一个或多个服务失败：

```bash
uv run python -m subgraph_concurrent_demo.main --fail hotel
uv run python -m subgraph_concurrent_demo.main --fail hotel --fail activity
```

程序会先打印父图，然后输出包含子图 namespace 的实时更新。默认延迟下，三个专家会几乎同时开始，并按照活动、交通、住宿的顺序完成。

运行前请确保项目根目录 `.env` 已配置 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `MODEL`。一次正常运行会在三个专家分支中分别执行一次真实模型调用。

## 测试

```bash
uv run python -m unittest subgraph_concurrent_demo.test_demo -v
```

单元测试会注入本地确定性选择器，不会调用模型或消耗接口额度。测试覆盖：

- 使用异步 Barrier 证明三个专家服务真实重叠执行；
- reducer 合并三个子图的并发输出；
- 子图私有状态不会进入父图；
- join 后结果使用固定业务顺序；
- 单分支失败时局部降级。
