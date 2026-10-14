# TransitMind

TransitMind 是一个面向交通数据的、可解释的 Text2SQL Agent 实现。项目使用普通 Python 代码实现 Agent loop、工具调用、上下文组装和受控 SQL 执行，重点是让每一步都能被追踪和解释。

项目不依赖 LangGraph、LangChain 等 Agent 编排框架。模型负责理解问题和选择工具，Agent 负责维护消息、执行工具、把结果送回模型，并在达到完成条件时结束本轮任务。

## 主要能力

- 自定义 Agent loop：支持多轮模型请求、tool calling、工具结果回传、轮次上限和事件 trace。
- PostgreSQL Text2SQL：从数据库元数据中获取表结构，由模型生成 SQL，再交给受控 SQL 工具执行。
- SQL 治理：只允许读取查询，限制可访问的表、关联关系、结果行数、查询超时和 SQL 结构。
- 多层记忆：保存会话上下文、人工认可的查询经验和版本化 Schema 快照。
- Schema 检索：支持关键词、向量、Neo4j 图检索，以及使用 Reciprocal Rank Fusion 的混合检索。
- GTFS 数据接入：提供交通数据文件盘点和通用表导入流程；实际业务数据保留在外部数据库中，不随仓库提交。
- 可观测性基础：保留模型请求、工具请求、工具结果和停止原因，可将完整 trace 保存为 JSON。

## 工作流

```text
用户问题
  -> Agent 组装上下文
  -> 模型选择 schema / describe / run_sql 工具
  -> 工具访问 Schema 或只读 PostgreSQL
  -> 工具结果回到模型
  -> Agent 生成最终回答并保存 trace
```

## 代码结构

```text
transitmind/
  agent.py              Agent loop 和轮次控制
  models.py             消息、工具调用和运行结果模型
  tools.py              工具定义、注册和执行
  deepseek_model.py     OpenAI 兼容接口的模型适配器
  postgres_schema.py    PostgreSQL Schema 工具
  run_sql.py            只读 SQL 执行和治理
  schema_memory.py      Schema 快照与检索工具
  schema_vectors.py     本地向量索引
  schema_graph.py       Neo4j Schema 图检索
  schema_hybrid.py      向量与图的 RRF 混合检索
  experience_memory.py  查询经验保存与召回
  conversation_store.py 会话保存与加载
  gtfs_inventory.py     GTFS 文件盘点
  gtfs_import.py        GTFS 表导入
```

## 快速开始

项目要求 Python 3.11 或更高版本。先创建虚拟环境并安装基础依赖：

```powershell
py -3.12 -m venv .venv
uv pip install --python .venv/Scripts/python.exe -e .
```

复制 `.env.example` 为 `.env`，只在本机填写 API、数据库和 Neo4j 凭据。`.env` 已被 Git 忽略，不应提交真实密钥。

运行本地脚本模型演示：

```powershell
.\.venv\Scripts\python.exe -m transitmind
```

使用真实模型和只读 PostgreSQL：

```powershell
.\.venv\Scripts\python.exe -m transitmind --live --database --sql --question "请列出数据库中的交通机构"
```

保存一次完整运行 trace：

```powershell
.\.venv\Scripts\python.exe -m transitmind --live --database --sql --trace-file .\artifacts\trace.json --question "请列出数据库中的交通机构"
```

向量和图检索是可选能力，分别安装对应依赖：

```powershell
uv pip install --python .venv/Scripts/python.exe -e ".[vector,graph]"
```

## 数据与数据库

仓库不包含 MBTA 原始文件、业务数据库、向量模型缓存或运行结果。TransitMind 通过配置连接外部 PostgreSQL 数据库；数据库中可以放置 GTFS 或其他交通业务表，只要 Schema 工具能够读取其表结构，并在 SQL 治理规则中配置允许的访问范围。

建议使用独立的只读账号运行 Agent 查询。数据导入账号和 Agent 查询账号分开，避免模型生成的内容获得建表、写入或删除权限。

## 安全边界

- 不把 API key、数据库密码、Neo4j 密码提交到 Git。
- Agent 查询连接使用只读数据库账号。
- SQL 工具只开放受控的 `SELECT` 查询，不把任意 Cypher 或写操作交给模型。
- 原始数据、模型缓存、会话记录、审计产物和本地配置均不进入公开仓库。

## 项目状态

这是一个持续迭代的工程项目，当前重点是把 Text2SQL Agent 的核心链路拆开实现并保持可观察。后续可以继续完善 SQL 失败恢复、评测闭环、更多数据库适配和 Web 界面。
