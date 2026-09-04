## Context

动机与范围见 [proposal.md](./proposal.md)，行为合同见本 change 的五份 [delta specs](./specs/)。现有实现已有可复用基础，也存在不能继续沿用的假设：

- `SourceRecord`、`DocumentRecord`、`SyncResult` 与报告版本已经可序列化，SQLite 对同 ID 不同 payload 会拒绝覆盖；联网正式 PDF 已先校验 MIME/大小、计算 SHA-256 并归档，再进入解析。
- `ReportVersion`、研究记录和现有查询已具有 `as_of`/`available_at` 与冻结数据快照实践；这些语义必须保留。
- 当前 `AdapterManager`、`SyncRequest.providers`、在线 smoke 和前端来源选项各自硬编码 provider；`SyncResult.provider_results` 只保存自由文本，无法表达逐查询状态和覆盖。
- `OfficialDisclosureAdapter` 只按有限年数查询巨潮与一个交易所，失败被压成 warning；没有 baseline/incremental/reconcile、来源级 checkpoint、HTTP validator 或 IR adapter。
- `ReportStorage.save_sync_result()` 跨多个独立事务保存对象，可能留下半批数据；当前 SQLite 为 `user_version=5`，迁移写在 `ReportStorage._initialize()` 中。
- `create_app(service)` 仍会独立构造默认 `AdapterManager`，手工文档入口通过模块级 `RAW_ROOT` 写文件；临时数据库、API、CLI 与 smoke 尚未共享一个显式 database/data-root 运行时，存在把试点材料写入默认目录的风险。
- 当前没有跨 API/CLI/进程的 run 执行租约；started attempt 在进程崩溃后也没有不伪造来源结果的 abandoned closure 与 supersedes 关系语义。
- 当前列表/API 响应在 adapter 内直接解析，未形成 discovery snapshot/proof；因此无法在原响应消失后独立复核合法 `no_data`、总数和分页终止条件。
- 原始 PDF 较稳定，但同一 `.pdf.txt`、FTS 和部分 Parquet 输出可以被重建或覆盖，不能充当不可变证据快照；旧 `data_snapshot_id` 也不包含原始字节。
- 第一阶段方法文件仍为 `content_status: skeleton`。本设计只把 [计划.md](../../../计划.md) 已列出的第一步主题转成“采集覆盖问题”，不把它们提升为分析方法或结论。

### 真实联网反馈的因果边界

本次 planning-only 补强以 [阶段日志.md](../../../阶段日志.md) 已冻结的 v1.3 真实运行观察为诊断输入；不在本设计中改写该运行、注册表或验收记录。问题必须分成“外部触发”与“软件合同缺陷”，否则修复后仍可能把正常的来源拒绝误报成代码失败，或反过来用“网站不可用”掩盖系统继续请求和错误推进：

| 观察 | 外部触发 | 当前实现缺陷 | 修复后的合同 |
| --- | --- | --- | --- |
| SSE 返回 HTTP 200 HTML 和 `x-tengine-error: denied by bot` | 来源侧 bot challenge | 分类器只看有限正文特征后检查 MIME，没有识别精确挑战头，误记 `parse_failed: unexpected_mime` | 按固定优先级识别为 `restricted: upstream_bot_challenge`，不归档挑战正文 |
| SSE challenge 后仍继续附件和后续查询 | 同一挑战持续存在 | orchestrator 逐 plan/fetch 独立执行，没有当前 run 的来源级停止投影 | opening attempt 后，同来源版本剩余工作逐项写无 I/O `policy_skipped: source_access_halted` 并自动 partial finalize |
| CNINFO bootstrap 返回 504 | 上游网关超时 | 217 个依赖查询各自尝试解析缺失 binding，制造 `parse_failed: parameter_binding_missing` 噪声 | prerequisite 失败只发生一次；下游逐 plan 写 `policy_skipped: dependency_unavailable`，零 downstream transport 并共享因果链 |
| CLI reconcile 从父 run 最早 coverage 开始 | 无外部触发 | runtime 没有调用已有缺口选择逻辑，只取 `min(coverage.time_start)` | API/CLI/runtime 共用一个 selector，优先精确未解决 barrier 并回显有效 overlap range |

人工停止的 v1.3 run 没有 final event、coverage resolutions、checkpoint 或 manifest，只能作为不可变诊断记录保留；本补强明确禁止 resume、补写或把它选作 reconcile 父运行。后续真实样本必须使用全新隔离 namespace 创建新 run。

## Goals / Non-Goals

**Goals:**

- 在不替换现有证据/报告对象的前提下，增加来源定义层、采集控制层、不可变原始证据层和兼容 facade。
- 让计划生成、请求、分页进度、终态、覆盖、checkpoint 和证据快照均可在进程异常后恢复与审计。
- 让一次物理查询安全服务多个业务问题覆盖项，并用持久租约与 fencing 防止重复 I/O、旧执行者迟到提交和崩溃后永久卡住。
- 使相同输入、注册表版本、checkpoint 和冻结响应夹具产生确定性计划、状态、版本链与 manifest 哈希。
- 在 v1 内用来源专用 adapter 覆盖巨潮、上交所、深交所及贵州茅台 IR，不引入通用浏览器抓取或搜索。

**Non-Goals:**

- 本 change 不从材料中抽取商业模式字段，不调用 Codex 做定性总结；只提供未来调用必须使用的 `EvidenceSnapshotManifest` 门禁。
- 不把采集控制对象复制到 DuckDB/Parquet，不改变八步报告正文或现有四种报告导出；新对象以 SQLite + 内容寻址文件为权威存储。
- 不自动判断服务条款是否合法。人工批准负责给出带日期和依据的策略，系统负责按固定策略失败关闭并留痕。
- 不为旧自由文本同步历史虚构 attempts、no_data、available_at 或 checkpoints。

## Decisions

### 1. 注册表采用 Git 版本化 JSON，运行同时冻结规范化副本

新增 `config/data_sources/source_registry.schema.json`、`business_model_questions.v1.json` 和 `business_model_sources.v1.json`。JSON 便于排序、规范化哈希和复用现有配置加载方式，不新增运行依赖。顶层注册表具有 `registry_id`、`registry_version`、`effective_at`、`question_set_version` 和 definitions；加载器先按 schema 严格校验，再对排序后的 UTF-8 JSON 计算 SHA-256。

每次 run 将实际使用的规范化注册表/问题清单 JSON 与哈希写入 SQLite，而不是只保存仓库路径。这样未来 Git 配置变化后仍能解释旧运行。定义除端点与许可外，还固定请求及逐跳重定向 scheme/host/port/path allowlist、来源时区与发布时间精度、响应 schema/MIME、discovery 响应保留策略、`metadata_only|required_attachment` fetch policy、最大跳转/响应/解压字节数、共享速率/并发上限、允许重试的方法及 deadline。配置文件只保存公开端点和许可元数据，不保存 token、Cookie 或浏览器状态。

v1 业务来源定义如下；`official` 是兼容 alias，不是第五个来源。第四个定义必须存在于 v1 注册表，但其初始策略状态不是已批准访问：

| source_definition_id | 适用范围 | adapter_key | v1 初始状态 | 主要访问操作 |
| --- | --- | --- | --- | --- |
| `cninfo.disclosures` | 全部 A 股 | `cninfo` | 以版本化策略为准 | 公司身份/上市锚点、招股书、定期报告、业务相关公告元数据与附件 |
| `sse.disclosures` | 上交所证券 | `sse` | 以版本化策略为准 | 招股书/公告元数据与附件 |
| `szse.disclosures` | 深交所证券 | `szse` | 以版本化策略为准 | 招股书/公告元数据与附件 |
| `moutai.ir` | 仅 `600519` | `moutai_ir` | `pending_policy/disabled` | 获批后才允许访问固定官方 IR 列表页与附件 |

