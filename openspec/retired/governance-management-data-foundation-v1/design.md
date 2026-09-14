## Context

动机和产品范围见 [proposal.md](./proposal.md)，完整探索基线见 [治理与管理层数据采集底座实施方案](../../../治理与管理层数据采集底座实施方案.md)。本设计只解释实现方式，不补写仍为 skeleton 的 [第三步治理方法](../../../docs/methodology/steps/03_governance.md)。

当前实现约束：

- 当前分支从 main 的 d0020af0 基线建立，存储仍为 SQLite v5，采集由 AdapterManager 硬编码 provider，现有 API 在进程内自行构造 adapter。
- EventRecord 已有 announced_at、available_at、effective_at 和 evidence spans，但 parties/event_terms 是自由结构；它可作为兼容事件，不能作为类型化治理存量。
- 当前管理层正文解析只保留首个匹配，无法正确承载多人、多职务、代理、模糊辞任和更正。
- 当前 ReportVersion 只有单一 as_of，第三步只展示事件；现有 review 路径要求人工确认评级，与本 change 的自主治理报告不是同一工作流。
- business-model-acquisition-v1 与本分支从同一提交并行开始。它规划了共享 run、attempt、coverage、checkpoint、raw snapshot、manifest、registry 和 adapter kernel，但在本 change 创建时尚未实现。
- 项目没有固定的 OpenAI SDK 运行依赖。实现必须通过可注入协议隔离 Codex 运行器，离线 fake 不得冒充真实联网能力。

最大合并风险是两个分支同时实现 acquisition kernel 或同时占用 SQLite v6。设计因此把可并行的纯治理领域工作与必须等待共享 kernel 的集成工作分开。

## Goals / Non-Goals

**Goals:**

- 在不复制通用采集控制面的情况下实现治理专属配置、证据声明、类型化事实、人员身份、双时点重建、查询、Codex 工作流和 trace。
- 让纯领域模型和算法可使用冻结夹具与 in-memory repository 独立开发，后续只替换 acquisition/storage adapter。
- 用不可变对象、规范 JSON、内容 hash 和显式关系保证确定性复算。
- 用严格信息防火墙保证历史父 Codex 从未看见 known_at 之后的实质内容。
- 把业务不完整与技术完整性失败分开；前者交给 Codex 判断，后者失败关闭。
- 将新 schema、API 和报告以 additive 方式接入，不改变旧事件、旧报告和旧快照。

**Non-Goals:**

- 本分支不拥有 src/analysis/acquisition 下的共享 kernel，也不创建另一套 GovernanceRun、checkpoint、blob 或 evidence manifest。
- 本分支不占用 0006；治理持久化固定为依赖共享 v6 的 0007_governance_management_v1。
- 不把现有 EventRecord、ClaimRecord 或 ReportVersion 强行改造成新治理对象。
- 不建立新的治理评分或管理层诚信/能力事实字段。
- 不新增强制人工报告审批。
- v1 不新增专门前端页面；稳定 API、CLI、报告导出和 trace 是本 change 的交互面。
- 不在 Git 保存真实公告、数据库、联网结果、Codex 浏览转录或生成报告。

## Decisions

### 1. 分支所有权和两阶段集成

选择：

| 所有者 | 文件与职责 |
|---|---|
| business-model-acquisition-v1 分支 | src/analysis/acquisition、共享 registry/source policy、planner、run/attempt/coverage/checkpoint、raw snapshot/manifest、adapter、MigrationCoordinator 与 0006 |
| 本治理分支 | src/analysis/governance、治理问题与 query pack、治理抽取/实体/重建/Codex/trace、0007 |
| 两分支合并后接线 | SharedAcquisitionGateway、0007 注册、中央 runtime 注入、router 挂载和最终兼容回归 |

本分支先定义 GovernanceAcquisitionPort，只暴露治理层确实需要的能力：

- 读取固定 EvidenceSnapshotManifest 及其合格 raw/derived artifact；
- 查询运行和问题级 coverage；
- 请求一个带 scope、问题和时间范围的受控补采；
- 取得补采后的新 manifest 引用。

tests/governance/fakes.py 提供冻结 fake。fake 使用相同协议对象但不定义第二套持久化控制面。

在 shared kernel 合并前，所有依赖生产 run/checkpoint/manifest 的任务保持未完成；纯领域任务可以独立完成。集成前置脚本检查 acquisition_scope、question_set_id、question_set_version、query_pack_version、source_registry_version 是否已进入 run、coverage、checkpoint、manifest、latest selector、API 和 CLI。任一缺失都失败关闭，不在治理分支临时补一套并行实现。

备选方案：

