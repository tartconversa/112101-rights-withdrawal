# 内容授权覆盖事实库

本地可复现的 Python 数据湖，用于接收创作者、出版方和授权代理持续提交的权利主张，
对照内容版本与渠道使用记录，产出**可追溯的授权覆盖事实**。它回答的是：

- 某个发布版本在什么渠道、什么地区、什么期限内，目前由哪份授权凭证覆盖？
- 哪些实际使用还没有可核对的凭证？（缺口，不是侵权结论）
- 哪些版本无法关联到作品，或哪条使用明细与其他记录重复，因而不能计入可计费量？
- 授权补充、主体争议或撤回之后，**已经结算或尚未结算**的使用分别需要怎样复核？
- 上述结论来自哪一份原始记录、哪一行、何时收到并生效？

项目只依赖 Python 标准库和 SQLite，不需要网络或第三方包。

## 快速开始

```bash
# 所有命令都从项目根目录运行，先把 src/ 放进导入路径
export PYTHONPATH=src

# 1. 重建演示库（三个快照波次，体现撤回、补授、争议解除等变化）
python scripts/build_demo.py --reset

# 2. 查看最新快照的覆盖结论
python -m rights_facts --lake data/rights_lake.sqlite coverage
python -m rights_facts --lake data/rights_lake.sqlite review
python -m rights_facts --lake data/rights_lake.sqlite changes

# 3. 追溯单条使用：结论、凭证链（含原始文件与行号）、跨快照历史
python -m rights_facts --lake data/rights_lake.sqlite use U-63

# 4. 按作品核对主张、当前状态与实际使用
python -m rights_facts --lake data/rights_lake.sqlite work WORK-7

# 5. 运行测试
python -m unittest discover -s tests -v
```

`scripts/build_demo.py` 会自行调整导入路径；直接使用
`python -m rights_facts` 时，从项目根目录设置 `PYTHONPATH=src`
（PowerShell：`$env:PYTHONPATH = "src"`）即可，无需安装。

## 目录

| 路径 | 作用 |
| --- | --- |
| `sources/` | 当前基线快照的三份原始 CSV |
| `samples/scenario/wave-{a,b,c}/` | 三个按时间递进的快照，演示变化与追溯 |
| `src/rights_facts/sources.py` | 严格 CSV 解析、字段校验、快照加载 |
| `src/rights_facts/model.py` | 版本链、主张家族、当前状态解析 |
| `src/rights_facts/coverage.py` | 单条使用的确定性覆盖评估与证据链 |
| `src/rights_facts/store.py` | 追加式 SQLite 湖、变化事件、复核队列 |
| `src/rights_facts/report.py` | 人类可读与 JSON 报告 |
| `src/rights_facts/cli.py` | 命令行入口 |
| `scripts/build_demo.py` | 可复现地重建演示库 |
| `tests/` | 规则、来源校验、数据湖与端到端场景测试 |

## 源数据模型

三份 CSV 均以 `record_id` 作为**凭证行标识**，并保留物理行号用于追溯。

### `grants.csv`（权利主张与授权边界）

| 字段 | 说明 |
| --- | --- |
| `grant_id` | 一次授权/主张的**家族标识**；同一 `grant_id` 的多条记录是同一授权在不同时点的观察 |
| `claimant_id` | 授权主体 |
| `work_id` | 作品 |
| `channel` / `territory` | 渠道与地区，`*` 表示不限 |
| `valid_from` / `valid_to` | 生效边界，日期或带时区时间均可 |
| `grant_state` | `active` / `withdrawn` / `disputed` / `claimed` |
| `received_at` | 本记录到达权利系统的时点，带时区 |
| `supersedes_grant_id` | 本记录与另一授权家族的补充/替代关系（可为空） |
| `parent_claim_id` | 标记这是一项**主张**而非授权边界 |
| `coverage_scope` | `all-versions` / `original-only` / `derivatives-only` |
| `claim_scope_note` | 人工备注，原样保留 |

关键规则：**同一 `grant_id` 的最新 `received_at` 观察决定该家族当前状态**。
这使“先 active、后 withdrawn”的撤回能被正确表达；更早的观察保留为历史凭证，
而不是被删除。

### `content_versions.csv`（内容版本与衍生关系）

`version_id`、`work_id`、`parent_version`、`created_at`、`edit_kind`、
`channel_ref`、`source_ref`。`parent_version` 构成版本链；系统沿链回溯到原始版本，
检查链是否完整、是否跨越不同作品、是否成环。

### `uses.csv`（渠道使用）