`moutai.ir` 的确切 canonical 域名、允许路径、服务条款检查时间和许可依据必须在实现期人工只读核对并创建新定义版本后才能设为 `enabled`；未批准时保持禁用，以无 I/O 的静态策略处置进入覆盖清单，不得换用搜索结果。已批准来源在运行时新遇到登录、验证码、付费墙或许可不明时才创建阻塞性的 `policy_skipped`/相应受限 attempt。现有 AKShare、Sina、BaoStock、Tushare 只为已有非 `business_model` scope 建立 `legacy` 定义/alias；它们不计入上述 v1 范围，本 change 也不新增其数据能力。

第一步问题清单是采集合同而非研究方法。`business_model_questions.v1.json` 固定以下十项追踪主题；“source query”是注册表 query family，不代表已经取得答案：

| `计划.md` 采集主题 | 稳定 question ID | 至少映射的 source query family |
| --- | --- | --- |
| 公司起源、招股、主营业务构成与盈利模式 | `BM.Q01.IDENTITY_BUSINESS_MODEL` | `company_bootstrap`、`prospectus`、`periodic_report` |
| 分产品、分地区收入与毛利 | `BM.Q02.PRODUCT_REGION_ECONOMICS` | `periodic_report`、`business_announcement` |
| 客户/供应商集中度与渠道 | `BM.Q03.COUNTERPARTY_CHANNEL` | `prospectus`、`periodic_report`、`ir_publication` |
| 单位经济与定价相关披露 | `BM.Q04.UNIT_ECONOMICS_PRICING` | `prospectus`、`periodic_report`、`business_announcement` |
| 产能、产量、销量、库存、利用率与在建产能 | `BM.Q05.CAPACITY_OPERATIONS` | `periodic_report`、`business_announcement`、`ir_publication` |
| 资本开支周期 | `BM.Q06.CAPEX_CYCLE` | `periodic_report`、`business_announcement` |
| 研发投入与研发效率相关披露 | `BM.Q07.RD_INPUT_EFFICIENCY` | `periodic_report`、`prospectus` |
| 上下游关系与供应链位置 | `BM.Q08.VALUE_CHAIN` | `prospectus`、`periodic_report`、`ir_publication` |
| 未来战略与重大经营变化 | `BM.Q09.STRATEGY_CHANGES` | `periodic_report`、`business_announcement`、`ir_publication` |
| 技术/成本/渠道/品牌竞争力声明及可观察证据 | `BM.Q10.COMPETITIVE_CLAIMS_EVIDENCE` | `prospectus`、`periodic_report`、`business_announcement`、`ir_publication` |

validator 要求每个 question ID 至少映射一个已批准 query，或携带明确的“无适用来源”解释；每个 query 同时固定 query stage、日期边界、分页终止条件、canonical ID 规则、允许的响应类型、resource extraction schema 和 fetch policy。计划器先产生逻辑 `CoverageEntry(source_definition_version, question_id, query_id, time_slice)`，再按固定来源版本、请求方法/端点、规范参数、时间片、分区与分页语义组成的稳定 `execution_key` 产生 `PhysicalQueryPlanItem`；二者在尚无 attempt 的计划期通过不可变 `PhysicalQueryCoverageLink(plan_item_id, coverage_entry_id)` 多对多关联。执行期每个 discovery/fetch attempt 只引用其 `physical_query_plan_item_id`，retry 可有多个 attempt 指向同一 plan item，coverage 再经 plan link 解析这些真实执行。多个问题映射到同一传输查询时只执行一次 discovery，但问题级 coverage 不丢；任何会改变实际请求或终止语义的参数不同都不得错误合并。上述主题只约束采集，不实现单位经济、研发效率、定价权或竞争力判断。

**替代方案：**直接扩展 `SourceRecord`。未采用，因为它表示一次具体证据实例，混入可变的访问策略会使旧证据含义随配置漂移。

### 2. 策略、观测、来源内容版本与字节身份分离

- `SourceDefinition`：允许如何访问一个来源的版本化策略。
- `SourceRecord`：现有一次证据来源记录；增加可选且有默认值的 `source_definition_id`、`source_definition_version`、`raw_resource_snapshot_id`、`canonical_resource_id` 和 `available_at`，保持旧 JSON 可读。
- `DiscoveryObservation` / `DiscoveryProof`：逐列表/API page 保存访问、schema/分页/总数/终止证明；允许归档时引用 `resource_role=discovery_response` 快照，不允许保留正文时只保存完整响应哈希、固定 parser/schema 的校验结果和合规最小摘要。
- `DiscoveredResource`：由已冻结 discovery snapshot 或 proof 的纯解析结果产生，保存父证据、page/cursor、row locator/hash、source canonical ID、附件引用和 required fetch 标志。
- `ResourceObservation`：每次 fetch attempt 对一个资源的实际访问结果，保存父 discovery、原始/最终 URL、逐跳重定向、validator 及其来源 snapshot、HTTP/时间摘要和可空 snapshot 引用；成功内容具有 `new|changed|unchanged` disposition，受限/失败访问则以空 snapshot 关联 attempt 终态。新的观测时间或 ETag 不会改写 snapshot。
- `RawResourceSnapshot`：一个来源定义上的某一确切字节版本，身份不包含每次访问上下文。`resource_role=content` 时必须具有 canonical resource、upstream material、canonical URL 与发布/PIT 字段，`upstream_material_id` 由监管公告 ID 优先、规范化公告键次之、内容哈希兜底，用于识别镜像独立性；`resource_role=discovery_response` 时改用 plan item + page/cursor + query-page canonical，上游材料身份与发布时间可空，且不得投影为 `DocumentRecord` 或默认进入 LLM manifest。
- `ContentBlob`：仅按完整 SHA-256/长度标识的原始字节。不同来源或 canonical 取得相同字节时可共享 blob，但不能合并 provenance snapshot。

同一上游材料从巨潮与交易所取得时保留各自的 attempt、observation 和 snapshot；内容 blob 可去重，但 `upstream_material_id` 相同，不能形成独立双源。同一来源/canonical 再次取得相同哈希时只追加 observation 并引用既有 snapshot；同 URL 出现新哈希时创建新 snapshot/version。现有 `upstream_source_id=official-document:<hash>` 继续可读，新记录从 snapshot 确定性派生兼容值。

discovery 有两条互斥合规路径。允许保留原文时，先冻结 response snapshot，再由 `parse_retained_discovery(snapshot_id)` 只读解析；禁止保留原文但允许最小审计时，唯一流程是“有界内存响应 → 确定性 `validate_and_normalize_without_retention` → 单事务提交 `DiscoveryObservation + DiscoveryProof + DiscoveredResource[]` → 丢弃字节”。后一 proof 只证明当时固定 parser/schema 的校验和规范化结果，不能在没有 body 时声称可独立重放，提交后也不得再从 proof 反向解析原响应；若审计合同要求未来独立重放而许可禁止留存，则查询为 `policy_skipped`。两类 discovery 证明均不成为默认 LLM 内容。

**替代方案：**把访问站点 ID 当成上游证据 ID。未采用，因为这会把同一 PDF 的镜像误算为独立来源。

### 3. 采集模型使用固定计划加 append-only 事件

在 `src/analysis/acquisition/models.py` 增加以下 Pydantic 对象，并由现有 `src/analysis/models.py` 兼容导出必要公共类型；终态对象使用 `frozen=True`，运行中变化通过事件追加而不是覆盖终态 payload：