- 在治理分支复制 kernel：短期可运行，后续会产生 checkpoint、namespace、迁移和 adapter 双真相，拒绝。
- 让治理分支直接修改并行 change：破坏用户要求的隔离和可合并性，拒绝。
- 完全等待另一分支再开发：没有必要；领域模型、算法和协议可以通过 fake 并行完成，拒绝。

### 2. 模块边界与依赖方向

目标布局：

    config/data_sources/
      governance_management_questions.v1.json
      governance_management_query_pack.v1.json

    src/analysis/governance/
      __init__.py
      canonical.py
      models.py
      ports.py
      admission.py
      extraction/
        __init__.py
        loader.py
        deterministic.py
        candidates.py
        validators.py
      entity_resolution.py
      reducers.py
      reconstruction.py
      snapshot_service.py
      repository.py
      sqlite_repository.py
      migrations.py
      service.py
      api.py
      codex_tools.py
      research_gate.py
      codex_exec_runner.py
      report_service.py
      renderers.py
      trace_renderer.py

    tests/governance/
      fixtures/
      fakes.py
      test_*.py

依赖只允许向内：

    API / CLI / report adapters
              |
              v
    application services and ports
              |
              v
    pure domain models and reducers

    shared acquisition -> GovernanceAcquisitionPort adapter
    SQLite            -> GovernanceRepository adapter
    Codex CLI         -> CodexRunner / ResearchTaskBroker adapter

纯领域层不 import FastAPI、sqlite3、httpx、Codex CLI 或旧 AdapterManager。外层 adapter 负责把共享对象映射为治理只读引用。

备选方案是在现有 models.py、storage.py、api.py 中继续堆叠治理类型和 SQL。该方案会扩大并行 merge 冲突，使领域测试依赖全应用初始化，因此拒绝。中央 api.py 和 CLI 只保留最小的 runtime 注入及 router/command group 挂载。

### 3. 配置和采集 scope

治理配置使用与共享 acquisition registry 相同的 config/data_sources 根，不建立 config/acquisition 第二套注册机制。

governance_management_questions.v1.json 固定：

- schema_version、question_set_id、question_set_version；
- GOV.Q01 至 GOV.Q11 的精确 ID；
- 中文标签、目标 record kinds；
- history_policy：since_listing、last_five_complete_fiscal_years、overlapping_lifecycle 或 first_proven_archive；
- anchor kinds、allowed delta kinds；
- negative_fact_policy=never_infer_from_coverage。

governance_management_query_pack.v1.json 固定：

- query_pack_id/version、acquisition_scope；
- source_definition_id/version 引用，不复制域名和许可策略；
- question IDs、execution_key、query family、market applicability；
- date/page/canonical 语义和物理去重字段；
- 历史事件流或 current_observation_only 时间语义；
- discovery/fetch stage 和 required attachment。

validator 完成 canonical JSON、稳定 hash、精确问题集、question-query 穷尽追踪、历史窗口和多对多物理查询验证。相同物理查询可以覆盖多个问题，但每个问题保留独立 CoverageLink。

来源角色在治理领域固定为：

- official_disclosure；
- regulator_exchange；
- discovery_only；
- contextual_evidence；
- deferred。

SourceDefinition 的 allowlist、许可、归档、LLM、速率和并发仍由共享 registry 管理。治理 query pack 只能引用 enabled 定义，不能覆盖其策略。AKShare adapter 只能产生 discovery lead；法院、工商、中登在 v1 validator 中为 deferred。

备选方案是把 query 嵌入每个来源定义。这样新增治理问题会无意义地改变商业模式来源版本与 checkpoint，因此选择 source policy 与 scope query pack 分离。

### 4. 规范序列化、ID 与 hash

新增对象使用 Pydantic v2 严格模型，领域值默认 frozen。规范 JSON 规则集中在 canonical.py：

- UTF-8、ensure_ascii=false、对象 key 排序、无多余空白；
- datetime 必须 timezone-aware，先转 UTC，再以 Z 形式序列化；
- 同时保留原始时区和 time_precision；
- Decimal 以规范十进制字符串保存，不经二进制浮点；
- enum 保存稳定英文值；
- set-like 集合按稳定 ID/hash 排序；
- 事件和工具读取等语义有序集合保留顺序，并用 sequence 明确；
- null 是否进入 hash 由 schema version 固定；
- content hash 包含 schema/version 和所有业务字段，排除数据库 rowid、created_at 等非语义运行字段；
- 不同对象使用显式前缀 ID，防止跨 namespace 引用。

运行类对象使用不可预测 run ID 并另有 content hash；claim、evidence span、snapshot item 和派生产物尽量使用内容寻址 ID。相同输入/版本必须产生同一规范 payload hash。

