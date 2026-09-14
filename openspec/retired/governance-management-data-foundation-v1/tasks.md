> 历史参考，已于2026-09-14退出活动队列；原任务状态保留，不作当前执行指令。当前任务见 [eight-step-production-pipeline-v1](../../changes/eight-step-production-pipeline-v1/tasks.md)。

## 1. 并行安全边界与治理注册配置

- [x] 1.1 创建 src/analysis/governance 包、GovernanceAcquisitionPort 和 tests/governance/fakes.py，只引用 shared kernel 的 run、coverage、raw snapshot、manifest 与补采协议，不定义 GovernanceRun、GovernanceCheckpoint、ContentBlob 或 EvidenceSnapshotManifest；运行 python -m pytest tests/governance/test_acquisition_port.py -q，证明 fake 可驱动治理层且不存在第二套控制面。
- [x] 1.2 创建 config/data_sources/governance_management_questions.v1.json，精确固定 GOV.Q01 至 GOV.Q11、目标记录、history policy、anchor/delta 类型和禁止从 coverage 推断负面事实的规则；运行 python scripts/validate_governance_registry.py --questions config/data_sources/governance_management_questions.v1.json，输出 GOVERNANCE_QUESTIONS_OK questions=11。
- [x] 1.3 创建 config/data_sources/governance_management_query_pack.v1.json，引用共享 SourceDefinition、分离来源策略与 scope 查询语义，并区分历史事件流和 current_observation_only 网页；运行 python -m pytest tests/governance/test_registry.py -q -k "query_pack or history_horizon or mutable_page"。
- [x] 1.4 固定 official_disclosure、regulator_exchange、discovery_only、contextual_evidence、deferred 来源角色，技术上限制 AKShare 为 discovery_only 并排除法院、工商和中登；运行 python -m pytest tests/governance/test_registry.py -q -k "source_role or akshare or deferred"。
- [x] 1.5 实现治理配置 schema、canonical JSON、稳定 hash、精确问题集、question-query 穷尽追踪和物理查询多对多映射校验；运行 python scripts/validate_governance_registry.py --questions config/data_sources/governance_management_questions.v1.json --query-pack config/data_sources/governance_management_query_pack.v1.json --require-plan-traceability，输出 GOVERNANCE_REGISTRY_OK，任何未知/缺失问题或错误去重须非零退出。
- [x] 1.6 增加 scope 隔离夹具，证明同 ticker 的 business_model 与 governance_management 不共享 checkpoint、manifest 或 latest selector，且新增治理问题不改变商业模式 query-pack hash；运行 python -m pytest tests/governance/test_registry.py -q -k "scope_identity or checkpoint_key or latest_selector"。
- [x] 1.7 增加边界测试，确保治理事实 schema 不含治理总分、治理评级、管理层诚信或能力字段，且 docs/methodology/steps/03_governance.md 仍保持 skeleton；运行 python -m pytest tests/governance/test_scope_boundaries.py -q。

## 2. 规范序列化与治理领域模型