| 对象 | 核心字段/职责 |
| --- | --- |
| `CompanyAcquisitionProfile` | ticker、交易所、上市日、招股书日期及各锚点证据；为 baseline 提供可审计起点 |
| `SourceDefinition` / `SourceQueryDefinition` | 注册表字段、query 到问题映射、允许端点、分页、validator、重试及 LLM/归档策略 |
| `AcquisitionRun` | 固定输入、registry/question 哈希、模式、as_of、父 run/reconcile 目标 |
| `AcquisitionRunEvent` | `planned/running/finalized` 事件与最终 `succeeded/partial/failed`、`coverage_accounted`、`default_consume_eligible` |
| `PhysicalQueryPlanItem` / `PhysicalQueryCoverageLink` | 按 execution key 去重的物理查询，以及计划项与一个或多个问题级 coverage 的不可变多对多关系 |
| `AcquisitionExecutionLease` | run 唯一 owner token hash、lease epoch、TTL/heartbeat；提供 claim/reclaim 与迟到写 fencing 的可变控制投影 |
| `AcquisitionAttempt` | `physical_query_plan_item_id`、`discovery|fetch`、execution key/query 或父资源、time slice/page/cursor、retry group/ordinal、lease epoch、脱敏请求摘要 |
| `AcquisitionAttemptEvent` | started、segment committed、12 类 outcome terminal，或与 outcome 互斥的 `abandoned` lifecycle closure；新尝试以 supersedes 关系恢复 |
| `DiscoveryObservation` / `DiscoveryProof` / `DiscoveredResource` | 列表/API 的 `discovery_response` 快照，或无正文留存时原子提交的最小证明与带 row lineage 的规范资源引用 |
| `ResourceObservation` | 每次 fetch attempt、父 discovery、URL/redirect、validator 锚点、时间、`new|changed|unchanged` 与 snapshot 引用 |
| `CoverageEntry` / `CoverageResolution` | 固定来源 × 问题 × 时间计划，经 M:N links 引用真实 attempts、发现证明、缺口、快照和最终解释 |
| `SourceCheckpoint` | append-only 版本、每 query partition 的连续位置、来源安全下界、validators、当前未解决 barrier 与 CAS 父版本 |
| `BarrierResolution` | opening barrier、精确 work position、resolving attempt、兼容来源/query 语义、proof/snapshot/observation 引用与创建时间；只追加且不改写历史失败 |
| `StorageNamespace` / `StorageBindingIntent` | SQLite 与 data root 共享的绑定 ID/layout version，以及首次绑定期间 DB 邻接且不暴露绝对路径的持久 intent/journal，防止数据库与证据目录错配或崩溃后被其他 root 抢绑 |
| `SourceCandidate` | 未注册域名/URL、发现上下文、审核状态；与证据表无外键路径 |
| `ContentBlob` / `RawResourceSnapshot` / `DerivedArtifact` | 原始字节去重、按 `content|discovery_response` 条件约束的快照、完整哈希/时点/版本链，以及版本化文本/OCR/页图 |
| `SnapshotIntegrityEvent` | append-only `verified|quarantined` 完整性结果；当前可消费性由事件派生，不修改 snapshot |
| `EvidenceSnapshotManifest` | run、coverage 摘要、纳入/排除的资源与派生版本、as_of、策略决定和 manifest 哈希 |

`AcquisitionAttempt` 在 I/O 前插入并引用一个物理 plan item；每次协议重试建立新的 attempt，共享 retry group 但不复制 plan-to-coverage links，分页成功位置通过 segment event 即时提交。discovery attempt 的终态只描述自身 I/O：完整分页且返回规范资源为 `success`，有有效旧发现锚点证明响应未变化时为 `unchanged`，完整合法空结果为 `no_data`，已有成功页后续页失败为 `partial_success`。注册表标记 required 的每个资源另建 fetch attempt；取得 `new|changed` 内容为 `success`，有效 304 或相同哈希为 `unchanged`，其他状态按该次 fetch 事实记录。`PhysicalQuery/CoverageResolution` 才聚合 discovery proof 与全部 required fetch，附件失败只使聚合为 partial/blocked，不反向改写已终结 discovery attempt。这样“每次尝试”和混合资源结果都不会被自由文本总结吞掉。`legacy_unassessed` 只用于旧对象兼容标记，不加入 12 个新 attempt 结果枚举。

attempt 生命周期是 `started -> segment* -> outcome_terminal`；执行所有权在结果未知时丢失则是 `started -> abandoned`，二者互斥。接管者使用新 attempt 的不可变 `supersedes_attempt_id` 关系从最后安全 segment 重试；`abandoned` closure 和 supersedes 关系都不属于 12 类来源结果，也不参与 no_data/成功汇总。未解决 abandoned 会打开 barrier；只有后继 attempt 精确覆盖同一工作位置并提交足够证据、再追加 `BarrierResolution` 后，checkpoint 才可重新计算，旧 attempt 永远保留。只有确有请求 deadline 证据且执行者仍持有有效 lease epoch 时才写 `timeout`，不能从进程退出推断来源超时。

每次会发起外部 I/O 的 production、smoke 或 ad-hoc execute 都在首个请求前原子 claim run 级租约；plan/list/show 不需要。claim/renew/release/reclaim 更新唯一控制投影，同时追加不可变 run audit event；租约过期后新 owner 递增 epoch 并恢复，所有 segment、outcome terminal、snapshot linkage、finalize 和 checkpoint 写入都携带 epoch，旧 owner 的迟到提交被 fencing 拒绝。租约是唯一允许 CAS 原地更新的运行控制状态，不替代审计事件。

运行完成含三个正交字段：

- `coverage_accounted`：所有计划单元都有终态/静态 policy skip；即使受限也可为 true。
- `material_gap_count`：未取得或未完整取得的适用内容数量。
- `default_consume_eligible`：没有非终态或 checkpoint barrier，证据门禁通过，可作为默认最新批次。

审计者可显式为 `coverage_accounted=true` 但有材料缺口的 run 冻结“部分证据 manifest”；manifest 必须列出全部缺口，且不能成为默认最新批次。本 change 不把该 manifest 交给 LLM。

**替代方案：**只在同步结束时写一个可变 run JSON。未采用，因为当前 `save_sync_result()` 已证明中途失败会丢失逐步状态并产生半批记录。

### 4. baseline 以证据锚点生成确定性半开区间计划

时间统一存 UTC-aware datetime，同时保存来源原始时间字段、source timezone、`instant|date|unknown` 精度和 available_at basis；来源查询按其本地日界转换，内部覆盖使用半开区间 `[start, end)` 防止相邻窗口漏/重。只有日期时使用下一本地日界作为 PIT 保守上界，不能填充为当天 00:00；没有版本证明则使用 retrieved_at。公司起点按规格选择招股书/上市锚点；单个 query 的 effective start 为公司起点与 `earliest_available_at` 较晚者，前段生成 `source_not_available` 静态 policy skip。

每个来源定义给出最大日期窗口与 page size，但不设会把 baseline 静默截断的全局记录上限。计划器先切时间片，再由 adapter 完整遍历页码/游标；命中服务端上限、重复游标、缺页或无法证明结束条件时为 `partial_success`。公司锚点缺少时，先执行注册表中的 bootstrap query；仍不可得才使用有依据的来源最早可得日并在 run 上记录 anchor quality，不猜日期。

**替代方案：**沿用 `annual_years/announcement_years`。未采用，因为最多十年不满足首次全历史且无法证明覆盖。

### 5. 增量采用分区 checkpoint、重叠回看和哈希最终裁决

checkpoint 主键逻辑为 `(ticker, source_definition_id, source_definition_version, question_set_version, checkpoint_version)`。partition 按物理 query definition/execution family、来源分区与时间范围建立，不按其映射的业务问题复制；每个 partition 保存由时间精度保守上界与 canonical ID 组成的稳定全序 `safe_through`、opaque cursor（仅在来源保证可续用时）、最近 validators 及其 snapshot anchor 与最早 barrier。来源级 `safe_through` 是必需物理 partition 的最小连续位置。

incremental 计划从 `safe_through - overlap_window` 至 run.as_of。判断顺序：