备选方案是直接使用 model_dump_json 或数据库 payload 字符串求 hash。其字段顺序、datetime 和 Decimal 表达可能随版本漂移，因此拒绝。

### 5. 领域对象和存储形态

领域对象分为六组：

| 组 | 对象 |
|---|---|
| 抽取与证据 | GovernanceExtractionRun、GovernanceEvidenceSpan、GovernanceClaim、ValidationResult |
| 维护与不确定性 | ReviewDecision、GapRecord、ConflictRecord、CorrectionRecord |
| 人员 | GovernancePerson、PersonAlias、BiographyClaim、PersonLinkCandidate、PersonLinkDecision、RosterSnapshot、RoleTenure |
| 类型化事实 | OwnershipSnapshot/Position、ControlRelation、PledgePositionSnapshot、CompensationRecord、RelatedPartyRelation/Transaction、IncentivePlan/Grant/VestingCondition、AuditorEngagement、AuditOpinionRecord、InternalControlRecord、RegulatoryMatter、InquiryRecord、LitigationMatter、CommitmentRecord、GovernancePolicyVersion |
| 重建 | GovernanceSnapshot、SnapshotAnchorLink、SnapshotDeltaLink、SnapshotRecordLink |
| Codex | CodexInputPack、CodexToolRead、CodexSessionManifest、ResearchTask、ResearchResultBundle、QuarantinedResearchItem、SnapshotAdoption、GovernanceReport |

GovernanceEvidenceSpan 不修改旧 EvidenceSpan，而是增加 raw_snapshot_id、content_hash、derived_artifact_id、页/表/段/字符定位和 excerpt_hash；需要兼容展示时提供显式 mapper。

EventRecord 仍表示变化。类型化记录通过 source_event_ids 引用旧或新事件，但不依赖 event_terms 反序列化权威存量。ClaimRecord 继续表示报告叙述，不复用为字段级 GovernanceClaim。

持久层使用 append-only 表和不可变 payload，选择性提升查询字段：

- governance_extraction_runs；
- governance_evidence_spans；
- governance_claims；
- governance_validation_results；
- governance_review_decisions；
- governance_persons、governance_person_aliases；
- governance_person_link_candidates、governance_person_link_decisions；
- governance_records；
- governance_gaps、governance_conflicts；
- governance_snapshots；
- governance_snapshot_anchors、governance_snapshot_deltas、governance_snapshot_records；
- governance_codex_input_packs、governance_codex_sessions、governance_codex_tool_reads；
- governance_research_tasks、governance_research_results、governance_research_quarantine；
- governance_snapshot_adoptions、governance_reports。

governance_records 使用 record_kind + 严格版本化 payload 的混合模式，并提升 company_id、subject_id、reference/effective/available times、verification status、supersedes_record_id 和 canonical_hash 为列。强关系使用外键表，不把全部关系塞入 JSON。这样可以保持类型扩展能力，又能对双时点和血缘建立索引。

备选方案：

- 为每种类型建立完全独立表：schema 数量和迁移成本过高，跨类型查询复杂，拒绝。
- 所有对象只存一个 JSON 表：无法建立可靠外键、时点索引和唯一约束，拒绝。

### 6. SQLite 0007 与分析投影

0007_governance_management_v1 由 shared MigrationCoordinator 注册，前置条件是：

- 数据库已经完整处于 v6；
- 0006 migration row、schema、storage namespace 和 v6 recovery point 均通过验证；
- 备份与 data-root binding 符合共享 kernel 合同。

0007 在单一事务中创建治理表、索引、约束和 migration row，最后设置 user_version=7。任何 DDL/DML 失败回滚整个 0007。v7 重开必须 no-op；future version、dirty v0、迁移记录冲突或单独对 v5 执行 0007 必须在治理 DDL 前失败。

旧 SourceRecord、DocumentRecord、SyncResult、EventRecord、ReportVersion payload 和报告文件不被重写。功能回滚优先关闭 governance_management_v1 feature flag 并保留 v7 数据；旧程序不能直接打开 v7 工作库。若必须二进制回滚，先停止写入，验证并恢复 v6 recovery point 到独立路径。

SQLite 是事务权威。DuckDB/Parquet 仅作为可重建分析投影：

- 投影记录 source SQLite snapshot/version 和 canonical hash；
- 不接受独立写入；
- 启用时执行 SQLite-DuckDB-Parquet 数量、ID、hash 与时间边界一致性检查；
- 未启用投影不影响核心治理查询。