- [x] 2.1 在 canonical.py 实现 UTF-8 规范 JSON、UTC aware 时间、Asia/Shanghai 日期精度、Decimal 字符串、集合稳定排序和 schema-aware SHA-256；运行 python -m pytest tests/governance/test_canonical.py -q，覆盖相同输入稳定 hash、naive datetime 拒绝和 Decimal 无浮点漂移。
- [x] 2.2 在 models.py 增加来源角色、抽取器种类、三轴状态、完整性、双时点 perspective、报告生成状态和 time precision 封闭枚举；运行 python -m pytest tests/governance/test_models.py -q -k "enum or timezone or perspective"。
- [x] 2.3 实现不可变 GovernanceExtractionRun、GovernanceEvidenceSpan、GovernanceClaim 和 ValidationResult，包含 raw snapshot/content hash、页表段字符定位、摘录 hash、抽取/schema 版本和 supersedes；运行 python -m pytest tests/governance/test_models.py -q -k "extraction_run or claim or evidence_span or validation"。
- [x] 2.4 实现纯函数 canonical_eligible，严格要求正式来源、完整 hash/lineage、字段定位、deterministic、complete、passed、not_required 和无冲突，且属性不可由外部填写；运行 python -m pytest tests/governance/test_admission.py -q。
- [x] 2.5 实现 append-only ReviewDecision、GapRecord、ConflictRecord 和 CorrectionRecord；运行 python -m pytest tests/governance/test_models.py -q -k "review or gap or conflict or correction"，证明批准不覆盖候选历史、非正式来源不能靠复核升级。
- [x] 2.6 实现 GovernancePerson、PersonAlias、BiographyClaim、PersonLinkCandidate/Decision、RosterSnapshot 和 RoleTenure；运行 python -m pytest tests/governance/test_models.py -q -k "person or alias or biography or roster or tenure or person_link"。
- [x] 2.7 实现 OwnershipSnapshot/Position、ControlRelation 和 PledgePositionSnapshot，保留股数、比例、基数、股本口径和完整性；运行 python -m pytest tests/governance/test_models.py -q -k "ownership or control or pledge"。
- [x] 2.8 实现 CompensationRecord、RelatedPartyRelation/Transaction、IncentivePlan/Grant/VestingCondition、AuditorEngagement、AuditOpinionRecord 和 InternalControlRecord；运行 python -m pytest tests/governance/test_models.py -q -k "compensation or related_party or incentive or auditor or audit_opinion or internal_control"。
- [x] 2.9 实现 RegulatoryMatter、InquiryRecord、LitigationMatter、CommitmentRecord 和 GovernancePolicyVersion；运行 python -m pytest tests/governance/test_models.py -q -k "regulatory or inquiry or litigation or commitment or policy"。
- [x] 2.10 实现 GovernanceSnapshot 及 anchor/delta/record links，并固定 scope/question/source/query/extractor/reconstruction 版本、coverage、不确定性和 hash；运行 python -m pytest tests/governance/test_models.py -q -k "snapshot or anchor_link or delta_link or record_link"。
- [x] 2.11 实现 CodexInputPack、CodexToolRead、CodexSessionManifest、ResearchTask/ResultBundle、QuarantinedResearchItem、SnapshotAdoption 和 GovernanceReport；运行 python -m pytest tests/governance/test_models.py -q -k "codex or research or quarantine or adoption or report"。
- [x] 2.12 为所有 discriminated union 和不可变对象增加 JSON Schema 与 round-trip 测试；运行 python -m pytest tests/governance/test_model_roundtrip.py -q，确保未知 kind、额外字段、错误 namespace 和非规范 payload 失败关闭。

## 3. 冻结证据抽取与权威准入