1. canonical ID 定位逻辑资源；
2. ETag/Last-Modified 用于条件请求和观测；
3. 304 只有在 validator 来自同一兼容来源/canonical 或 URL 的已提交、未隔离且完整性有效 snapshot 时才 `unchanged` 并引用该 anchor；没有 anchor、anchor 已隔离或定义不兼容时，该 fetch 终结为 `parse_failed: validator_anchor_missing|invalid_304`，再按策略建立独立的无条件 fetch attempt 或保留 barrier；
4. 有 body 时重新计算完整 SHA-256，哈希才是内容版本最终裁决；
5. 同 canonical/URL 新哈希创建新版本；同哈希只追加观测，不复制证据。

只有 `PhysicalQuery/CoverageResolution` 能证明完整 discovery proof、全部 required fetch，以及 `success`、`unchanged`、合法 `no_data` 的连续区间时才可推进分区。列表 discovery 成功但必需附件失败时保留已取得快照与 discovery 的 `success`，失败 fetch 保留自身终态，聚合 resolution 为 partial/blocked，并在该资源位置形成 barrier；其他来源镜像成功不能清除本来源 barrier。计划期可确定的“不适用市场/来源尚不存在/尚未批准且 disabled”使用无 attempt 的静态 policy disposition，保留在 coverage 但不属于 required partition；已 enabled 的适用查询在运行时因许可不明或临时禁用而跳过，必须创建阻塞性的 `policy_skipped` attempt。所有访问/网络/解析/归档失败、未解决 abandoned 和 `partial_success` 都形成 barrier。barrier 表示当前未解决工作位置而非历史失败永久封锁；只有兼容的后继 attempt 精确覆盖同 source/query/canonical/work position，并提交所需完整 proof 或 snapshot/observation，才可追加不可变 `BarrierResolution`。finalize 只考虑未解决 barrier，并使用 `BEGIN IMMEDIATE`、父 checkpoint version 与当前 lease epoch 做 CAS/fencing；失败者重新对账，不覆盖新版本。

reconcile 读取原覆盖缺口、完整性异常或不兼容 registry 变化，生成新的关联 run。迟到资源可以新增版本和覆盖修正，但不修改旧 run，也不会简单回退 checkpoint；若证明当前安全性不足，则写新 checkpoint 版本并设置 barrier。对于允许历史 URL 静默替换的来源，注册表另行定义周期性历史资源复核/reconcile cadence；有限 overlap 只承诺检查窗口内变化，不能宣称持续检查全部历史。

reconcile target 由一个无 I/O 的 `ReconcileTargetSelector`（或等价唯一领域服务）权威计算，`AcquisitionRuntime.plan_company_run()`、CLI 与 API 只能消费该结果，不得复制选择算法。父 run 必须已有不可变 final event；显式指定未 finalized 父项时在子 run 创建前拒绝，`--from-latest-run` 只在同 ticker/scope 的 finalized 非 reconcile production runs 中选择并明确报告被排除的更新未终结 run。优先级固定为：最早未解决 barrier 的精确 source/query/partition/work position；没有 barrier 的未解决 required coverage；quarantined snapshot 所属 coverage；前三者都不存在时最早已完成 required slice 的周期性复核。每类按父计划时间片/ordinal 与规范 work-position 排序，再以稳定 ID 打破平局。选择结果携带 barrier/opening attempt/retry group/canonical、父时间片和按该来源固定 overlap 计算的 effective range；planner 只加入该目标、必要 overlap 与 parameter-binding prerequisites，不从父 run 全局最早 coverage 重跑所有来源。explicit incremental 仍保持更保守的前置条件：任一 enabled 且适用来源没有兼容 `source_safe_through` 时，在创建 run/I/O 前拒绝。

**替代方案：**每家公司一个最大发布时间水位线、只信 ETag，或把父 run 全局最早 coverage 当 reconcile 起点。未采用，因为多 query 进度不同、公告可能迟到、上游 validator 可能错误，而全局最早起点会掩盖真正 barrier 并产生无意义全历史重跑。

### 6. 原始字节、派生物和 manifest 分层冻结

新内容使用绑定 data root 下的完整哈希相对路径 `raw/blobs/sha256/<前两位>/<64位sha256>`；扩展名/MIME 仅为元数据，避免 16 位前缀碰撞。数据库与 data root 用同一 `StorageNamespace`/layout marker 配对，任何不匹配在 attempt/I/O 前失败关闭。写入流程为同目录临时文件 → flush/fsync → 原子 rename → 重新读取完整哈希/长度 → 在一个 SQLite 事务内插入 content blob、snapshot 与 creating observation；snapshot/observation 的循环引用使用延迟外键或等价的同事务约束，提交时必须同时成立。SQLite 与文件系统不能做同一事务，因此顺序始终“blob 先成功、snapshot 与 creating observation 后提交、checkpoint 最后提交”；DB 失败留下的 blob 是未引用 orphan，不是证据，本 change 只报告/隔离，不自动删除。临时/原子写失败、发布后哈希或长度不一致、snapshot 元数据提交失败分别把对应 attempt 终结为 `parse_failed`，reason code 固定为 `archive_write_failed`、`integrity_mismatch`、`snapshot_commit_failed`，且均形成 checkpoint barrier。

discovery response 与正文资源共用 blob/snapshot 原子发布机制但以 resource role 区分。允许保留 discovery 原文时先提交 `resource_role=discovery_response` snapshot，再由只读纯 parser 从 snapshot ID 产生 `DiscoveredResource`。不允许保留原文时不得先提交 proof 再尝试解析：有界 body 必须在内存中由固定 parser/schema 的 `validate_and_normalize_without_retention` 一次完成校验与规范化，并在一个 SQLite 事务中提交 `DiscoveryObservation`、含完整响应哈希/长度/HTTP/MIME/schema/分页/总数/终止摘要的 `DiscoveryProof` 和全部 `DiscoveredResource`，成功后立即丢弃字节。该 proof 不含 body、不能声称可独立重放；若合同要求重放则 `policy_skipped`。缺页、总数不闭合或 proof 不完整时，首页为 `parse_failed`、已有成功 page segment 后为 `partial_success`，不能成为 `no_data`。

`available_at` 是“该确切字节版本可证明公开”的安全时间，snapshot 保存该值及其 basis，但不吸收每次访问上下文。`content` 快照必须保存 canonical/upstream 身份及原始 published/precision/source timezone：官方不可变附件可使用经验证的发布瞬时值，只提供当地日期时使用下一本地日界并保存 `published_at_precision=date`，会被覆盖且无版本证明的网页使用 creating `ResourceObservation.retrieved_at` 计算 `available_at`。`discovery_response` 快照以 plan item/page/cursor/query-page canonical 为角色身份，上游材料身份和 published_at 可空；本次 observed/retrieved 时间保存在 creating `DiscoveryObservation`，后续观测同样只追加 observation，均不得修改 snapshot。归档策略不允许时只保存合规的状态/最小诊断摘要，不保存正文。

派生物写入绑定 data root 下的相对路径 `raw/derived/<snapshot_id>/<extractor-id>/<output-hash>`，包含 extractor 版本与参数。若现有或未来流程生成文本/OCR/表格/页图，现有 FTS 继续是可重建索引，但必须索引指定 derived artifact；不得覆盖被 manifest 引用的 `.txt`。本 change 不新增业务文本、OCR 或表格抽取器。`EvidenceSnapshotManifest` 使用 canonical JSON 哈希，文件副本写入同一 data root 下的相对路径 `acquisition/evidence/`，SQLite 保存同一内容和哈希；backup、quarantine 同样只从绑定根解析，API 只返回元数据/相对标识，不返回原始字节或绝对本地路径。项目默认 data root 可以位于 `var/`，但 `var` 不是持久模型的一部分。

完整性复核不修改不可变 snapshot：每次读取检查都追加 `SnapshotIntegrityEvent`。最新有效事件为 `quarantined` 时，新 manifest 与默认消费查询必须拒绝该 snapshot 并生成 reconcile 候选；历史报告和旧 manifest 引用仍保持原样。