备选方案是在当前 v5 storage.py 直接追加表并占用 v6。它会与并行 acquisition 分支发生不可安全合并的迁移冲突，因此拒绝。

### 7. 抽取和权威准入流水线

流水线顺序：

    EvidenceSnapshotManifest
              |
              v
    verified raw/derived artifact loader
              |
              v
    deterministic extractor or candidate extractor
              |
              v
    field-level GovernanceClaim + EvidenceSpan
              |
              v
    type/unit/date/total/key/cross-field validators
              |
       +------+------+
       |             |
       v             v
    canonical     candidate/conflict
       |             |
       +------+------+
              v
       typed governance record

loader 只接受 manifest 中完整性通过且策略允许的 raw snapshot/derived artifact，不接受任意文件路径、URL 或 legacy DocumentRecord.path。

确定性 extractor 按 document kind、table signature、schema version 和规则版本注册。首批实现名册/任期、持股/控制/质押、薪酬/激励/关联交易、审计/内控、监管/问询/诉讼/承诺/制度/更正。每个 extractor 输出零个或多个字段级 claim，不直接写 canonical 表。

validator 覆盖：

- schema 类型与必填字段；
- Decimal、股数、比例、币种和单位；
- 合计与表内交叉关系；
- reference/effective/announced/available/retrieved 时间；
- 公司与人员 namespace；
- evidence span/hash；
- 数值计算基数；
- supersedes 和 conflict；
- 领域守恒条件。

canonical_eligible 是纯函数，仅当 official/regulator source、完整 hash/lineage、字段定位、deterministic、extraction complete、verification passed、review not_required 且无冲突时为 true，不能从数据库或 API 手填。

LLM 或歧义 regex 只能由 CandidateExtractor 写候选。approved ReviewDecision 可以在正式来源和全部校验均满足时派生新 canonical 版本，但该队列不被报告运行等待。普通 CodexRunner 没有 ReviewDecision 写权限。

备选方案是让 LLM 高置信结果自动入库，或让人工 review 成为每次报告门禁。前者破坏确定性证据边界，后者违背自主报告目标，均拒绝。

### 8. 人员身份和领域 reducer

人员 ID 使用 govp:{company_id}:{stable_local_key} 命名空间。stable_local_key 优先来自同一公司内的确定性任职/名册链；不足时使用持久随机 ID，不能用姓名作为全局 key。

PersonAlias 和 BiographyClaim 都有证据与 available_at。公司内解析可以使用确定性别名、同一公告上下文和连续任职链，但任何跨公司相似只生成 PersonLinkCandidate。

PersonLinkDecision：

- append-only；
- decision=approved 或 rejected；
- 保存依据、producer、available_at 和 supersedes_decision_id；
- 只有 snapshot known_at 当时可见的最新有效 approved 决定参与跨公司聚合；
- 从不改变公司内 person_id。

RoleTenure reducer 按 role taxonomy 区分 single-occupancy、multi-occupancy 和 scope-specific role。acting 是独立状态，不等同正式任职。只有明确证据才能关闭任期；valid_to=null 不代表确认 current。模糊辞任或无卸任说明的接任产生 candidate/conflict。

Ownership/Pledge reducer 只有在股数、比例基数、股本口径和增量数量完整时更新精确存量。部分解押缺数量只保留事件和 gap。所有 reducer 为纯函数，输入是已筛选 claim/anchor/delta，输出状态、采用/排除理由和不变量错误。

### 9. 双时点、日期精度与重建算法

查询使用 GovernancePerspective：

- strict：known_at 必须等于 state_at；
- reconstructed：known_at 必须大于或等于 state_at；大于时标记 future_knowledge_used；
- known_at 小于 state_at 在 v1 非法。

API 接受 RFC3339 datetime 或 ISO date。内部使用 QueryInstant：

- datetime 直接转 UTC；
- date 按 Asia/Shanghai 解释为该日结束的排他边界；
- 原始输入、时区和精度随查询保存。

仅有发布日期 D 的来源保存 availability 区间 [D 00:00, D+1 00:00)，保守 available_at_upper_bound 为 D+1 00:00 Asia/Shanghai。日内查询只有 cutoff 不早于 upper bound 才能纳入；日期级“截至 D”规范化到同一排他边界，因此可包含 D 日材料。若来源有可信精确发布时间，使用该时间。