- [x] 3.1 实现 manifest-bound extraction loader，只接受 GovernanceAcquisitionPort 返回且完整性/LLM policy 合格的 raw snapshot 或 derived artifact，不接受任意文件路径、URL 或 legacy DocumentRecord.path；运行 python -m pytest tests/governance/test_extraction.py -q -k "manifest_only or arbitrary_path or policy or lineage"。
- [x] 3.2 实现字段类型、单位、货币、日期、主键、合计、比例基数、证据定位和跨字段校验器；运行 python -m pytest tests/governance/test_extraction.py -q -k "validator or unit or total or date or basis or evidence"。
- [x] 3.3 实现名册、人员履历和任职表的确定性抽取，支持多人、多职务、代理和换届；运行 python -m pytest tests/governance/test_extraction.py -q -k "roster or biography or multiple_people or multiple_roles or acting or renewal"。
- [x] 3.4 实现股东持股、控制关系、质押及解除质押的确定性抽取；运行 python -m pytest tests/governance/test_extraction.py -q -k "ownership or control or pledge"，缺数量或基数时只能输出不完整声明。
- [x] 3.5 实现薪酬、激励计划、授予/归属条件和关联交易的稳定表格抽取；运行 python -m pytest tests/governance/test_extraction.py -q -k "compensation or incentive or vesting or related_party"。
- [x] 3.6 实现审计聘任、签字人员、审计意见、内控意见、缺陷及整改的确定性抽取；运行 python -m pytest tests/governance/test_extraction.py -q -k "auditor or signing_auditor or audit_opinion or internal_control"。
- [x] 3.7 实现监管、问询、正式披露诉讼、承诺、制度版本及更正的类型化抽取；运行 python -m pytest tests/governance/test_extraction.py -q -k "regulatory or inquiry or litigation or commitment or policy or correction"。
- [x] 3.8 实现 extraction orchestrator，每次执行创建新 GovernanceExtractionRun，先保存 claims/validation 再由 admission 派生 typed records；运行 python -m pytest tests/governance/test_extraction.py tests/governance/test_admission.py -q -k "immutable_run or orchestration or deterministic_admission"。
- [x] 3.9 实现 CandidateExtractor 边界，使全部 LLM 与有歧义 regex 输出保持 candidate，ReviewDecision 为可选独立维护路径且普通报告 runner 无批准权限；运行 python -m pytest tests/governance/test_admission.py -q -k "llm_candidate or ambiguous_regex or optional_review or runner_cannot_approve"。
- [x] 3.10 增加标题召回、同源镜像、来源冲突和字段 evidence span 故障夹具；运行 python -m pytest tests/governance/test_extraction.py tests/governance/test_admission.py -q -k "title_only or mirror or source_conflict or broken_span"。

## 4. 人员身份与类型化状态 reducer

- [x] 4.1 实现公司命名空间内的人员身份解析和稳定 local ID；运行 python -m pytest tests/governance/test_entity_resolution.py -q -k "company_local or same_name or alias"，两家公司同名人员不得合并。
- [x] 4.2 实现 PersonLinkCandidate 和 append-only PersonLinkDecision；运行 python -m pytest tests/governance/test_entity_resolution.py -q -k "link_candidate or approved or rejected or decision_time"，只有 snapshot known_at 可见的最新 approved 决定影响聚合。
- [x] 4.3 实现 role taxonomy 的 single/multi occupancy、名册锚点和任期 reducer；运行 python -m pytest tests/governance/test_entity_resolution.py -q -k "roster or role_tenure or multi_occupancy or valid_to"。
- [x] 4.4 实现任命、辞任、代理、换届、续任和更正规则；运行 python -m pytest tests/governance/test_entity_resolution.py -q -k "appointment or resignation or acting or renewal or correction or overlap"，模糊辞任不得关闭全部角色。
- [x] 4.5 实现持股、控制链和质押存量 reducer；运行 python -m pytest tests/governance/test_entity_resolution.py -q -k "ownership_state or control_chain or partial_release or conservation"，数字不足时保留事件但不计算精确余额。
- [x] 4.6 实现其余类型化记录的区间/年度投影、冲突和更正关系；运行 python -m pytest tests/governance/test_entity_resolution.py -q -k "annual_record or interval_record or conflict or supersedes"。
- [x] 4.7 增加旧 EventRecord 到 source_event_ids 的单向兼容 mapper；运行 python -m pytest tests/governance/test_event_compatibility.py -q，证明旧事件 payload 不变且自由 event_terms 不会自动成为 canonical 状态。

## 5. 双时点重建与不可变画像