`use_id`、`version_id`、`channel`、`territory`、`used_at`、`usage_units`、
`source_ref`、`reported_at`、`batch_ref`、`settlement_state`。
`settlement_state` 为 `unsettled` / `settled` / `void`。

## 覆盖判定规则

对每条使用，评估按以下顺序进行，任何一步无法确认都会得到**非判决性**结论：

1. **版本链**：使用引用的版本必须存在；衍生版本的父链必须完整、不跨作品、不成环。
   - 版本缺失 → `unresolved`
   - 父链不完整/成环 → `under_determination`
   - 父链引用不同作品 → `unresolved`（无法归属，不能默认归入任一授权）
2. **结构适用性**：作品、渠道、地区、期限、版本范围
   （`original-only` 不覆盖衍生版本，`derivatives-only` 反之）全部匹配。
   日期型边界按自然日包含首尾，不悄悄扩宽为时间戳。
3. **状态优先级**：在所有结构适用的记录中，**最新收到的记录**决定当前依据。
   因此只在 HK 撤回的授权不会抹掉仍覆盖 CN 的授权；后来到达的补充授权可以
   追溯覆盖早期使用；争议解除后的确认记录可以恢复覆盖。
4. **结算完整性**：重复 `use_id`、迟到明细会降低可计费使用的确定性，
   在结论与复核队列中显式标出。

结果只有四类，且都不构成法律定性：

| outcome | 含义 |
| --- | --- |
| `covered` | 存在当前有效、且范围匹配的授权 |
| `not_covered` | 未找到覆盖该使用的有效授权；这是覆盖缺口 |
| `under_determination` | 证据不足、状态争议、撤回或数据不完整，不能判定 |
| `unresolved` | 版本或作品归属无法关联，待核实 |

**缺少证据时不把使用默认归入授权或侵权**：没有凭证得到 `under_determination`，
只有主张得到 `under_determination`，两者都会进入复核队列而不是被判为未授权使用。

## 追加式数据湖

- 每次 `ingest` 追加一条 `ingest_run`，保留快照名称、接收时间、内容哈希和计数。
  内容与接收时间都相同的快照默认拒绝重复入库，除非显式 `--allow-duplicate`。
- 原始行按 `(run_id, source_file, line_number)` 原样保存；规范化事实与覆盖结论
  各自按 run 保存。任何结论都能回到具体凭证行。
- 每次入库与上一快照比较，写入 `coverage_change`：
  `outcome_changed`、`settlement_changed`、`new_use`、`use_disappeared`。
  已结算使用即使本次结论仍为 `covered`，只要曾发生变化，也会进入高优先级复核。
- `review_item` 是**派生**的复核清单，优先级：
  - **1**：撤回/争议、无法判定的使用，或已结算但结论/状态发生变化的使用；
  - **2**：未覆盖缺口、重复记录、非结算状态下的结论变化；
  - **3**：迟到明细，需要结算期间取舍。

## 命令行

```text
rights_facts [--lake PATH] ingest PATH [--name NAME] [--received-at ISO8601] [--allow-duplicate] [--json]
rights_facts [--lake PATH] runs [--json]
rights_facts [--lake PATH] coverage [--run latest|previous|序号|run_id] [--json]
rights_facts [--lake PATH] review  [--run ...] [--json]
rights_facts [--lake PATH] changes [--run ...] [--json]
rights_facts [--lake PATH] issues  [--run ...] [--json]
rights_facts [--lake PATH] use  USE_ID [--json]
rights_facts [--lake PATH] work  WORK_ID [--run ...] [--json]
```

`--json` 输出便于其他系统按 `use_id` / `work_id` 消费；所有 JSON 都带 `run`
（序号、快照名、接收时间、内容哈希），说明数据用户看到的是哪一版事实。

## 数据完整性检查

`issues` 报告区分严重度：

- `error`：该行无法解析或身份冲突，已跳过或保留首条；
- `warning`：字段越界、时间缺时区、悬空的替代引用等，需要人工处理；
- `info`：预期内的观察历史（如同一 `grant_id` 有多条记录），仅为可追溯性记录。

## 已知边界

- 判定仅基于快照内的凭证，不访问外部系统，也不推断缺失的授权。
- `supersedes_grant_id` 记录补充/替代关系并参与环检测，但状态优先级按
  “结构适用的最新记录”计算；这样地区或渠道范围不同的撤回不会误伤其他范围。
- 时间是带时区的；日期型授权边界只精确到自然日，系统不在报告之外自行外推。
- 库是单文件 SQLite，适合本地复现与评审；并发写入需要外部串行化。