重建伪代码：

    query = normalize(state_at, known_at, perspective)
    visible = claims where available_at_upper_bound <= query.known_cutoff
    visible_versions = resolve_supersedes_within(visible)
    approved_links = select_link_decisions_visible_at(query.known_cutoff)

    for each question and state kind:
        anchor = latest complete anchor where
                 reference_cutoff <= query.state_cutoff
        deltas = visible_versions where
                 effective_cutoff <= query.state_cutoff
                 and after anchor boundary
        deltas = stable_sort(
            supersedes precedence,
            effective_at,
            announced_at,
            stable_id,
        )
        state, applied, excluded, gaps, conflicts = reducer(anchor, deltas)

    snapshot_payload = canonicalize(
        query, versions, manifest, anchors, applied, excluded,
        canonical records, coverage, gaps, conflicts, candidates
    )

算法先做 available_at 门禁，后做任何更正解析和摘要生成。这样 strict 路径不会因“先看过未来更正、再决定忽略”而泄漏。

没有完整锚点时 reducer 可输出局部事实，但 completeness=incomplete。无法裁决的权威差异输出 conflicted，不选择或平均精确值。no_data 永不转为否定事实。

### 10. Snapshot service 和只读 view

GovernanceSnapshotService 接受固定 EvidenceSnapshotManifest、查询和版本包，完成重建、完整性校验、canonical hash 与幂等保存。

POST /api/companies/{ticker}/governance-snapshots 是唯一创建入口。相同 semantic hash 已存在时返回已有 snapshot 并注明 idempotent_reuse；新证据、规则或决定产生新 snapshot，并可显式引用 supersedes_snapshot_id。

GET /api/companies/{ticker}/governance-view 是纯读取 selector：

- 规范化双时点和版本；
- 只选择已有、完整性有效、精确匹配且 latest-consume-eligible 的 snapshot；
- 不联网、不抽取、不重建、不写入；
- 没有匹配项返回 404 snapshot_not_found，并提示使用显式 snapshot 创建流程。

GET /api/governance-snapshots/{id} 始终读取固定 snapshot。CodexInputPack 必须绑定固定 ID，不使用可漂移的 view selector。

备选方案是让 GET 临时重建并返回 view_hash。它会产生没有持久 ID、无法复查工具读取的临时状态，因此选择“只读选择已有 snapshot”。

### 11. API、CLI 与错误模型

src/analysis/governance/api.py 提供 APIRouter。GovernanceRuntime 由 composition root 注入 repository、snapshot service、acquisition port、Codex runner、research broker 和 renderer；中央 api.py 只 include router，不再由 router 自行构造默认依赖。

固定证据路径由 specs/governance-evidence-api 定义。除此之外增加：

- POST /api/companies/{ticker}/governance-reports；
- GET /api/governance-reports/{report_id}；
- GET /api/governance-sessions/{session_id}；
- GET /api/governance-reports/{report_id}/traces/{trace_name}；
- GET /api/governance-reports/{report_id}/exports/{format}。

写入口都要求显式 DB/data-root namespace 已由 composition root 绑定，API 不接受任意写入路径。

错误映射：

| 状态 | HTTP | 例子 |
|---|---:|---|
| validation_error | 422 | 非法双时点、schema、任意路径/URL |
| not_found | 404 | 未知 snapshot、record、span |
| immutable_conflict | 409 | ID 已被不同 hash 占用、adoption 版本冲突 |
| storage_busy | 503 | 有限 busy timeout 后仍无法写入 |
| integrity_failure | 500 | hash、血缘、future leakage、namespace 污染 |

所有错误返回 stable code、public detail 和 trace/correlation ID，不返回本机绝对路径或敏感参数。

CLI 新增 analysis.cli governance：

- snapshot create/show；
- report create/show；
- trace render；
- validate；
- export。

联网 acquisition 仍由共享 acquire 命令负责。治理写命令必须显式提供 DB 和 data-root；只读 show 可以从已绑定 runtime 读取。JSON 输出使用相同 Pydantic response schema。

### 12. Codex input、工具和实际读取清单

CodexInputPackBuilder 从固定 snapshot 生成：

- 公司和双时点；
- snapshot/manifest/version/hash；
- 11 问题 coverage、anchor 和 completeness 摘要；
- 重要 canonical facts/events 索引；
- 全部 active gap/conflict/pending candidate ID；
- 工具 JSON schemas；
- research budget 与严格时点规则；
- GovernanceReport JSON Schema。

压缩只作用于事实摘要，不得丢弃活动不确定性 ID。原文通过 snapshot-bound 工具按需读取。

CodexToolService 每次调用：

1. 校验 session 绑定 snapshot；
2. 校验参数、来源 LLM policy 和 known_at；
3. 取得稳定 payload；
4. 在返回给 Codex 前原子持久化 CodexToolRead，含 sequence、规范参数、payload artifact/hash、实际 record/claim/span/raw IDs；
5. 返回相同 payload。