- [x] 5.1 实现 state_at/known_at/perspective 规范化：默认 strict、strict 两时点相等、隐式 hindsight 和 known_at 小于 state_at 为领域校验错误；运行 python -m pytest tests/governance/test_bitemporal_reconstruction.py -q -k "default_strict or explicit_reconstructed or invalid_time"。
- [x] 5.2 实现 RFC3339 timestamp 与 ISO date 的 QueryInstant、Asia/Shanghai 日结束排他边界和 date-only available_at 保守上界；运行 python -m pytest tests/governance/test_bitemporal_reconstruction.py -q -k "date_only or intraday or timezone or conservative_boundary"。
- [x] 5.3 实现先按 available_at 过滤、再解析 correction/withdrawal/supersedes 的版本选择器；运行 python -m pytest tests/governance/test_bitemporal_reconstruction.py -q -k "availability_first or supersedes or withdrawal or late_correction"。
- [x] 5.4 实现通用 anchor 选择和 delta 排序框架，顺序固定为 supersedes 优先、业务生效时间、公告时间、稳定 ID；运行 python -m pytest tests/governance/test_bitemporal_reconstruction.py -q -k "anchor or delta_order or out_of_order or excluded_reason"。
- [x] 5.5 接入名册和任期重建，覆盖完整锚点、任命、辞任、代理、换届、续任、更正和无锚点局部事实；运行 python -m pytest tests/governance/test_bitemporal_reconstruction.py -q -k "roster_timeline or no_anchor or appointment or resignation or acting or renewal"。
- [x] 5.6 接入持股、控制链和质押重建；运行 python -m pytest tests/governance/test_bitemporal_reconstruction.py -q -k "ownership or control or pledge or incomplete_balance"。
- [x] 5.7 接入其余治理问题的区间/年度状态、question-level coverage、gap、conflict 和 no_data 语义；运行 python -m pytest tests/governance/test_bitemporal_reconstruction.py -q -k "coverage or incomplete or conflicted or no_data or annual"。
- [x] 5.8 实现 in-memory GovernanceSnapshotService 的稳定排序、semantic hash、幂等创建、supersedes 和完整性校验；运行 python -m pytest tests/governance/test_snapshot_service.py -q，证明相同输入复算 hash 相同、新资料产生新 snapshot、旧 snapshot 不变。
- [x] 5.9 注入未来 claim、断裂 lineage、跨 scope/namespace 和 hash mismatch，验证 snapshot 发布失败关闭；运行 python -m pytest tests/governance/test_snapshot_service.py -q -k "future_leak or broken_lineage or cross_scope or hash_mismatch"。

## 6. Codex 按需取证、委派和自主报告

