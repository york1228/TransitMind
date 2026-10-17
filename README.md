# TransitMind

TransitMind 是一个面向交通数据的、可解释的 Text2SQL Agent 实现。项目使用普通 Python 代码实现 Agent loop、工具调用、上下文组装和受控 SQL 执行，重点是让每一步都能被追踪和解释。

项目不依赖 LangGraph、LangChain 等 Agent 编排框架。模型负责理解问题和选择工具，Agent 负责维护消息、执行工具、把结果送回模型，并在达到完成条件时结束本轮任务。

## 主要能力

- 自定义 Agent loop：支持多轮模型请求、tool calling、工具结果回传、轮次上限、工具与 SQL 尝试额度和事件 trace。
- PostgreSQL Text2SQL：从数据库元数据中获取表结构，由模型生成 SQL，再交给受控 SQL 工具执行。
- SQL 治理：只允许读取查询，限制可访问的表、关联关系、结果行数、查询超时和 SQL 结构。
- SQL 恢复决策：根据失败错误码允许模型修正 SQL，或由程序结束当前运行；处理决定保存在 trace 并显示于网页。
- 多层记忆：保存会话上下文、人工认可的查询经验和版本化 Schema 快照。
- Schema 检索：支持关键词、向量、Neo4j 图检索、使用 Reciprocal Rank Fusion 的混合检索，以及从已知表名出发的定向扩展工具。
- GTFS 数据接入：提供交通数据文件盘点和通用表导入流程；实际业务数据保留在外部数据库中，不随仓库提交。
- 可观测性基础：保留模型请求、工具请求、工具结果、模型与工具耗时、API Token 用量和停止原因，可将完整 trace 保存为 JSON。
- 本地工作台：在网页提交问题，分别查看回答、数据库结果、Schema 候选与每轮工具调用；支持回看现有 trace。

## 工作流

```text
CLI / FastAPI 接收问题
  -> application.execute_query 组装 Agent、准备可选历史与经验
  -> 模型选择 schema / describe / run_sql 工具
  -> 工具访问 Schema 或只读 PostgreSQL
  -> 工具结果回到模型
  -> application 封装运行结果并释放本次客户端
  -> CLI / FastAPI 输出结果并保存各自的 trace 文件
```

## 代码结构

```text
transitmind/
  agent.py              Agent loop、轮次与工具执行预算
  application.py        一次查询的上下文、执行、资源释放和公共记录
  runtime.py            终端与网页共用的 Agent 组装
  web.py                FastAPI 本地工作台接口
  static/               查询与调试页面、本地浏览器资源
  models.py             消息、工具调用和运行结果模型
  tools.py              工具定义、注册和执行
  deepseek_model.py     OpenAI 兼容接口的模型适配器
  database_config.py    公共 PostgreSQL 配置和连接常量
  postgres_check.py     只读连接检查命令与兼容导出
  postgres_schema.py    PostgreSQL Schema 工具
  run_sql.py            只读 SQL 执行和治理
  sql_policy.py         查询授权、结果限制、超时及共用规则说明
  sql_recovery.py       SQL 失败后的修正或停止决定
  schema_store.py       Schema 快照读写与原存储路径
  schema_memory.py      Schema 采集与检索工具
  resources/            随包安装的默认 MBTA 业务说明
  schema_vectors.py     本地向量索引
  schema_graph.py       Neo4j Schema 图检索
  schema_hybrid.py      向量与图的 RRF 混合检索
  schema_expand.py      已知表的一层图邻居扩展
  experience_memory.py  查询经验保存与召回
  conversation_store.py 会话保存与加载
  gtfs_inventory.py     GTFS 文件盘点
  gtfs_import.py        GTFS 表导入
```

CLI 和 FastAPI 都调用 `application.execute_query()`；`runtime.build_agent()` 继续负责模型、工具和提示词的组装。会话历史与经验记忆由应用层按参数启用，网页默认保持独立提问。CLI 保留原来的参数、终端输出、JSON 字段与退出码，网页保留原有运行记录外层字段；两端都保留 Agent 的具体 `stop_reason`。客户端在应用层资源作用域退出时释放，Agent 实例不跨请求共享。

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

一次查询默认最多请求模型 4 轮、尝试工具 8 次，其中 `run_sql` 最多尝试 3 次。使用 `--max-tool-calls` 和 `--max-sql-attempts` 调整，0 禁止对应调用。SQL 同时占用两种额度，失败也计数；被预算拦截的调用不执行、不扣次数。用完额度后仍允许模型直接给出答案，只有下一次超额请求才分别以 `tool_call_limit` 或 `sql_attempt_limit` 结束，CLI 返回码为 2。

trace 的 `execution_budget` 保存上限和实际尝试次数；`tool_result` 事件的 `details.duration_ms` 表示注册表执行该工具的耗时，不包含模型请求。`run_stopped` 事件的 `details.blocked_call` 记录被拦截的第一个请求。预算中止时保留已有结果，当前批次剩余工具不执行，不再请求模型，未完成运行不追加到持久会话。