若 read manifest 无法持久化，正文不返回给 Codex，避免出现“读过但无法审计”。工具没有候选批准、snapshot 切换或事实写权限。

CodexSessionManifest 是 tool reads、research tasks、snapshot adoptions 和 final citations 的不可变索引。它不保存隐藏推理；模型输出中明确返回的分析文本属于报告 artifact，而不是 chain-of-thought。

### 13. Fresh-context Codex runner

应用层定义两个协议：

- CodexRunner：给定固定 input pack、工具代理和 output schema，返回结构化 GovernanceReport。
- ResearchTaskBroker：给定最小 ResearchTask，返回 ResearchResultBundle artifact ID，不直接返回未经 gate 的浏览叙述。

离线测试使用 DeterministicCodexRunner 和 FakeResearchTaskBroker，仅证明协议、状态和门禁。

生产初始 adapter 选择 CodexExecRunner。规划时本机 CLI 已能以非交互、ephemeral、JSONL、JSON Schema、live search 和 read-only sandbox 运行；adapter 仍必须在启动时探测实际版本与所需 flag，缺失能力失败关闭，不能把本机一次验证当成永久产品 API。

进程启动使用 argv 数组，不经过 shell 拼接，形态为：

    codex
      --search
      --sandbox read-only
      --ask-for-approval never
      --cd <isolated-task-root>
      exec
      --ephemeral
      --output-schema <research-result-schema>
      --json
      -

prompt 通过 stdin 传入；模型沿用受控运行 profile，不在任务中写 token。子任务根目录只包含最小任务 JSON 和输出 schema，无父上下文或私有数据库。JSONL 事件写入受限 research artifact/quarantine，用于预算计数和诊断，不直接注入父 Codex；只有最终结构化 bundle 进入 ResearchGate。

预算由 broker 和 process supervisor 双重执行：

- parent round、child count、parallelism、wall clock；
- 从 JSONL tool events 计数的网络请求上限；
- recursion_depth=1；
- stdout/stderr/result 大小上限；
- 超时或越界终止进程并写 budget_exhausted/failed 状态。

备选方案：

- 直接依赖 Codex Desktop 的任务 UI：项目 Python 没有已确认稳定接口，拒绝作为 v1 生产合同。
- 在同一父模型上下文直接浏览：无法保证未来内容未进入模型，严格模式拒绝。
- 引入 OpenAI SDK 并自行重建全部工具：增加凭据、模型和工具编排面，且不满足“使用现有 Codex 能力”的目标，v1 不选。

### 14. Research gate 和未来信息防火墙

ResearchTask 只包含 ticker/company ID、问题、state/known/perspective、gap IDs、必要证据 IDs、允许来源、预算和 result schema。

ResearchResultBundle 首先写入父上下文不可见的 staging store。ResearchGate 对每项执行：

1. schema/hash/task 关联校验；
2. URL/source candidate 分类；
3. 许可和 allowlist 检查；
4. 受控重新获取与原始字节冻结；
5. announced_at/available_at/retrieved_at 与精度验证；
6. strict known_at eligibility；
7. authoritative candidate、contextual、discovery、deferred 或 quarantine 路由。

父 Codex 得到 SanitizedResearchReceipt：

- eligible evidence/record/artifact IDs；
- eligible contextual items；
- discovery lead 状态；
- unresolved gaps；
- quarantine count 和非实质 reason code。

receipt 对 quarantine 项不包含 title、summary、URL path 中的语义文本、数值或结论。即使父 Codex拥有开放联网能力，strict session 的网络请求也必须通过 broker/gate，不提供未受控直接浏览工具。

ResearchResultBundle schema/hash 非法属于 hard failure，因为系统无法证明父子边界。正常超时、403/429、未找到资料和预算耗尽是 soft state。

reconstructed session 使用相同 gate，只是 known_at 可晚于 state_at；任何 item 仍须 available_at 不晚于显式 known_at。

### 15. 显式 snapshot adoption

补采从不修改 session 当前 snapshot。ResearchGate 完成后可建议一个新 snapshot ID；Codex 必须调用 adopt_snapshot(old_id, new_id, expected_session_revision)。

adoption 事务验证：

- old 是 session 当前 snapshot；
- new 与同一 company/scope/namespace 对应；
- new 的双时点和 perspective 合法；
- new manifest 和 integrity checks 通过；
- strict future firewall 通过；
- expected revision 匹配。

成功后追加 SnapshotAdoption 并递增 session revision。旧 input pack、tool reads 和草稿 artifact hash 均保留；后续工具绑定新 snapshot。失败返回 409 或 integrity failure，不隐式切换。

### 16. 自主治理报告