- [x] 6.1 实现紧凑 CodexInputPackBuilder，包含固定 snapshot、11 个问题摘要和全部活动 gap/conflict/candidate ID，但不预塞大段原文；运行 python -m pytest tests/governance/test_codex_tools.py -q -k "compact_pack or all_active_ids or no_fulltext"。
- [x] 6.2 实现 snapshot-bound 的只读治理工具注册与参数 schema，所有响应带对象 ID、版本、hash 和引用；运行 python -m pytest tests/governance/test_codex_tools.py -q -k "readonly or snapshot_bound or schema or hash"。
- [x] 6.3 实现“先原子记录、后返回正文”的 CodexToolRead 和 session recorder，保存规范参数、顺序、payload artifact/hash、实际对象与引用，不保存凭据或隐藏推理；运行 python -m pytest tests/governance/test_codex_tools.py -q -k "session_manifest or actual_reads or persist_before_return or redaction"。
- [x] 6.4 实现可注入 ResearchTaskBroker、最小任务合同和轮数/子任务/请求/并发/时间/输出大小/递归预算，v1 深度固定为 1；运行 python -m pytest tests/governance/test_research_gate.py -q -k "minimal_payload or budget or recursion or output_limit"。
- [x] 6.5 实现 ResearchResultBundle schema/hash/task 关联校验及 authoritative candidate、contextual、discovery、deferred、unresolved 分类；运行 python -m pytest tests/governance/test_research_gate.py -q -k "bundle_schema or task_binding or classification or authoritative_reingest"。
- [x] 6.6 实现 parent-invisible staging、ResearchGate 和 quarantine，使晚于或无法证明不晚于 known_at 的内容只返回计数/reason code；运行 python -m pytest tests/governance/test_research_gate.py -q -k "future_firewall or quarantine or no_title_leak or no_summary_leak"。
- [x] 6.7 实现 SanitizedResearchReceipt，确保 official candidate 重走受控采集、contextual 同样受时点门、AKShare/摘要仅作 lead；运行 python -m pytest tests/governance/test_research_gate.py -q -k "sanitized_receipt or contextual_time or discovery_only"。
- [x] 6.8 实现显式 adopt_snapshot(old,new,expected_revision) 与 append-only SnapshotAdoption；运行 python -m pytest tests/governance/test_research_gate.py -q -k "adopt_snapshot or no_implicit_switch or revision_conflict"，切换前 input/tool/draft hash 必须不变。
- [x] 6.9 实现 CodexRunner 协议、DeterministicCodexRunner 和 FakeResearchTaskBroker；运行 python -m pytest tests/governance/test_codex_runner_contract.py -q，测试结果只能标记 protocol_passed，不得标记真实 Codex 或联网通过。
- [x] 6.10 实现 CodexExecRunner capability probe 和 argv/stdin 启动器，要求非交互、ephemeral、output schema、JSONL、live search、read-only sandbox，不使用 shell 拼接；运行 python -m pytest tests/governance/test_codex_exec_runner.py -q -k "capability_probe or argv or stdin or no_shell or env_allowlist"。
- [x] 6.11 实现 CodexExecRunner process supervisor，从 JSONL 计数工具/网络请求并执行 wall-clock、并发、stdout/stderr/result 大小和递归预算；运行 python -m pytest tests/governance/test_codex_exec_runner.py -q -k "timeout or request_budget or size_limit or terminate or ephemeral"。
- [x] 6.12 实现 GovernanceReportService 及结构化校验，complete/incomplete/conflicted/no_data/受限/预算耗尽均无需人工即可 completed，技术 hash/lineage/future/time/namespace/bundle/session/report schema 错误才阻断；运行 python -m pytest tests/governance/test_autonomous_report.py -q。
- [x] 6.13 增加事实、contextual evidence 和 Codex judgment 三层报告权限测试，允许 Codex 作诚信/能力定性判断但禁止其反写 GovernanceClaim，并且不要求治理总分；运行 python -m pytest tests/governance/test_autonomous_report.py -q -k "layering or judgment or no_fact_write or no_score"。

## 7. 报告导出与人类可读 trace

- [x] 7.1 实现单一 GovernanceReportView，固定 report/session/snapshot/manifest/hash、双时点、章节、引用和不确定性；运行 python -m pytest tests/governance/test_report_rendering.py -q -k "canonical_view or identifiers or sections"。
- [x] 7.2 实现 Markdown 和 HTML renderer，二者不调用 Codex、不重建事实且引用集合一致；运行 python -m pytest tests/governance/test_report_rendering.py -q -k "markdown or html or same_citations"。
- [x] 7.3 实现 XLSX renderer 的 Summary、Findings、Evidence、Gaps、Conflicts、Session sheets；运行 python -m pytest tests/governance/test_report_rendering.py -q -k "xlsx or sheets or same_snapshot"。
- [x] 7.4 实现由同一 HTML view 生成 PDF 的受控 renderer；运行 python -m pytest tests/governance/test_report_rendering.py -q -k "pdf or same_report_hash"，不得重新调用 Codex 或改变判断。
- [x] 7.5 实现 TraceRenderer，自动生成 01-acquisition-coverage.md 至 08-report-validation.md；运行 python -m pytest tests/governance/test_trace_projection.py -q -k "eight_files or projection or anchor_delta or all_active_ids"。
- [x] 7.6 实现 trace/report 统一 redactor、UTF-8 和 hard-failure trace；运行 python -m pytest tests/governance/test_trace_projection.py -q -k "redaction or utf8 or hard_failure"，不得出现 token、Cookie、owner token、浏览器配置、隐藏推理或未脱敏绝对路径。
- [x] 7.7 实现相同权威对象与 renderer 版本的稳定重渲染；运行 python -m pytest tests/governance/test_trace_projection.py tests/governance/test_report_rendering.py -q -k "deterministic or stable"，比较规范化后的逐字节输出。