传输层禁用自动重定向并在每一跳请求前校验 allowlist、HTTPS 不降级、无 URL 凭据且目标不是未批准的 private/loopback/link-local 地址；响应按可信 Content-Length 及流式压缩/解压硬上限读取，越限时不发布 blob、不得自动重试。越界 redirect、HTTPS downgrade、未批准 private target 或响应超限在尚无成功 segment 时固定为 `policy_skipped: redirect_not_allowlisted|transport_target_forbidden|response_size_exceeded`，已有成功页的 discovery 则聚合为 `partial_success` 并保留具体 reason。首个 discovery page 的 schema/proof 失败为 `parse_failed`，已有成功 page segment 后为 `partial_success`；`restricted` 只表示来源侧限制、验证码或挑战。

响应分类优先级固定为：HTTP status → 精确且窄化的 challenge header/value allowlist → 最多固定字节数的登录/付费/challenge body markers → expected MIME → schema。v1 代码 allowlist 只新增规范化后的 `x-tengine-error=denied by bot`，映射 `restricted: upstream_bot_challenge`；该顺序保证 HTTP 429 等协议状态优先，同时不会先被 HTML MIME 吞掉。没有精确信号的普通 HTML 仍为 `parse_failed: unexpected_mime`，不能为了减少 parse_failed 把所有 HTML 泛化为 restricted。逐跳 redirect chain、允许的挑战头摘要和 validator anchor 仅以脱敏元数据保存；不得启动浏览器、提交凭据、解验证码、伪造浏览器指纹或切换未批准镜像。

**替代方案：**继续把 `DocumentRecord` 和可变 `.pdf.txt` 当原始快照。未采用，因为它没有 exact-version available_at、validator、父版本和不可变派生物语义。

### 7. Adapter 协议拆分发现、获取和状态分类

采集实现放入 `src/analysis/acquisition/`，按 `registry`、`planner`、`orchestrator`、`repository`、`snapshots`、`checkpoints`、`manifests`、`status_classifier` 和 `adapters/` 分离；现有 `models.py`、`storage.py`、`documents.py` 只保留兼容公开模型/投影入口，避免继续膨胀千行 `official_adapter.py` 或把新事务边界混入 `save_sync_result()`。

来源 adapter 实现同一窄协议：`bootstrap_company()`、`execute_query()`、`parse_retained_discovery(snapshot_id)`、`validate_and_normalize_without_retention(bounded_envelope)`、`fetch_resource()`。输入只含 `SourceDefinition`、固定 query、窗口/cursor、validators 和 deadline；网络方法输出未丢失原始 provenance 且受大小上限约束的 typed transport envelope，不直接保存 DB、解析业务事实或生成自由文本 provider 结论。允许留存时 orchestrator 先让 snapshot service 冻结 discovery response，再调用只接受 snapshot ID 的纯 parser；禁止留存时验证/规范化只能在有界 envelope 尚在内存时执行一次，其 observation/proof/resources 由 repository 原子提交后即丢弃 body，后续只读已提交 proof/rows。两条路径都产生带 row lineage 的资源描述，required resource 随后逐个建立 fetch attempt。现有财务/事件解析器只消费 `content` snapshot 派生的兼容 `DocumentRecord`。

巨潮、SSE、SZSE 从当前 `official_adapter.py` 的私有 clients 拆为独立 adapter；贵州茅台 IR 使用固定域名/路径和显式页面 schema 的 source-specific adapter。adapter factory 以 `adapter_key` 注册实现能力，但“哪些来源启用、按什么顺序、对哪个公司适用”只来自 registry；因此增加已实现来源定义不再修改 Manager 的来源常量。

重试策略由 definition 限制：只对允许重试的网络/5xx/429执行有上限退避并尊重 Retry-After；每个重试是新 attempt。401/403/登录/验证码/付费/许可限制不重试。错误分类使用异常类型和响应结构，不用中文错误子串。

API 与独立 CLI 是不同进程，不能依赖共享 Python limiter。v1 将四个来源的最大并发固定为 1，并使用按 workspace identity + source/host 派生的 `CrossProcessSourceGate`（Windows named mutex/等价跨进程锁）包围每次请求；取得门禁后仍等待一个完整注册表最小间隔再发请求，进程崩溃释放锁后下一执行者也先等待，因此不同 DB/data-root namespace 不能绕过本机工作区的并发与最小间隔。deadline 内无法取得门禁时写 `rate_limited: local_source_gate_timeout` barrier。更高来源并发或多主机共享配额留给后续带持久协调器的 change。query POST 只有在注册表声明传输语义幂等时才允许自动重试。`Retry-After` 无论为秒数或 HTTP-date 都受单次上限和 run deadline 约束，超过 deadline 时终结为 `rate_limited` 并形成 barrier，不能无限 sleep。adapter 不接收 `data_root`、不自行写文件，也不能启用客户端自动 follow_redirects。

#### 7.1 当前 run 的来源级访问熔断

orchestrator 在每个 source work boundary 前，从 durable terminal attempt events 计算 `(run_id, source_definition_id, source_definition_version)` 的 `SourceAccessHaltProjection`；内存集合只能作为加速缓存，不能成为权威。精确 challenge 先正常完成 opening attempt、observation 和 barrier，再使该来源版本在本 run 内进入 halted。所有尚未执行的 discovery plan 和已经发现的 required resource 都先物化其既有 physical/fetch plan，再分别创建 attempt，并在任何 adapter/transport 调用前写 `policy_skipped: source_access_halted`。terminal event 现有 `protocol_summary` JSON 保存 `halt_opening_attempt_id`、`halt_opening_reason_code`、source/version 和 causal group，不新增可变“source disabled”表；租约接管重新扫描事件即可得到同一结果。finalize 仍逐 coverage 解释这些 skips，因而可自动得到 `partial + coverage_accounted=true + default_consume_eligible=false`，但每个 skip 和 opening challenge 都保留未解决 barrier，checkpoint 不会前进。

halt 只约束当前 run/source definition version。它不修改 registry、不写永久全局断路器，也不阻止新 run 按原定义再次进行一次合规尝试；持续挑战由后续真实门记录为外部阻塞，并可由 operator 决定何时再次运行。已在 challenge 前提交的 proofs/snapshots 继续保留，但另一来源镜像不能解除本来源 barriers。

#### 7.2 显式 prerequisite 图与失败扇出

planner 从每个 `parameter_bindings[].source_query_id` 建立 plan-level prerequisite edge，并验证图无环、依赖在同一 source definition version 且排序早于消费者。执行消费者前先读取 prerequisite 的 durable terminal fact/proof：失败、受限、partial、runtime skip、abandoned、合法 no_data 或尚无 terminal proof 时，不构造 wire request，而为每个 downstream physical plan 写 `policy_skipped: dependency_unavailable`；terminal `protocol_summary` 保存 prerequisite plan/attempt/proof、原 outcome/reason 和共享 causal group。prerequisite 成功/有效 unchanged 且 proof 存在，但 binding 为零值、多值或格式不合法时才写 `parse_failed: parameter_binding_invalid`。旧 `parameter_binding_missing` 记录不回写，新执行路径不再用它混淆上游不可用与 binding schema 缺陷。

217 个 downstream plan 仍各有独立 attempt/coverage/barrier，以兑现“来源 × 问题 × 时间范围均有明确状态”；读取接口另按 causal group 压缩展示，避免用户面对 217 条无上下文噪声。不能省略 downstream coverage，也不能用一个 aggregate attempt 替代逐计划审计。504 是否重试仍只服从固定 registry retry policy；依赖传播本身不得临时增加重试、改变速率或绕过 deadline。

**替代方案：**保留一个聚合 `official` adapter、把所有 HTML 都判为 restricted、永久全局禁用受挑战来源、挑战后直接省略剩余 coverage、或用一个 aggregate dependency attempt 代替逐 plan 结果。均未采用：这些方案分别会掩盖来源位置、制造误报、把瞬时状态变成未审核政策、破坏完整覆盖或丢失精确 barrier。