GovernanceReportService 不调用现有 AnalysisService.review。它使用固定 CodexRunner 和 GovernanceReport schema：

- company、state_at、known_at、perspective；
- snapshot、session manifest；
- generation_status、decision_author=codex；
- 所有权控制、人员与董事会、薪酬激励、关联方、审计内控、监管诉讼承诺等章节；
- evidence-backed findings；
- contextual findings；
- Codex judgments；
- gaps/conflicts/candidates；
- citations；
- future_knowledge_used；
- technical validation。

事实、背景和判断使用不同 schema 节点。报告可以包含管理层诚信或能力的 Codex judgment，但权威数据模型没有对应字段，报告写入也不触发 claim admission。

complete、incomplete、conflicted、no_data、pending candidate、未决 link、来源受限、child 失败和 budget exhausted 都可进入 generation_status=completed。只有 specs 列出的技术完整性错误阻断完成。

报告生成不等待人工。可选人工 ReviewDecision 只改变后续数据 snapshot；开发期人工黄金验收只验证实现和投影。

### 17. 报告导出与八份 trace

GovernanceReport JSON 是报告权威对象。GovernanceReportView 是所有格式的单一规范投影：

- Markdown 和 HTML 使用同一章节/引用模型；
- XLSX 使用固定 sheets：Summary、Findings、Evidence、Gaps、Conflicts、Session；
- PDF 由同一 HTML view 经现有受控渲染路径产生；
- 每种输出固定 report ID/hash、双时点、snapshot、manifest 和 generation status。

不同格式不得重新调用 Codex或重算治理事实。导出器只读 GovernanceReportView；格式差异不产生新判断。

每个 run 在绑定 data-root 下生成：

    var/runs/<run_id>/trace/
      01-acquisition-coverage.md
      02-evidence-manifest.md
      03-extraction-results.md
      04-entity-resolution.md
      05-governance-reconstruction.md
      06-codex-input-pack.md
      07-codex-assessment.md
      08-report-validation.md

TraceRenderer 接收权威对象 ID，重新读取并确定性渲染；不接受手写 Markdown 作为输入。05 显示 anchor、applied/excluded delta 和原因；06 证明所有活动 gap/conflict/candidate 已交给 Codex；07 分开正式事实、contextual evidence 和 judgment；08 分开 hard checks 与 soft states。

trace 与报告输出经过统一 redactor，禁止 token、Cookie、owner token、浏览器配置、未脱敏 URL 参数、本机绝对路径和隐藏推理。原始研究 JSONL 只在受限 artifact/quarantine 中保存 hash 和最小诊断，不进入可读 trace。

### 18. Frontend 决策

v1 不新增前端页面，避免在方法仍为 skeleton 时把数据底座包装成完整治理产品。OpenAPI schema、稳定 API、CLI、Markdown/HTML/XLSX/PDF 和 trace 足够支持开发、Codex 与审计。

现有前端若存在，只能继续使用旧报告和事件接口；feature flag 关闭时行为不变。未来 UI change 可以消费固定 snapshot 和旁路状态，但不得从 view 结果隐藏 incomplete/conflicted/candidate。

### 19. 测试结构与验收门

离线自动化以设计文档 A01-A25 为主矩阵：

- models/admission：枚举、canonical hash、确定性准入、LLM 隔离；
- registry：11 问题、历史范围、source roles、query M:N、AKShare/deferred；
- entity resolution：同名、别名、多职、代理、模糊辞任、link decision 双时点；
- reconstruction：严格/事后、日期精度、锚点增量、更正、乱序、数值守恒；
- snapshot/repository：幂等、append-only、supersedes、fault injection；
- API/CLI：固定路径、纯读 view、错误映射、稳定分页、路径拒绝；
- Codex：compact pack、actual reads、budget、future quarantine、adoption、hard/soft；
- report/export/trace：三层表达、无人工门、四格式同源、八份投影；
- compatibility：旧 EventRecord、reporting、API、exports、storage。

真实联网门单独使用隔离 DB/data-root，至少覆盖 600519、300750、复杂任免、监管决定和质押部分解除。具体公告 ID 在只读预检后固定，不在 planning 中猜测。真实 Codex runner 与 future quarantine 必须单独观察；fake 通过不能标记该门通过。

人工黄金门核对字段 evidence span、多人多职务、身份、状态机、strict/reconstructed 差异和八份 trace。签署只证明实现与投影，不参与普通报告。

OpenSpec strict validation、pytest、方法库、既有黄金结构、SQLite-DuckDB-Parquet consistency、在线和人工状态分别记录。任何一门不得推导另一门。

### 20. 安全与隐私