## 8. Shared kernel、SQLite 0007 与持久化集成

- [ ] 8.1 在 scripts/check_governance_prerequisites.py 校验 shared kernel 已真实提供 acquisition_scope、question_set_id/version、source_registry_version、query_pack_version，且进入 run、coverage、checkpoint、manifest、latest selector、API 和 CLI；合入 shared kernel 后运行脚本须输出 GOVERNANCE_PREREQUISITES_OK，不满足时暂停并更新 artifacts，不在治理分支复制 kernel。
- [ ] 8.2 实现 SharedAcquisitionGateway，使治理 scope 复用统一 planner、CNINFO/SSE/SZSE/监管 adapter、coverage、checkpoint、raw snapshot 和 manifest；运行 python -m pytest tests/governance/test_acquisition_integration.py -q，证明治理与 business-model 不共享 checkpoint/latest selector且共享物理查询不丢失问题级 coverage。
- [ ] 8.3 将治理 question/query pack 接入共享 registry validator 和两阶段 acquisition API；运行 python -m pytest tests/governance/test_acquisition_integration.py -q -k "registry or create_run or execute_run or manifest"，AKShare 必须只产生 discovery lead。
- [ ] 8.4 注册 0007_governance_management_v1，要求完整 v6 和已验证 recovery point，并在单一事务创建治理表/索引/外键；运行 python -m pytest tests/governance/test_migration.py -q -k "fresh_to_v7 or v5_direct_rejected or v6_to_v7 or v7_noop or future_version or dirty or fault_rollback"。
- [ ] 8.5 实现 extraction run、span、claim、validation、review、人员、链接决定和类型化 record 的 SQLite repository；运行 python -m pytest tests/governance/test_repository.py -q -k "extraction or claim or review or person or typed_record or immutable"。
- [ ] 8.6 实现 snapshot、anchor/delta/record links、gap、conflict、coverage 和 lineage repository；运行 python -m pytest tests/governance/test_repository.py -q -k "snapshot or anchor or delta or gap or conflict or lineage or idempotent"。
- [ ] 8.7 实现 Codex input/session/tool reads、research task/result/quarantine、snapshot adoption 和 report repository；运行 python -m pytest tests/governance/test_repository.py -q -k "codex or research or quarantine or adoption or report or atomic_read"。
- [ ] 8.8 创建 scripts/validate_governance_consistency.py，检查 namespace/scope、manifest→claim→record→snapshot→session→report、hash、未来时点和跨公司引用；完整夹具输出 GOVERNANCE_CONSISTENCY_OK，损坏任一引用时非零退出。
- [ ] 8.9 在启用分析投影时实现 SQLite→DuckDB/Parquet 的只读重建和 ID/hash/time-boundary 一致性校验；运行 python -m pytest tests/governance/test_analytical_projection.py -q，未启用投影时核心治理查询仍须通过。

## 9. API、CLI、feature flag 与旧系统兼容