### 8. SQLite v6 为控制面权威，finalize 单事务提交

迁移 `0006_business_model_acquisition_v1` 为 additive、幂等事务，建立：

- `storage_namespaces`、`source_registry_versions`、`source_definition_versions`、`source_candidates`；
- `acquisition_runs`、`acquisition_run_events`、`acquisition_execution_leases`、`physical_query_plan_items`、`physical_query_coverage_links`、`coverage_entries`、`coverage_resolutions`；
- `acquisition_attempts`、`acquisition_attempt_events`、`acquisition_attempt_segments`；
- `discovery_observations`、`discovery_proofs`、`discovered_resources`；
- `source_checkpoints`、`barrier_resolutions`；
- `content_blobs`、`raw_resource_snapshots`、`resource_observations`、`snapshot_integrity_events`、`derived_artifacts`；
- `evidence_manifests`、`evidence_manifest_items`。

正常 `AcquisitionRuntime` 之前增加不调用 `ReportStorage._initialize()` 的 `StorageBootstrapper/MigrationCoordinator`。它以只读 SQLite 连接检查版本/表/迁移记录和源 DB 指纹，并按稳定排序同时取得基于 database identity 与 data-root identity 的跨进程 bootstrap locks；相同 DB/不同 root 或相同 root/不同 DB 均必须互斥，不能只锁二者组合出的 pair key。首次绑定先在 DB 邻接位置原子发布持久 `StorageBindingIntent` sidecar，保存 namespace ID、nonce、layout、数据库/根的不可逆 identity hash、初始源指纹/版本、当前 `bootstrap_stage` 及已验证 backup/migration manifest 哈希，但不保存绝对路径；再在目标 data root 原子发布 matching `pending` marker。若在二者之间崩溃，只有 identity hash 匹配的原 root 可用同 nonce 补齐并恢复，其他空 root 必须失败关闭。DB-side intent 是绑定完成前的权威 journal，root pending marker 镜像身份并确认阶段；完成适用备份和迁移后，在 v6 事务中写 matching bound namespace row，再原子把 root marker 发布为 `bound`，最后把 sidecar 标记完成或安全退役。v4 链固定为 `preflight -> backup_v4_verified -> migrated_v5 -> backup_v5_verified -> committed_v6 -> marker_bound`；每个文件阶段用原子 replace，每个 DB migration 用独立事务。若崩溃发生在 DB commit、intent 更新或 root 确认之间，coordinator 只有在同 nonce、双方身份匹配，且数据库恰为 journal 允许的本阶段或唯一下一版本、migration row/schema/旧 payload 及已有备份均验证通过时才可前滚，不能重跑 0005 或跳过 v5 recovery point。matching intent/root pending + 原阶段 DB/无 row 可恢复；matching intent/root pending + bound DB row 只 finalize；pending intent 指向其他 root、DB bound 但 root marker 缺失、root bound 但 DB row 缺失、非预期中间版本或身份冲突一律要求显式 repair，绝不自动把任意空 root 绑定到已有 DB。独立 `acquisition-db backup` 复用只读 preflight/备份逻辑但不创建 binding intent、不构造正常 runtime、不触发迁移；只有 bound pairing 完成后才创建 run 或联网。

所有表使用稳定主键、外键和按 ticker/run/source/status/canonical/hash 的索引；attempt outcome terminal 与 abandoned closure 互斥，supersedes 链无环；`BarrierResolution` 只能引用尚未解决且位置/语义兼容的 barrier 与后继证据；终态事件、snapshot、manifest、barrier resolution 和 checkpoint 版本以唯一约束及现有“同 ID 不同 payload 拒绝”模式保护。`storage_namespaces` 保存 DB 与 data root 共享 ID/nonce/layout version；DB-side binding intent 与 data root marker 只保存不可逆 identity hash 和绑定元数据，不保存绝对路径。完成绑定后不匹配即在 I/O 前拒绝，未完成状态只按上述 intent/pending journal 恢复协议处理。初始化逻辑改为显式支持 `0(empty)->6`、`4->5->6`、`5->6`、`6 no-op`，dirty v0、不支持版本与 `>6` 失败关闭，不能再无条件写 5。

本次联网补强不增加表或修改 v6 schema：挑战/依赖原因和 causal references 写入既有 attempt terminal event 的结构化 `protocol_summary`，来源 halt 由 durable events 投影，reconcile target 继续使用 run 已有 JSON 字段。`business_model_sources.v1.3.json` 及其 definition payload 保持不可变；精确挑战分类、执行器停止和 selector 接线属于运行时代码修复，不为制造“新版本”而创建 registry v1.4。只有端点、schema、分页/时间语义、速率/重试边界或许可/LLM 策略实际变化时，才按来源注册表合同创建新版本。

SQLite 设置有限 `busy_timeout`。claim/renew/reclaim、attempt start、每个 segment/discovery proof/snapshot 提交都使用短事务，网络 I/O 期间绝不持有写事务；run/plan 在首个请求前提交。最终 coverage resolutions、run final event 与 checkpoint 新版本在同一个 `BEGIN IMMEDIATE` 事务中重新校验父 checkpoint version 与当前 lease epoch；raw snapshot 已提交后即使 finalize 失败也保留，后续 reconcile 引用，绝不靠回滚删除证据。旧 epoch 的 segment、终态、snapshot linkage 与 finalize 一律 fencing 拒绝。

DuckDB/Parquet 不承载这些控制对象，也不做 schema 迁移。兼容 sync 后如果现有解析器产出 facts/研究记录，仍沿原有路径写入；一致性门检查这些数量/ID 与旧行为相同，并确认 acquisition snapshot ID 不被冒充为现有 `data_snapshot_id`。

**替代方案：**引入 PostgreSQL/任务队列或把控制面复制到 DuckDB。未采用，因为本项目是本地单用户，新增基础设施不能改善核心审计语义，反而扩大迁移与回滚面。

### 9. API 先创建 durable run，再显式执行

为保证调用方在长时间 baseline 前拿到 run ID，API 分两步：

- `POST /api/companies/{ticker}/acquisition-runs`：校验并持久化 run/coverage plan，返回 `201` 与 run ID，不联网；
- `POST /api/acquisition-runs/{run_id}/execute`：在现有 threadpool 中原子 claim/renew run lease 后执行或恢复，返回最终/当前聚合；活跃租约返回不泄露 owner token 的 409，过期租约以更高 epoch 接管，重复调用使用已持久安全位置，旧 epoch 的迟到写被 fencing；
- `GET /api/acquisition-runs`、`/{id}`、`/{id}/attempts`、`/{id}/coverage`；
- `GET /api/acquisition-runs/{id}/observations` 与 `GET /api/raw-resource-snapshots/{id}/integrity-events`；
- `GET /api/companies/{ticker}/acquisition-checkpoints`；
- `GET /api/source-definitions`、`/api/source-candidates`；
- `GET /api/raw-resource-snapshots/{id}` 与 `/api/evidence-manifests/{id}` 只返回脱敏元数据。

`POST /api/documents` 增加 source definition/version；已知 alias/允许 URL 可兼容解析，无法解析的旧请求返回 `409 source_review_required` 和 candidate ID，不再直接产生权威文档。这是必要的安全收紧。所有列表 API 有稳定排序、分页和 ticker/status/source filters。

API 使用显式注入的 `AcquisitionRuntime`，不在 `create_app()` 内额外构造默认 Manager；手工文档入口也使用同一 snapshot store/data root。错误映射区分 not found 404、租约/来源审核 conflict 409、validation 422、有限 busy timeout 后的 storage busy 503 和 integrity/internal 500，不能把来源失败或锁竞争误报为 404。