`run_sql` 失败时，Agent 根据现有错误码增加 `recovery`，同时写入模型可见的工具消息与 `tool_result` 事件。字段、表名、参数、语法、SQL 校验或查询超时错误允许模型在原预算内修正；权限、数据库范围、只读状态和其他未分类错误以 `sql_recovery_stopped` 结束，CLI 返回码为 2。停止事件的 `failure` 保存对应调用 ID、错误码和处理说明。允许修正不保证模型一定修正成功，当前没有连接故障自动重试。详细阅读路线见本地学习记录 `docs/lessons/08-sql-recovery.md`。

一次运行内，允许修正的失败请求会按完整 `run_sql` 参数记录；再次提交完全相同参数时，在执行前以 `repeated_failed_sql` 结束，CLI 返回码为 2。`blocked_call` 是本次未执行请求，`failure` 是原失败调用的诊断，重复请求不扣工具和 SQL 次数。字典键顺序、调用 ID 不影响比较，SQL 字符串不作大小写、空白或语义归一化；不同参数继续按原预算执行。成功调用不加入失败记录，失败记录不跨提问共享。预算检查优先于重复检查。

`model_response` 和 `model_error` 事件记录本轮 `duration_ms` 和 API 返回的 `usage`。公共结果中的 `token_usage` 汇总本次运行各轮的输入、输出和合计 Token，并保留已记录轮数；历史会话中的用量不重复累计。脚本模型或 API 未提供用量时保留为空，部分统计会标明覆盖轮数，失败模型轮次也计入覆盖轮数的分母。`duration_ms` 是一次查询的总耗时，包含上下文准备、Agent 执行、可选会话保存和资源释放；`model_duration_ms`、`tool_duration_ms` 分别累计模型请求和工具执行时间，不包含 HTTP 传输或页面渲染。

模型适配器抛出 `ModelCallError` 时，Agent 保留已有消息、工具结果和失败轮次，以 `stop_reason: error` 返回清理后的 `error` 诊断。CLI 先保存指定的 trace，再以退出码 1 报告错误；网页保存原有运行记录并展示已执行结果。API 已返回但因输出长度等原因被拒绝的回复，只保留实际用量和诊断，不接受为最终回答。初始化等尚未进入循环的错误仍沿用原错误摘要，当前不覆盖任意程序异常或进程终止。详细说明见本地学习记录 `docs/lessons/09-interrupted-trace.md`。

向量和图检索是可选能力，分别安装对应依赖：

```powershell
uv pip install --python .venv/Scripts/python.exe -e ".[vector,graph]"
```

已有 Schema 快照和同版本的 Neo4j 图时，可以观察已知表的邻居：

```powershell
.\.venv\Scripts\python.exe -m transitmind.schema_expand --tables routes
```

在真实 SQL 命令中增加 `--schema-memory --schema-expand`，即可让模型按需要调用定向扩展工具。它接收一到两个表名，返回最多三张表的结构和连接依据。

## 本地工作台

安装网页可选依赖并启动本机服务：

```powershell
uv pip install --python .venv/Scripts/python.exe -e ".[web]"
.\.venv\Scripts\python.exe -m transitmind.web --port 8767
```

浏览器打开 `http://127.0.0.1:8767`。页面支持提交独立问题、选择 Schema 检索与扩展、查看 SQL 和数据库结果、按轮次展开工具调用，以及回看已有 trace。运行记录保存在 `artifacts/runs/`。

查询区可设置工具与 SQL 上限；运行概要显示实际次数和额度，调试步骤显示工具耗时、停止原因及被拦截的请求。旧 trace 缺少这些字段时仍按原有信息展示。

运行概要下方显示总耗时、模型请求耗时、工具执行耗时、输入/输出/合计 Token；调试页每轮模型步骤显示该轮用量和耗时，模型适配器错误会显示为“模型请求失败”。Token 来自 API 实际返回，不按文字长度估算，也不换算费用。旧记录缺少的值显示“未记录”，只有重新运行后的记录才能获得新增统计。模型请求中断后，页面保留此前真实执行的 SQL、结果及已取得的用量；缺失的失败轮用量会标明部分统计。

Schema 页可浏览和人工维护说明、查看本地索引版本，也可对单表发起 AI 起草。模型建议先作为可编辑草稿展示，人工确认后才保存新版本；生成不读取业务数据行，不自动保存或重建索引。点击 AI 起草会请求已配置的 DeepSeek API。

Schema 同步默认通过 `importlib.resources` 读取包内的 `transitmind/resources/gtfs-schema-notes.json`，打包时包含该文件，运行时不依赖 `docs`。原同步命令保持可用；指定外部业务说明可用 `python -m transitmind.schema_memory sync --notes <文件路径>`。`docs/examples/gtfs-schema-notes.json` 保留为学习示例，页面保存的人工说明继续优先于默认值。

打开页面不自动发起模型或数据库查询。提交问题时使用真实 DeepSeek 和现有只读 PostgreSQL 工具，可能产生 API 费用。执行完成后展示过程；当前网页尚未接入连续会话和实时事件，但支持 Schema 说明编辑与 AI 草稿。

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