- [ ] 9.1 创建 GovernanceRuntime 和独立 FastAPI APIRouter，只在中央 api.py 注入 runtime 与 include router，并由 governance_management_v1 feature flag 控制；运行 python -m pytest tests/governance/test_api.py -q -k "feature_flag or runtime_injection or no_default_construction"。
- [ ] 9.2 实现只读 GET governance-view、显式 POST governance-snapshots 和固定 ID GET；运行 python -m pytest tests/governance/test_api.py -q -k "view or create_snapshot or get_snapshot or snapshot_not_found"，view 不得联网、抽取、重建或写数据。
- [ ] 9.3 实现 roles、ownership、control-graph、pledges、events 及其余类型化记录查询；运行 python -m pytest tests/governance/test_api.py -q -k "roles or ownership or control_graph or pledges or events or typed_records"。
- [ ] 9.4 实现 coverage、gaps、conflicts 和 candidates 旁路，canonical 响应不得混入候选或冲突精确值；运行 python -m pytest tests/governance/test_api.py -q -k "coverage or gaps or conflicts or candidates or canonical_isolation"。
- [ ] 9.5 实现 record lineage 和受控 evidence excerpt；运行 python -m pytest tests/governance/test_lineage_api.py -q，验证 span-only、LLM policy、known_at、无任意路径/URL及无绝对私有路径。
- [ ] 9.6 实现 validation 422、not-found 404、immutable conflict 409、storage busy 503 和 integrity 500 的稳定错误映射；运行 python -m pytest tests/governance/test_api.py -q -k "error_mapping or 422 or 404 or 409 or 503 or integrity"。
- [ ] 9.7 实现 governance report、session、trace 和 md/html/xlsx/pdf export API；运行 python -m pytest tests/governance/test_report_api.py -q，所有格式须固定同一 report/snapshot/citation 集合。
- [ ] 9.8 增加 analysis.cli governance snapshot|show|report|trace|validate|export，联网采集仍由共享 acquire 命令负责；运行 python -m pytest tests/governance/test_cli.py -q，治理写命令要求显式 DB/data-root 且 JSON 输出稳定。
- [ ] 9.9 保持旧 EventRecord、事件/lineage API、ReportVersion、同步和既有导出不变；运行 python -m pytest tests/governance/test_report_compatibility.py tests/test_event_state.py tests/test_reporting.py tests/test_api.py tests/test_exports.py tests/test_storage.py -q。
- [ ] 9.10 验证 feature flag 关闭时中央 API/CLI 和现有前端仍只看到旧行为，且已冻结治理数据不被删除；运行 python -m pytest tests/governance/test_feature_flag.py -q 和 npm --prefix frontend run build。

## 10. 自动化确定性门（独立门 1）

- [x] 10.1 建立 2022-12-31 完整名册、2023-04-20 发布、2023-06-01 任命/代理、2023-08-01 模糊辞任、2024-02-01 追溯更正的合成时间轴，并冻结更正前 strict、更正后 strict、同 state_at reconstructed、无锚点 incomplete 四个黄金结果；运行 python -m pytest tests/governance/test_bitemporal_reconstruction.py -q -k "A01 or A02 or A03 or A04 or A05 or A06 or A07 or A08 or A09 or A10 or A11 or A12"。
- [x] 10.2 建立来源角色、同源镜像、确定性准入和 LLM 候选隔离夹具；运行 python -m pytest tests/governance/test_extraction.py tests/governance/test_admission.py -q -k "A13 or A14 or A15 or A16"。
- [x] 10.3 建立 Codex 缺口、委派、未来污染、预算、实际读取和自主报告夹具；运行 python -m pytest tests/governance/test_codex_tools.py tests/governance/test_research_gate.py tests/governance/test_autonomous_report.py -q -k "A17 or A18 or A19 or A20 or A21 or A22 or A23 or A24"。
- [ ] 10.4 建立确定性、不可变性和迁移恢复夹具；运行 python -m pytest tests/governance/test_snapshot_service.py tests/governance/test_migration.py -q -k "A25 or deterministic or immutable or recovery_point"，旧 payload/report hash 必须不变。
- [x] 10.5 运行治理门 python -m pytest tests/governance -q、全量门 python -m pytest、方法门 python scripts/validate_method_library.py 和既有黄金结构门 python scripts/validate_golden_samples.py；分别记录真实结果，既有黄金 pending 不得冒充治理人工验收。
- [x] 10.6 扩展 .gitignore 覆盖 var/runs、var/pilots、原始证据、数据库、quarantine 和生成报告，并将 registry validator、治理离线测试、全量回归及 npm --prefix frontend run build 接入 CI；运行 git check-ignore -v var/runs/test/trace/01-acquisition-coverage.md var/pilots/governance-management-v1/analysis.db，两个路径均须命中。
- [x] 10.7 运行 openspec validate governance-management-data-foundation-v1 --type change --strict --no-interactive、git diff --check 和敏感信息扫描；OpenSpec valid 只证明规格结构，不替代 pytest、在线或人工门。