现有 `/api/companies/{ticker}/sync` 保留 facade：现有非 business_model scopes 经 legacy registry definitions 路由；business_model 只有在不存在兼容 checkpoint 时选择/恢复 baseline，存在安全兼容 checkpoint 才选择 incremental，已知缺口或不兼容版本要求显式 reconcile，不以“曾运行过一次”代替安全条件。响应增加带默认值的 `acquisition_run_id`、coverage/checkpoint/snapshot 摘要；`provider_results` 只由 attempts 确定性派生。`latest_sync_result()` 按 `default_consume_eligible` 过滤，使新 partial run 不遮蔽旧可用批次。

**替代方案：**单个长 POST 同时创建并执行。未采用，因为客户端超时后可能不知道 run ID，难以恢复与审计。

### 10. CLI、smoke 与 frontend 共用同一服务

每个 API 服务进程或 CLI invocation 各自只构建一个组合根 `AcquisitionRuntime`，一次绑定 `ReportStorage(db_path)`/采集 repository、`SnapshotStore(data_root)`、storage namespace、registry loader、adapter factory、cross-process source gate、clock/http client 与 orchestrator；同一进程内的 API、smoke、兼容 sync 和手工 ingest 复用该实例，不得分别解析根或构造 Manager，跨进程则以 namespace/SQLite/run lease/source gate 协调而非假设共享对象。CLI 增加 `acquire start/list/show/execute`（start 默认创建并执行，可 `--plan-only`）和 `smoke-sources`；reconcile 接受明确 `--from-run` 和 `--from-latest-run`，两条路径都调用第 5 节的唯一 selector。显式未 finalized 父项直接拒绝；latest 只选择同 ticker/scope 的 finalized 非 reconcile production run，并在存在更新未终结 run 时显式报告排除原因。输出除 resolved parent 外还包含 strategy、source/query/partition、barrier/opening attempt、work position、父时间片和 effective overlap range，禁止 CLI/runtime 再用 `min(coverage.time_start)` 重新推导。所有写入型 acquisition/smoke 命令统一接受必需的 `--db` 与 `--data-root`，完成 bootstrap/bound 后只构建一次 runtime 并先校验 namespace，其 blob、derived、manifest 与 quarantine 均从 data root 解析，避免试点污染默认库；`acquisition-db backup` 是例外，只构建 non-migrating bootstrapper/preflight 并把备份写入所选根的 `backups/`。`serve` 启用采集写接口时也在进程启动固定一组 db/root，不能按请求切换。输出 JSON 时固定字段与 API 相同。退出码约定：0 为运行成功且默认可消费，2 为参数/注册表/namespace/非法父运行或不安全 incremental 错误，3 为运行终结但有材料缺口或不具默认消费资格，4 为执行器 integrity/internal 失败，5 为可重试的 active lease conflict 或 storage busy，JSON subtype 固定为 `active_lease|storage_busy`；逐来源真实状态仍在 JSON 中，不能只看退出码。

`online_smoke.py` 不再维护 PROVIDERS 或 if/elif 工厂；它创建 `run_kind=smoke` 的持久运行并执行 registry 中 `smoke_enabled` 的最小探针。smoke 不读写 production checkpoint，默认非阻塞 CI，`--strict` 只控制该次命令退出码。

前端改动保持最小：从 `/api/source-definitions` 获取来源和适用性，business_model 页面不提供“取消勾选仍称完整”的控件，只显示固定计划、run ID、覆盖率、缺口与 attempt 状态；现有财务 scope 的 legacy 选择继续可用但标记范围。增加静态/契约测试证明 v1 来源 ID 不硬编码且页面消费结构化 API；build 只证明可编译，不冒充交互行为测试。前端不展示原始正文、不触发 Codex，也不改变报告结论。

**替代方案：**分别维护 CLI/smoke/UI provider 列表。未采用，因为这正是当前状态漂移的来源。

### 11. 报告、导出和 LLM 在本 change 中保持消费边界

不改 `ReportVersion` 正文、旧报告 JSON、Markdown/HTML/XLSX/PDF 渲染或方法文件。新 `DocumentRecord`/`SourceRecord` 通过可选字段关联 raw snapshot；未来的业务抽取 change 才会把 `acquisition_run_id`/`evidence_manifest_id` 固定到新报告输入。现有报告重算、再分析与导出仍使用原有 snapshot 和 method bundle，不回查最新 acquisition 数据。

本 change 只实现 `build_evidence_manifest()` 与校验器，不实现“send to Codex”。未来任何调用只能接收 manifest ID，校验失败或 LLM policy 拒绝即 fail closed；这样不会在本 change 中意外越过非目标。

### 12. 三个验收门分别存证与判定

1. **自动化门**：冻结响应/MockTransport 覆盖 registry schema、M:N shared execution、两条 discovery-before-parse/proof 路径、required attachment barrier/resolution、所有 12 状态、abandoned closure 与 supersedes 关系、lease race/expired reclaim/stale-owner fencing、baseline 分片、no_data、分页/重试/barrier、日期精度、有效锚点 304/hash、同 URL 新版本、乱序/周期性 reconcile、逐跳 redirect/大小、双进程来源门禁、snapshot 篡改、namespace 首次绑定与中间崩溃、候选隔离、fresh/v4/v5/v6 迁移矩阵、旧 JSON/API、并发 checkpoint、latest 可消费批次、Git ignore/敏感头清洗；本轮再增加精确 Tengine challenge header 与 generic HTML 反例、challenge 后同来源零 I/O/自动 partial finalize、lease takeover 后 durable halt、bootstrap 504 的 per-plan dependency fan-out/zero downstream I/O、成功 proof 的 invalid binding，以及 API/CLI/runtime 一致的 exact reconcile selector/finalized-parent 拒绝夹具。运行聚焦测试后仍需全量 pytest、方法库、黄金清单结构、前端测试/构建、严格 OpenSpec 校验和一次当前提交的干净 CI；这些均不替代联网门。
2. **真实联网门**：人工先核对四个 v1 definition 的当日许可/路径。代码补强后必须使用全新显式隔离且 namespace 绑定的 `--db`/`--data-root`，不得 resume 或修改旧 v1.3 未 finalized run。以 `600519` 执行一次无需人工终止且自动产生 final event 的 baseline；只有所有 enabled 且适用来源都形成安全 checkpoint 时，才原样执行至少两次 incremental，否则命令须在 I/O 前拒绝并保持该门 pending。随后只以 finalized run 为父执行一次由权威 selector 给出精确 barrier/work position 和 effective overlap 的 reconcile。逐来源记录真实状态、运行/registry/checkpoint/manifest ID、discovery proof/snapshot、required fetch、起止时间与缺口。SSE/巨潮为适用来源；茅台 IR 只有已批准定义版本才执行，否则保留 pending policy 处置；SZSE 对 600519 必须显示不适用静态处置。另以一个已记录的深市样本（优先 `300750`）执行只读 metadata smoke，避免把 SZSE 的“未调用”误称联网通过。该门独立取 `passed|pending|failed`：全部必需探针已按合同完成才 passed；来源持续 challenge、许可或网络环境阻塞，但分类/熔断/coverage/barrier 均符合合同时为 pending；误分类、挑战后继续 I/O、错误依赖状态、越过 barrier、证据门禁破坏或未解释状态才为 failed。不得提交原文或数据库。
3. **人工黄金门**：对 600519 锚点、每个适用来源 × 十项追踪主题 × 早/中/近期至少一项覆盖、共享物理查询与 coverage links、discovery proof、试点中实际观察到的全部非成功条目、至少 20 个成功/unchanged 快照以及全部发现的同 URL 版本链，人工核对 URL、canonical/upstream 身份、原始发布时间/精度/available_at、磁盘哈希和覆盖解释；单独签署 `pending|passed|failed` 与复核人/时间。它不复用当前 0/10 的财务黄金验收结论，也不声称全 A 股。

实际命令与观察结果只在实现期写入 [阶段日志.md](../../../阶段日志.md)；本设计和 tasks 只定义门槛。

## Risks / Trade-offs

