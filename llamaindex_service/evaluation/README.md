# 检索评估与并发探针

本目录提供可重复运行的 HTTP 评估入口。内置数据是 **4 份虚构文档、20 个中英文问题**，其中 16 个有答案、4 个无答案。它用于开发冒烟检查，不替代方案要求的 100 条真实业务脱敏标注，也不能据此宣称达到生产质量或容量门槛。

## 本轮已测结果

已在独立的临时 PostgreSQL 数据库上运行真实 API、pgvector 查询与摄取 Worker。模型使用 64 维哈希向量和明确标记的开发摘录器；API 通过进程内 ASGI 调用，没有 TCP 网络开销。测试结束后已删除该临时数据库，本地完整 JSON 保存在被 Git 忽略的 `evaluation/results/synthetic-smoke.json`。

| 项目 | 实测 |
| --- | --- |
| 文档 / 问题 | 4 份 / 20 条中英文合成问题 |
| 检索请求错误 | 0 |
| 有答案问题 Recall@3 / MRR | 1.0 / 0.9375 |
| 无答案状态正确率 | 0.25，即 4 条中仅 1 条正确拒答 |
| 并发检索探针 | 总计 20 次、并发 4、20 次成功、0 次失败 |
| 探针成功请求 p95 | 42.267 ms；仅限该小样本和进程内调用环境 |

无答案结果说明开发摘录器不能承担语义问答验收。较高的合成检索指标也不说明真实模型质量已经达标；这里没有测量真实 Embedding 的语义能力、答案事实支持率、引用语义正确率或百万片段容量。

## 运行样例评估

先启动服务 API 和独立 Worker，使用同一数据库、存储与模型配置。以下命令创建一个独立样例知识库，上传四份文档，等待持久化任务全部完成，再计算检索指标：

```bash
uv run --group rag python -m llamaindex_service.evaluation.evaluate \
  --base-url http://127.0.0.1:8000 --seed --top-k 3 \
  --output /tmp/rag-smoke-report.json
```

`--seed` 创建的知识库会保留，报告包含知识库、文档、版本和任务 ID，可用已有知识库继续评估：

```bash
uv run --group rag python -m llamaindex_service.evaluation.evaluate \
  --knowledge-base-id YOUR_KB_ID --top-k 3 \
  --check-no-answer --output /tmp/rag-smoke-report.json
```

`--check-no-answer` 会额外对 4 个无答案问题调用问答 API。真实模型可能产生调用费用；默认只调用检索接口。`model_mode=mock` 的结果只说明数据流程和评分入口可以运行，哈希向量和摘录器不具备语义理解或可靠拒答能力。失败请求计入召回和拒答指标的分母，同时单独报告错误数；不会通过排除失败来提高分数。

JWT 模式可设置环境变量 `RAG_AUTH_TOKEN`；报告不会包含此凭证。`--base-url` 可更换服务地址，`--wait-seconds` 调整摄取等待时间。生产部署应使用专用测试租户，不把内置虚构文档加入正式知识库。

## 指标与标注

每行 JSONL 包含 `id`、`query`、`answerable`、`expected_sources` 和可选 `split`。`expected_sources` 是相关文档的原始文件名数组；文件名需在评估范围内唯一。一个问题依赖多份文档时列出所有相关文件。无答案样本使用 `answerable=false` 和空数组。

- Recall@k：本次返回的前 k 个片段中，找到的相关文档数除以标注相关文档数，然后对有答案问题取平均。
- MRR：前 k 个片段按文档去重后，第一个相关文档名次的倒数，再对有答案问题取平均。不会通过去重引入第 k 个片段之后的来源。
- 无答案正确率：选择 `--check-no-answer` 后，服务最终状态为 `no_answer` 的比例。不执行额外语义评审。
- p50/p95：成功检索请求从客户端发出到响应完成的耗时，采用 nearest-rank 百分位，包括查询向量化与可选重排。错误数量单独列出。

报告记录数据集 SHA-256、种子文档 SHA-256、模型运行模式、实际索引代次及逐问题结果。使用真实标注时以 `--dataset /path/to/questions.jsonl` 指定文件；调参集与保留测试集应分开运行，不能用内置冒烟样本作为质量验收集。此入口没有实现事实支持率或引用语义正确率评判，需要按方案人工标注与抽检。

## 并发检索探针

```bash
uv run --group rag python -m llamaindex_service.evaluation.load_test \
  --knowledge-base-id YOUR_KB_ID --requests 50 --concurrency 5 \
  --query 'Atlas 默认超时和重试次数是多少？' \
  --output /tmp/rag-load-report.json
```

探针不会调用答案生成接口；真实 Embedding 和 Reranker 仍可能产生费用。报告区分成功数、429 等 HTTP 错误、p50/p95、最大延迟和成功吞吐量。单次延迟不包括客户端并发队列等待。默认不预热，请自行区分冷启动与预热后的测量，并记录硬件、数据量、数据库配置及模型部署信息。这些结果不能外推为百万片段或生产 SLO 的证明。

离线校验本目录的评分、样例标注、HTTP 编排及并发边界：

```bash
uv run --group rag python -m unittest llamaindex_service.evaluation.test_evaluation -v
```