## 11. 真实联网样本门（独立门 2）

- [ ] 11.1 在任何联网前只读复核 CNINFO、SSE、SZSE、证监会/证监局和交易所监管来源的域名、路径、逐跳重定向、许可、robots、保留、时间精度、速率及 LLM policy，生成新 source registry 版本；不确定来源保持 disabled/pending，registry validator 通过后才能继续。
- [ ] 11.2 在隔离 namespace 执行 600519 governance_management baseline，记录上交所/巨潮的 run、coverage、snapshot、manifest、镜像组、缺口和限制；命令使用显式 --db var/pilots/governance-management-v1/analysis.db 与 --data-root var/pilots/governance-management-v1/data，默认工作库和 raw 根 hash 必须不变。
- [ ] 11.3 对 300750 执行同一隔离 baseline 并验证深交所/巨潮路径；不得用 600519 结果声称深市通过。
- [ ] 11.4 只读预检后固定一个复杂任免公告、一个监管决定和一条质押部分解除链，分别执行 acquire、governance snapshot 和 lineage；样本 ID 必须来自实际预检，不能沿用 planning 猜测。
- [ ] 11.5 验证 AKShare 只能完成“线索到正式原文”，并用真实 CodexExecRunner 执行一个 strict 历史任务，故意让子任务发现 known_at 之后材料；父 session 必须只收到 quarantine 数量/reason，不得收到标题、摘要、URL 语义、数值或结论。
- [ ] 11.6 将在线门按 passed、pending 或 failed 及检查日期、registry/run/snapshot/manifest/session ID、URL hash、时点精度和限制追加到 阶段日志.md；网络/许可阻塞可记 pending，绕过限制、未来泄漏或证据身份错误必须记 failed，有限样本不得外推全 A 股或全历史。

## 12. 人工黄金门与最终完成边界（独立门 3）

- [x] 12.1 新增只含元数据和签署状态的治理黄金 manifest 及 scripts/validate_governance_golden.py；未签署时普通命令输出 pending_manual_validation，--strict 非零退出，原始公告不得进入 Git。
- [ ] 12.2 人工对照正式原文核验字段级 evidence span，并逐项对照机器 JSON 检查八份 trace；记录复核人、时间、对象 ID/hash 和 pass/fail，不参与普通报告运行。
- [ ] 12.3 人工核验多人、多职务、代理、同名、跨公司决定、股权控制链、质押部分解除和数字不足样本；任何未核对项保持 pending。
- [ ] 12.4 人工比较同一历史 state_at 的 strict 与 reconstructed 报告，确认引用差异、future_knowledge_used 以及正式事实、contextual evidence、Codex judgment 三层表达；不规定 Codex 必须给出何种结论。
- [ ] 12.5 在全部签署后运行 python scripts/validate_governance_golden.py --strict，输出 GOVERNANCE_GOLDEN_OK；随后在 阶段日志.md 分别记录自动化、真实联网、真实 Codex launcher 和人工黄金状态。
- [ ] 12.6 通过另行批准的变更同步 计划.md 与现有运行时“用户确认”假设；在该权威同步、shared kernel 集成、三个验收门和真实 Codex launcher 状态均满足前，不得宣称整个 v1 Definition of Done 完成。