- **[来源许可或页面结构随时变化]** → 注册表版本固定检查日期和依据，运行时 fail closed；真实联网结果只代表当时时点。
- **[全历史 baseline 请求量大、可能限流]** → 按来源允许窗口切片、限速、退避和即时 checkpoint/barrier；可恢复但不以截断换“成功”。
- **[并发 API/CLI 重复执行或崩溃后旧进程迟到提交]** → run 级 lease/heartbeat、单调 epoch fencing、abandoned closure 与不可变 supersedes 关系；网络期间不持有数据库写锁。
- **[列表响应消失后无法证明 no_data/完整分页]** → 许可允许时冻结 discovery response，禁止原文保留时保存完整哈希与最小 schema/分页 proof；缺 proof 一律不完成覆盖。
- **[日期字段缺少时分秒造成同日未来泄漏]** → 保存原始字段、来源时区与精度，date-only 使用下一本地日界，unknown/可变页面使用 retrieved_at。
- **[错误 data root 与数据库配对或崩溃后换 root 抢绑导致证据串库]** → DB identity/root identity 双端锁、DB 邻接持久 binding intent、root pending/bound marker 与 v6 namespace row 配对；API/CLI/smoke/手工入口统一 runtime 且在 I/O 前校验。
- **[当前下载无法证明历史页面确切版本]** → 无可靠版本时间时使用 retrieved_at，宁可排除历史回放也不制造 point-in-time 精度。
- **[文件系统与 SQLite 无跨介质原子事务]** → blob 先校验、snapshot 后提交、checkpoint 最后；orphan 只隔离报告，不自动删除。
- **[append-only 事件增加查询复杂度和 SQLite 体积]** → 提供聚合查询/索引，内容按哈希去重；以审计完整性换取少量本地存储。
- **[兼容 facade 可能让旧调用方误以为 provider 文本仍权威]** → 字段标记 deprecated，所有新判断只读结构化 attempts；旧历史明确 `legacy_unassessed`。
- **[手工文档 API 安全收紧会影响未注册来源调用]** → 已知 alias 自动解析；未知来源返回 candidate/409 和明确迁移信息，不再静默授予权威身份。
- **[600519 无法联网覆盖 SZSE]** → 将“不适用”保留在主试点覆盖中，并用独立深市 metadata smoke 只验证适配器可访问性；两者都不能外推为全市场验证。
- **[partial run 中的新材料有价值但不宜成为默认最新批次]** → 允许审计者显式冻结带完整缺口清单的部分 manifest，默认消费仍保留上一可用批次。
- **[逐跳重定向、超大/压缩响应或多进程并发伤害来源与本机]** → 禁用自动 follow、每跳 allowlist、流式压缩/解压硬上限与 workspace + source/host 的 `CrossProcessSourceGate`；越界停止且不重试，v1 同源最大并发固定为 1。
- **[挑战头规则过宽会把普通错误页误判为受限]** → 只接受精确 header/value allowlist，并以 generic HTML 负例锁定 MIME/schema fallback；新信号需代码审查和冻结夹具。
- **[run-local 熔断可能跳过该来源稍后可恢复的工作]** → 以保护来源和“不绕过 challenge”为优先；只限制当前 run，保留逐项 barrier，后续新 run/reconcile 可按策略重新尝试。
- **[dependency fan-out 产生大量无 I/O attempts]** → 保留逐计划审计和 checkpoint 精度，读取层按 causal group 汇总；不以压缩展示删除原始状态。
- **[未 finalized run 无法直接 reconcile]** → 保留其所有已提交诊断证据但拒绝补写；使用全新 baseline 获取自洽终态，避免把人工中止位置伪装成完整父覆盖。

## Migration Plan

1. **迁移前检查与备份**：在构造任何会自动迁移的 storage/runtime 前只读检查 `user_version`、migration rows、表集合、`integrity_check` 与外键；校验 registry 并运行现有全量自动化。正式首次绑定先以双端锁和 DB 邻接 binding intent 固定唯一 data root，再写 root pending marker；独立 backup 命令不创建 intent。现有 v4/v5 使用 SQLite backup API 或 `VACUUM INTO` 在所选 data root 的相对目录 `backups/` 生成带时间戳且匹配原版本的备份并记录哈希；fresh 空 v0 不制造 legacy 备份，dirty v0/不支持/未来版本直接失败。intent、备份、数据库与原始材料不进 Git。
2. **部署禁用态代码**：先加入模型、registry loader、snapshot service、API/CLI 查询与 feature flag，默认 `acquisition_v1_enabled=false`；旧报告和同步路径保持可运行。
3. **显式事务迁移阶梯**：空且 `user_version=0` 的 fresh 库在单事务 bootstrap 完整 v6；v4 先在显式事务执行现有 0005，确认 v5 并生成已验证 v5 recovery point，再在独立显式事务执行 0006；v5 先验证备份再执行 0006；v6 严格 no-op。每项 migration 只记录一次，0006 不依赖 `executescript` 隐含事务并使用 fault injection 验证中途回滚；每个成功阶段原子前滚同 nonce pending journal，崩溃恢复只接受 journal 所允许且经 migration/schema/payload/backup 证明的唯一下一状态。dirty v0、迁移记录冲突、不支持版本与 `>6` 在 DDL/DML 前失败关闭。
4. **兼容字段与历史处理**：新 Pydantic 字段均有默认值；不扫描 `provider_results` 推断状态。现有 raw 文件只在显式 legacy reconcile 中重算完整哈希且匹配旧 `DocumentRecord.sha256` 后才可建立 `legacy_snapshot` 引用，否则保持 unresolved；不改旧 payload。
5. **shadow 与自动化门**：保持 v6 schema 与 v1.3 registry 不变，先实现 classifier/source halt/dependency graph/shared selector 的聚焦冻结夹具，再启用 registry 计划/fixture 执行但不推进 production checkpoint；对比旧 financial sync 的 facts、SourceRecord、报告读取及 SQLite/DuckDB/Parquet 数量/ID，完成安全/迁移/前端全量回归和当前提交的干净 CI。若实现发现必须改变 endpoint/schema/rate/retry/license policy，停止本代码补丁并先规划新的 registry/definition 版本，不原地编辑 v1.3。
6. **试点启用**：人工确认实际来源许可后，只对 business_model v1 与指定试点开启；使用全新隔离 namespace 创建新 600519 baseline，确认它即使受限也能自行 finalize。旧未 finalized v1.3 run 不 resume、不修改、不作 reconcile 父项。安全 checkpoint 齐全才执行两次 incremental；否则在 I/O 前拒绝并保持在线门 pending。随后对新 finalized 父 run 执行精确目标 reconcile，完成独立的真实联网和人工黄金门并将实际状态写入阶段日志。
7. **兼容切换**：让 `/sync`、smoke 和前端改读 registry；保留 deprecated 字段至少一个发布周期。只有默认可消费 run 才进入 latest 选择。

**回滚：**

- 功能问题优先使用新版本二进制关闭 feature flag，继续读取旧表/旧 API facade；不删除 v6 表、snapshot 或 checkpoint，也不回写旧报告。
- fresh bootstrap 或当前 migration 事务失败时 SQLite 回滚该阶段；v4 路径的 0006 失败保留已验证 v5 recovery point，v5 路径保持 v5。迁移成功后如必须回退到旧版本程序，先停服务，核对匹配备份清单 SHA-256、`PRAGMA integrity_check` 与外键结果，把备份恢复到新的显式版本路径，再用冻结的旧读者对 Source/Document/Sync/Report 做只读 smoke；不能让当前会无条件写 `user_version=5` 的旧代码直接打开 v6 工作库。
- 恢复演练必须证明 v6 工作库哈希与 `user_version=6` 未改变、旧读者只打开恢复路径、历史 v5 payload/报告逐字节一致。恢复后产生的 v6 内容 blob 留在原隔离 data root，后续可由新版本 reconcile；本 change 不自动递归删除任何 blob。回滚只影响新入口，历史 v5 报告、数据快照和导出不变。
