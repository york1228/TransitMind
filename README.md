# TransitMind

**用中文提问，查询交通数据库，并看清 SQL 是怎么生成和执行的。**

TransitMind 是一个面向公共交通数据的 Text2SQL Agent。它根据问题查找表结构、生成 SQL、查询 PostgreSQL，再把结果整理成回答。网页可以查看生成的 SQL、数据库结果和每轮工具调用，也支持连续追问。

Agent 的核心循环由 Python 直接实现。项目围绕 Schema 检索、查询安全、失败恢复和评测逐步迭代，当前使用 MBTA 公交与轨道交通计划数据作为演示场景。

## 可以问什么

- 每个交通机构有多少条线路？
- 计划班次最多的前 5 条线路是什么？
- 位于 Cambridge 的站点一共有多少个？

例如，提问“每个交通机构有多少条线路”，Agent 可以生成这样的查询：

```sql
SELECT a.agency_id, a.agency_name, COUNT(r.route_id) AS route_count
FROM public.agency a
LEFT JOIN public.routes r ON a.agency_id = r.agency_id
GROUP BY a.agency_id, a.agency_name
ORDER BY route_count DESC;
```

以上是查询示例。实际 SQL 由模型根据工具返回的表结构生成，回答以数据库执行结果为依据。

```text
用户提问 -> 检索表结构 -> 模型生成 SQL -> 程序校验与执行 -> 模型整理回答
                             ^                  |
                             +--- 错误反馈 ------+
```

## 核心能力

| 能力 | 在项目中做什么 |
| --- | --- |
| Agent 与工具调用 | 模型选择工具，Python 执行工具并回传结果；支持多轮处理和调用额度 |
| Schema 检索 | 用关键词、向量或图关系查找相关表，支持 RRF 混合检索和邻居扩展 |
| 多层记忆 | 分开管理会话历史、人工认可的查询经验和数据库结构 |
| SQL 治理与恢复 | 使用只读账号，校验 SQL 结构与授权范围；失败后按错误类型修正或停止 |
| Web 工作台 | 流式回答、SQL 与结果表、按轮次查看执行过程、会话续问、Schema 说明维护 |
| 追踪与评测 | 保存 JSON trace，统计耗时与 Token，并用冻结题集评估结果和行为 |

## 快速开始

以下命令在 Windows PowerShell 中执行，需要 Python 3.11 或更高版本。

### 1. 安装并体验基础循环

```powershell
git clone https://github.com/york1228/TransitMind.git
cd TransitMind
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[web]"
.\.venv\Scripts\python.exe -m transitmind
```

这一步使用**脚本模型和本地教学数据**，用于观察工具调用流程，无需 API Key 或数据库。

### 2. 准备真实模型与数据库

将 [`.env.example`](.env.example) 复制为 `.env`，填写 DeepSeek API Key 和 PostgreSQL 凭据。

当前真实查询需要本机 `transitmind` 数据库、七张 GTFS 业务表，以及 `transitmind_reader` 只读账号。数据准备和导入入口见[进阶文档](guides/implementation.md#数据与数据库)。原始数据、密码和运行结果不随仓库提供。

准备好数据库后，采集表结构，供工作台默认的关键词检索使用：

```powershell
.\.venv\Scripts\python.exe -m transitmind.schema_memory sync
```

基础查询使用 DeepSeek 和 PostgreSQL；选择向量、图或混合检索时，再按[检索准备说明](guides/implementation.md#schema-检索准备)安装依赖并构建索引。

### 3. 打开工作台

```powershell
.\.venv\Scripts\python.exe -m transitmind.web --port 8767
```

浏览器打开 [http://127.0.0.1:8767](http://127.0.0.1:8767)，即可提交问题、查看 SQL 与执行过程。真实模型查询会产生 API 用量。

也可以从终端提问并保存完整记录：

```powershell
.\.venv\Scripts\python.exe -m transitmind --live --database --sql --trace-file .\artifacts\trace.json --question "每个交通机构有多少条线路？"
```

## 技术与分工

| 技术 | 用途 |
| --- | --- |
| Python + DeepSeek Tool Calling | Agent 循环、工具选择与回答生成 |
| PostgreSQL + Psycopg | 存放并查询业务数据 |
| SQLGlot | 解析 SQL，检查授权范围与支持的语法 |
| FastEmbed + BGE | 将问题和表结构转成向量，进行相似度检索 |
| Neo4j | 保存表、字段和外键关系，辅助查找关联表 |
| FastAPI + SSE + 原生 HTML/CSS/JavaScript | 本地工作台与实时执行展示 |
| pytest + 自定义评测模块 | 批量采集答卷、比较执行结果和调用模型裁判 |

业务数据由 PostgreSQL 保存。Neo4j 保存结构关系；向量索引目前保存在本地 JSON 文件中。三者分别服务于数据查询和结构检索。

## 如何评测

仓库提供 [30 题冻结评测集](evals/transitmind_v1.json)，包括 20 道可答题和 10 道行为题，覆盖单表/多表查询、澄清、拒绝及结果截断提示等场景。

评测同时保留 SQL 执行结果比较、模型裁判判断和人工复核信息。每次运行按批次保存答卷与报告，便于对比改动前后的表现。具体命令与数据版本要求见[评测说明](guides/implementation.md#评测)。

## 当前范围

- 演示数据是 MBTA 的 GTFS 静态计划数据，可分析机构、线路、站点和计划班次；不包含实时延误或客流。
- SQL 目前限定七张 GTFS 表，支持最多三表、两次已授权关联，返回最多 100 行；模型接收前 12 行预览。
- 当前面向单人本地使用。接入其他业务库需要适配连接配置、表与关联授权、元数据说明和评测题。
- 运行统计中的完成率表示流程正常结束；SQL 与回答的正确性需结合评测报告判断。

## 进一步阅读

- [实现与进阶使用](guides/implementation.md)：运行参数、记忆、检索、恢复、流式接口及评测说明。
- [Agent 核心循环](transitmind/agent.py)：模型请求、工具执行、停止条件和错误反馈。
- [SQL 授权策略](transitmind/sql_policy.py)：允许访问的表、关联关系与执行限制。
- [评测实现](transitmind/evaluation.py)：答卷采集、结果比较与报告生成。
