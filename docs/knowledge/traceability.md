# 需求、测试与内容审阅

基线：`eight-step-knowledge-base-v1`，K01—K08，共 36 个场景；现有问题集 `1.0.0` 的 54 题。用户已确认跨行业方法、可靠公开来源、供分析 Agent 读取、不生成公司判断。程序断言、来源/案例审阅、外部 Agent 应用及人工抽查分别记账。

下表列验证方式，不代表所有场景已最终验收。程序测试集中在 `tests/knowledge/regression/`；内容样本使用明确标注的合成事实，预期值在内容正文之前建立。真实试用和最终状态见 `acceptance/`。

| 场景 | 程序或内容验证 |
|---|---|
| K01-S01 多方法读取 | `test_core_service.py::test_shared_methods_deduplicate_and_dependencies_order`；真实三题读取 |
| K01-S02 未知/待补区分 | `test_known_unknown_pending_and_no_default` |
| K01-S03 行业与通用覆盖 | `test_core_boundaries.py::test_partial_question_keeps_useful_method_without_claiming_full_coverage`；银行内容案例 |
| K01-S04 固定分母/有效映射 | `test_candidates_require_explicit_selection_and_coverage_denominator`、`test_invalid_mapping_or_dependency_blocks_candidate` |
| K01-S05 分层版本一致 | `test_version_snapshot_keeps_body_mapping_source_and_metric_definition` |
| K01-S06 长度不足 | `test_short_response_cannot_silently_drop_limits` |
| K02-S01 定价权支持关系 | `test_content_catalog.py` 的量价配对；真实 Agent 应用与内容理由 |
| K02-S02 亏损与异常分母 | ROIC零/负资本边界；后续估值方法内容案例及审阅 |
| K02-S03 缺失/不一致 | ROIC缺期初、WC缺组成、定价缺成交证据配对 |
| K02-S04 相近指标 | `test_metric_semantics_need_evidence_and_text_evidence_remains_text` |
| K02-S05 原文/待映射 | `test_exact_metric_snapshot_matches_and_unknown_metric_stays_pending` |
| K03-S01 来源不支持 | 首页定位结构拒绝；真实 Agent 错换来源样本；逐规则来源审阅 |
| K03-S02 同源/性质 | `test_same_source_republication_is_not_independent_support`；来源元数据 |
| K03-S03 暂不可获取 | 未核实来源发布拒绝；实际403记录与可用原文核查记录 |
| K04-S01 缺上下文 | `test_context_unknown_and_bank_are_separate_from_maturity` |
| K04-S02 银行边界 | 同上；真实银行读取及错误方法样本 |
| K04-S03 行业通用步骤 | ES07各题正文、案例与内容审阅；八步Agent样例 |
| K05-S01 骨架排除 | `test_nonpublished_never_counts_but_explicit_preview_keeps_status`；正文骨架检测 |
| K05-S02 草稿不发布 | 同上；候选必须显式选择 |
| K05-S03 证据发布 | `test_publication_rejects_missing_or_failed_evidence`；`test_release_gate.py` |
| K06-S01 ROIC口径分歧 | `test_roic_cases_keep_beginning_and_average_capital_separate`；12%/10%真实应用 |
| K06-S02 未解决分歧 | `test_declared_unresolved_conflict_blocks_publication_even_with_new_review` |
| K07-S01 历史正文/映射 | `test_version_snapshot_keeps_body_mapping_source_and_metric_definition` |
| K07-S02 未知/撤回版本 | 未知版本命令测试；更正记录与历史提示测试 |
| K07-S03 旧消费兼容 | `test_legacy_registry_remains_usable`、`tests/test_method_registry.py`、既有报告/存储回归 |
| K07-S04 影响传播 | `test_source_change_propagates_to_cases_questions_and_historical_notice` |
| K07-S05 审阅身份 | `test_body_source_case_and_metric_changes_invalidate_review`；父方法依赖身份测试 |
| K07-S06 历史输入定义 | `test_missing_historical_metric_does_not_substitute_latest` |
| K07-S07 当前覆盖下降 | `test_full54_drops_to53_and_unaffected_identity_survives`；有效替代路径测试 |
| K08-S01 测试先行 | 分离的测试/实现Git提交、`core-tdd-evidence.md`、`acceptance/release-gate-tdd.md` |
| K08-S02 结构对内容错 | 真实Agent对反转规则、错换来源、删除反例的独立审阅 |
| K08-S03 首版完成 | `tests/knowledge/release/test_full_release.py`；人工抽查独立记录 |
| K08-S04 回归/发布分离 | `test_release_command.py`；完整门实际失败而不skip/xfail |
| K08-S05 先小样例 | 冻结`pilot-v1`、三题真实读取记录和反馈修正提交，后续内容分批提交 |
| K08-S06 错误样本识别 | 三种有意错误内容变体的判定理由 |
| K08-S07 配对边界 | 充分证据支持有限判断；删关键证据后明确缩小判断的真实Agent结果 |

## 执行分工

`codex/knowledge-core` 与 `codex/knowledge-content` 由两个明确使用 `gpt-6-astra` / `ultra` 的 Agent 分别实现。主 Agent 在 `codex/knowledge-verification` 编写发布门，集成到 `codex/knowledge-base-v1`，并核查结果；另一个已有 Agent 独立应用首批指导。独立 Agent 与主 Agent 均不替代用户的人工内容抽查。

内容判定必须说明：可识别因素、证据足够支持的有限结论、禁止推断、适用边界及来源理由。不能以关键词匹配或全部拒答代替内容效果。
