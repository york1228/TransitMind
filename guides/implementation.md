# 实现与进阶使用

[返回项目首页](../README.md)

本文保留运行参数、SQL 恢复、上下文、trace 和流式接口的实现说明。首次了解项目，可以先阅读首页，再按需要查阅这里的细节。命令均在项目根目录执行，以下示例使用 Windows PowerShell。

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
- 本地工作台：独立查询或按会话连续提问，查看实际携带的历史、回答、数据库结果、Schema 候选与每轮工具调用；支持回看现有 trace。

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
  evaluation.py         评测采集、结果比较、裁判评分和批次报告
  gtfs_inventory.py     GTFS 文件盘点
  gtfs_import.py        GTFS 表导入
evals/
  transitmind_v1.json   30 题冻结评测集
```

CLI 和 FastAPI 都调用 `application.execute_query()`；`runtime.build_agent()` 继续负责模型、工具和提示词的组装。会话历史与经验记忆由应用层按参数启用，网页默认保持独立提问。CLI 保留原来的参数、终端输出、JSON 字段与退出码，网页保留原有运行记录外层字段；两端都保留 Agent 的具体 `stop_reason`。客户端在应用层资源作用域退出时释放，Agent 实例不跨请求共享。

## 快速开始

项目要求 Python 3.11 或更高版本。先创建虚拟环境并安装基础依赖：

```powershell
py -3.12 -m venv .venv
uv pip install --python .venv/Scripts/python.exe -e .
```

复制 [`.env.example`](../.env.example) 为 `.env`，在本机填写 API 和数据库凭据；图检索另需 Neo4j 配置。`.env` 已被 Git 忽略，不应提交真实密钥。运行真实查询前，需要准备本机 `transitmind` 数据库、业务表和 `transitmind_reader` 只读账号，详见下方“数据与数据库”。

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

`run_sql` 失败时，Agent 根据现有错误码增加 `recovery`，同时写入模型可见的工具消息与 `tool_result` 事件。字段、表名、参数、语法、SQL 校验或查询超时错误允许模型在原预算内修正；权限、数据库范围、只读状态和其他未分类错误以 `sql_recovery_stopped` 结束，CLI 返回码为 2。停止事件的 `failure` 保存对应调用 ID、错误码和处理说明。允许修正不保证模型一定修正成功，当前没有连接故障自动重试。实现见 [sql_recovery.py](../transitmind/sql_recovery.py) 和 [agent.py](../transitmind/agent.py)。

一次运行内，允许修正的失败请求会按完整 `run_sql` 参数记录；再次提交完全相同参数时，在执行前以 `repeated_failed_sql` 结束，CLI 返回码为 2。`blocked_call` 是本次未执行请求，`failure` 是原失败调用的诊断，重复请求不扣工具和 SQL 次数。字典键顺序、调用 ID 不影响比较，SQL 字符串不作大小写、空白或语义归一化；不同参数继续按原预算执行。成功调用不加入失败记录，失败记录不跨提问共享。预算检查优先于重复检查。

`model_response` 和 `model_error` 事件记录本轮 `duration_ms` 和 API 返回的 `usage`。公共结果中的 `token_usage` 汇总本次运行各轮的输入、输出和合计 Token，并保留已记录轮数；历史会话中的用量不重复累计。脚本模型或 API 未提供用量时保留为空，部分统计会标明覆盖轮数，失败模型轮次也计入覆盖轮数的分母。`duration_ms` 是一次查询的总耗时，包含上下文准备、Agent 执行、可选会话保存和资源释放；`model_duration_ms`、`tool_duration_ms` 分别累计模型请求和工具执行时间，不包含 HTTP 传输或页面渲染。

模型适配器抛出 `ModelCallError` 时，Agent 保留已有消息、工具结果和失败轮次，以 `stop_reason: error` 返回清理后的 `error` 诊断。CLI 先保存指定的 trace，再以退出码 1 报告错误；网页保存原有运行记录并展示已执行结果。API 已返回但因输出长度等原因被拒绝的回复，只保留实际用量和诊断，不接受为最终回答。初始化等尚未进入循环的错误仍沿用原错误摘要，当前不覆盖任意程序异常或进程终止。实现见 [deepseek_model.py](../transitmind/deepseek_model.py) 和 [application.py](../transitmind/application.py)。

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

默认独立查询；开启“会话模式”后，连续提问使用同一个 `conversation_id`，左侧“新会话”生成新的 ID。FastAPI 将 ID 传给公共应用层，沿用最近 3 次完整提问的历史窗口；只有成功完成的本轮消息追加到 `data/conversations/{id}.json`。经验记忆仍默认关闭。页面的“本次会话上下文”显示实际携带的问答、原始消息、会话 ID、运行 ID、历史数量和保存状态。

每次提问仍创建新 Agent、新运行记录，工具/SQL 预算与耗时统计独立计算。历史中的旧 Token 统计不重复累加，但再次发送历史所产生的输入 Token 会计入本次 API 用量。刷新页面后默认回到独立查询，可在左侧打开已有会话运行记录，点击“继续此会话”读取最新保存的历史；这不是从某条旧记录分叉。外部打开的 JSON 只供回看。当前按单人本地顺序提问使用，没有同一会话跨标签页并发写入协调。

左侧按 `conversation_id` 和运行模式折叠同一会话，以第一条已保存提问作为标题，按最近活动排序；展开后按时间顺序查看每次提问各自的 SQL、状态和调试记录。组内的续问图标沿用“继续此会话”，读取最新保存的历史。没有会话 ID 的旧记录与独立查询仍单列，不按问题内容猜测归属。目录不再截取前 50 次运行，避免拆散同一会话；本地规模下读取全部轻量条目，完整 trace 仍在点选时载入。

“统计”页通过 `GET /api/statistics` 只读汇总 `artifacts/**/*.json` 中可识别的 Agent 运行记录，区分真实模型、教学脚本和模式未记录三组。展示正常完成次数与比例、每次运行的平均调用次数及耗时、已记录的 Token、各工具成功/失败次数和停止原因。正常完成率不代表答案正确率，也不是生产环境评测分数。独立 Schema 检索不计入；有 `run_id` 的重复导出只计一次，旧 CLI 无 ID 时按文件计数。

统计只读取本次 `events` 和公共运行汇总，不从历史 `messages` 重算工具次数或 Token。失败工具也计尝试，执行前被拦截的不计；每项均值只使用实际记录该指标的样本，并显示覆盖数量。缺失用量不是零，Token 合计只是已记录部分，不估算费用。打开或刷新统计不会发起模型、数据库查询，不会改写已有文件。实现见 [run_statistics.py](../transitmind/run_statistics.py)。

查询区可设置工具与 SQL 上限；运行概要显示实际次数和额度，调试步骤显示工具耗时、停止原因及被拦截的请求。旧 trace 缺少这些字段时仍按原有信息展示。

运行概要下方显示总耗时、模型请求耗时、工具执行耗时、输入/输出/合计 Token；调试页每轮模型步骤显示该轮用量和耗时，模型适配器错误会显示为“模型请求失败”。Token 来自 API 实际返回，不按文字长度估算，也不换算费用。旧记录缺少的值显示“未记录”，只有重新运行后的记录才能获得新增统计。模型请求中断后，页面保留此前真实执行的 SQL、结果及已取得的用量；缺失的失败轮用量会标明部分统计。

Schema 页可浏览和人工维护说明、查看本地索引版本，也可对单表发起 AI 起草。模型建议先作为可编辑草稿展示，人工确认后才保存新版本；生成不读取业务数据行，不自动保存或重建索引。点击 AI 起草会请求已配置的 DeepSeek API。

Schema 同步默认通过 `importlib.resources` 读取包内的 [gtfs-schema-notes.json](../transitmind/resources/gtfs-schema-notes.json)，打包时包含该文件。指定外部业务说明可用 `python -m transitmind.schema_memory sync --notes <文件路径>`，页面保存的人工说明优先于默认值。

网页提交问题使用 `POST /api/runs/stream`，通过 SSE 实时接收 `started`、`trace`、`text_delta` 和 `result`。Agent 每产生一条普通 trace 就发送一条事件，查询页显示进度、生成的 SQL 和已返回的数据库结果；调试页可查看正在发生的步骤。模型可见文字通过 `text_delta` 实时预览，完整消息、最终回答、耗时与 Token 汇总在 `result` 到达后展示，该包在运行记录保存后发送。原 `POST /api/runs` 仍返回整份 JSON，并与 SSE 共用执行和落盘逻辑。

输入区固定在工作区底部，查询设置收进可展开面板，上方结果区独立滚动。提交后立即清空输入框，问题保留在运行概要和记录中；流式更新或回看旧记录不会把问题重新填进输入框。运行时展开执行过程，完成后保留可再次展开的完整步骤，原有 trace 也可回看。停留在底部时跟随新内容，向上阅读时暂停跟随，可用输入区上方的向下箭头回到最新结果。

完成后的展示顺序为回答正文、执行过程、查询结果。执行过程按模型轮次折叠，同一轮的模型调用、工具执行和结束事件归在一起；默认展开最新一轮，手动开合状态在 SSE 更新时保留。步骤箭头位于左侧，标题、输入数量和耗时紧凑排列，窄屏自动换行。

每个步骤仍可单独展开：桌面宽度足够时，左侧主区显示模型返回的文字、工具请求，以及工具步骤的返回字段或 SQL 结果；右侧窄栏只放状态、耗时、Token、调用标识等辅助信息。窄区域自动上下排列。“本轮发送给模型的输入”默认折叠在模型步骤左侧下方，不与本轮工具输出左右对照。

输入消息中的工具结果按之前的 `tool_call_id` 配对工具名和参数，标题包含 `describe_table · stops` 等来源；展开后解码工具消息内的 JSON，复用字段表、查询预览和错误展示，原始消息另外折叠保留。这里显示的是实际发给模型的消息内容，不混入仅供页面保留的完整 SQL 结果。读取表结构的标题直接包含表名，例如“读取 trips 表结构”，其表名来自模型请求中的 `arguments.table`；缺少中文字段说明时显示“未提供”，不让模型猜测补充。这些详情与调试页共用，展开旧记录即可查看，无须重新调用模型，也不代表展示了模型内部推理。

输入规范在 `RunRequest` 统一处理，两个接口共用。浏览器用 `fetch` 提交 JSON 并读取事件流，以空行划分 SSE 帧，保留 UTF-8 解码状态，处理网络分块中的中文。事件来自实际执行，不是结束后回放。实现见 [web.py](../transitmind/web.py) 和 [app.js](../transitmind/static/app.js)。

网页模型调用使用 DeepSeek 的文字流，按模型轮次显示纯文本预览；最终完整结果替换为原 Markdown 回答。若该轮随后提出工具调用，预览文字归入模型步骤，不当成最终答案。工具参数按 index 收完整并通过原校验后才执行，不展示内部推理，不增加请求次数或预算。文字分片只实时转发，不写满持久 trace，完整消息照常保存，Token 只使用 API 实际 usage。CLI、普通 JSON 接口、Schema AI 起草及脚本模型保留原调用方式。

连接中断时保留已收到的 SQL 和结果，不自动重发查询；失败前的文字预览在当前页标为“未接受”，不保存为最终回答，也不写入会话。未接受的文字缓存不在重新载入失败记录时恢复。关闭页面不取消后台查询，可以稍后从运行记录查看已保存的结果。模型文字流实现见 [deepseek_model.py](../transitmind/deepseek_model.py)。

打开页面不自动发起模型或数据库查询。提交问题时使用真实 DeepSeek 和现有只读 PostgreSQL 工具，可能产生 API 费用。

## 数据与数据库

仓库不包含 MBTA 原始文件、业务数据库、向量模型缓存或运行结果。当前版本连接本机 `transitmind` PostgreSQL 数据库，并使用 `public` 下的七张 GTFS 表：`agency`、`routes`、`stops`、`trips`、`stop_times`、`calendar` 和 `calendar_dates`。

演示数据目录为 `data/raw/MBTA_GTFS/`。`agency_import.py` 导入机构表，`gtfs_import.py` 用 `TableSpec` 配置导入其余六张表；`--apply` 才会建表和写入。存在外键或前置表依赖，按 `agency -> routes / stops / calendar / calendar_dates -> trips -> stop_times` 的顺序准备。读取文件时要求列与当前表配置一致，不保证任意日期的 GTFS 文件都能原样导入。

建议使用独立的只读账号运行 Agent 查询。数据导入账号和 Agent 查询账号分开，避免模型生成的内容获得建表、写入或删除权限。

接入其他业务数据库需要修改 [database_config.py](../transitmind/database_config.py) 中的连接范围、[sql_policy.py](../transitmind/sql_policy.py) 中的授权表与关联关系，并准备对应的元数据说明和评测题。当前不是仅替换连接串即可接入任意数据库的通用产品。

## Schema 检索准备

完成数据库准备后，采集表结构到本地快照：

```powershell
.\.venv\Scripts\python.exe -m transitmind.schema_memory sync
```

关键词检索使用这份快照。向量检索需要安装 `vector` 依赖并构建本地索引；首次构建会下载 Embedding 模型：

```powershell
.\.venv\Scripts\python.exe -m transitmind.schema_vectors build
```

图检索需要安装 `graph` 依赖、启动配置中的 Neo4j 服务，再同步同一份 Schema 快照：

```powershell
.\.venv\Scripts\python.exe -m transitmind.schema_graph sync
```

混合检索同时使用向量索引和 Neo4j 图，两者必须与 Schema 快照版本一致。Neo4j 保存表、字段和外键等元数据，业务记录仍由 PostgreSQL 查询。当前向量索引使用本地 JSON 存储和余弦相似度，没有使用独立的向量数据库。

## 评测

[transitmind_v1.json](../evals/transitmind_v1.json) 包含 30 道冻结题：20 道可答题，以及 10 道澄清、拒绝、截断提示等行为题。题集记录了 Schema 版本与七张表的行数指纹，使用不同数据快照时需重新确认比较基准。

[evaluation.py](../transitmind/evaluation.py) 负责保存答卷、比较 SQL 执行结果、调用模型裁判并输出批次报告。程序比较和裁判评分分别保留；裁判异常、需要人工复核的比较与 Agent 答错分别记录。运行正常完成不代表回答正确，也不能用工具成功率替代 Text2SQL 准确率。

只读预检会连接 PostgreSQL，执行参考 SQL，不调用模型：

```powershell
.\.venv\Scripts\python.exe -m transitmind.evaluation
```

批量真实评测需要完整数据、Schema 快照、混合检索索引和模型配置。安装开发及检索依赖后，显式开启付费评测：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,vector,graph]"
$env:TRANSITMIND_LIVE_EVALS = "1"
.\.venv\Scripts\python.exe -m pytest -m eval --run-live-evals -s
```

这会产生 Agent 与模型裁判的 API 用量，批次记录保存在 `artifacts/eval/runs/`。默认 `pytest` 排除真实评测。单题 CLI 的 `--case` 会直接调用真实模型，不受 pytest 的显式开关控制，使用前应确认参数。

当前公开仓库提供题集与评测实现；具体准确率应结合相应批次报告、数据版本和人工复核结论阅读。

## 安全边界

- 不把 API key、数据库密码、Neo4j 密码提交到 Git。
- Agent 查询连接使用只读数据库账号。
- SQL 工具只开放受控的 `SELECT` 查询，不把任意 Cypher 或写操作交给模型。
- 原始数据、模型缓存、会话记录、审计产物和本地配置均不进入公开仓库。

## 项目状态

这是一个持续迭代的本地工程项目。已有自定义 Agent loop、SQL 治理与恢复、多层记忆、混合检索、Web 工作台和评测模块。后续可围绕评测中的失败案例改进检索与 SQL 生成，并逐步适配其他业务数据库和部署方式。