- 所有外部 URL 必须来自版本化 registry 或进入 candidate。
- subprocess 使用 argv、stdin 和固定 env allowlist，不使用 shell=true。
- child sandbox 为 read-only；输出目录由父进程预建并限制大小。
- evidence excerpt 只按 span ID 读取，并再次检查 session known_at 与 LLM policy。
- API、日志、trace 和错误只返回相对 artifact ID/hash，不返回 DB/data-root 绝对路径。
- session manifest 保存实际读取，不保存 chain-of-thought。
- quarantine 与合格 evidence 使用不同 repository capability，父 runtime 无权读取隔离正文。
- 所有写入先校验 namespace/scope，跨公司 link 也不能改变公司内主键。

## Risks / Trade-offs

- [共享 kernel 尚未实现] → 纯治理逻辑先用 port/fake，0007、生产采集和最终 composition 明确等待 shared prerequisite；不以 stub 冒充完成。
- [并行分支的共享合同最终变化] → 集成前运行 prerequisite validator；若 contract 不满足 spec，暂停并更新本 change，不复制修补 kernel。
- [CLI 不是永久 API] → CodexRunner 隔离、启动时 capability probe、结构化 schema 和版本记录；未来可替换为官方稳定 adapter 而不改领域合同。
- [子 Codex 已看见未来内容] → staging/quarantine 与父权限隔离，先 gate 后返回，未来项只泄漏计数和 reason code。
- [日期级 available_at 过度保守] → 保存原始精度和区间；宁可产生 gap，也不提前纳入。
- [完整历史成本高] → checkpoint/overlap 由 shared kernel 处理，问题级 coverage 明确；预算耗尽继续报告但不能声称 complete。
- [LLM candidate 长期堆积] → 允许 Codex读取并判断，不阻塞报告；按问题和重要性提供可选维护队列。
- [混合 governance_records 表弱化类型约束] → Pydantic discriminated union、schema version、提升列 CHECK、外键和 round-trip tests 共同约束。
- [报告判断与权威事实混淆] → schema 三层分区、不同 repository 写权限、禁止 report runner 写 GovernanceClaim。
- [没有新前端降低可见性] → API、CLI 和八份 trace 先成为可验收界面；UI 另开 change，避免并行扩大范围。
- [在线来源许可或反爬变化] → registry 版本、policy failure、隔离样本；不绕过限制，允许在线门保持 pending。
- [四种导出格式产生漂移] → 单一 GovernanceReportView、无模型重跑、跨格式 ID/hash/citation tests。

## Migration Plan

1. 在本分支提交 OpenSpec、治理配置 schema、纯领域模型、canonical 序列化、ports、fake 和冻结夹具。
2. 实现确定性抽取、admission、人员解析、reducers、双时点 reconstruction、in-memory snapshot service 和全部离线算法测试。
3. 实现 Codex input/tool manifest、ResearchGate、fake runner、CodexExecRunner contract、报告与 trace 的纯应用层测试。
4. 等 business-model-acquisition-v1 的 shared kernel 与 0006 可合入后，合并或变基到本分支。
5. 运行 governance prerequisite validator；若失败，先协调共享合同，不实现平行 kernel。
6. 注册并事务化执行 0007，完成 SQLite repository、SharedAcquisitionGateway 和 namespace 一致性检查。
7. 注入 GovernanceRuntime，挂载 router/CLI，接入四格式导出和 feature flag。
8. 运行治理离线测试、全量回归、方法/既有黄金结构、OpenSpec strict、diff 和敏感信息门。
9. 完成来源许可预检后，在隔离 namespace 执行沪深、监管、复杂任免、质押和真实 Codex 试点；将真实状态写入阶段日志。
10. 完成开发期人工黄金签署后开启 feature flag；运行时不等待人工审批。

回滚：

- 代码回滚优先关闭 governance_management_v1，旧事件级第三步继续工作。
- v7 数据、原始快照、session、report 和 trace manifest 保留，不删除或改写。
- 需要旧二进制时停止所有写入，验证 v6 recovery point 后恢复到独立路径；旧程序不得打开 v7 工作库。
- 已发布 GovernanceSnapshot 和 GovernanceReport 永远按 ID/hash 可读。

## Open Questions

以下只影响部署参数，不改变 spec、架构或任务拆分：

- 生产 Codex profile/model 名称和每类研究问题的初始预算值，在真实在线门前由环境配置冻结。
- 证监会及各证监局具体 endpoint/allowlist、保留许可和速率，在联网前逐项只读预检；未确认定义保持 disabled。
- GovernanceReport 是否默认同时生成四种格式，或按请求惰性生成，可在不改变单一 view 和同源保证的前提下由运行配置决定。
