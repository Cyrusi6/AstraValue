# 知识目录与读取接口 v1

此合同供当前候选切片协作使用。知识入口不接受公司事实；示例事实只放在外部评测案例。

## 目录

`config/methods/knowledge/catalog.v1.json` 是 JSON 对象，包含 `schema_version`、`catalog_version`、`mapping_version`（均为版本字符串）以及以下数组。

- `sources`: `source_id, version, title, author, url, locator, acquired_at, verification_status, access_status, redistribution, content_sha256`。已取得正文且已核对定位时 `verification_status="verified"`；`publication_date` 未知须显式 `unknown`。同一内容只保留一个当前 `source_id`。
- `source_aliases`（可选）：历史 `alias_source_id/alias_version` 到当前 `canonical_source_id/canonical_version` 的映射，`status` 必须为 `historical_alias`，并声明 `scope`。别名只用于读取旧 bundle/manifest 和更正定位；新方法、案例和新 bundle 必须引用 canonical ID。
- `methods`: `method_id, version, legacy_method_id, title, question_ids, content_status, body_path, steps, required_inputs, evidence_requirements, rules, counterexamples, limitations, industry_gaps, case_ids, applicability, dependencies`。正文 `body_path` 相对项目根，快照会保存正文。步骤/反例/限制/行业缺口均为字符串数组。`dependencies` 是先行知识方法 ID 数组；`alternatives` 可选，为替代方法 ID 数组。`content_status` 为 `skeleton/draft/reviewed/published`。`question_ids` 不复制维护问题标题。
- `cases`: `case_id, method_ids, kind, expected_factors, supported_conclusions, forbidden_conclusions, reason, source_refs, evaluation_mode`。`kind` 为 `normal/counterexample/missing/boundary`，三个结论/因素字段为字符串数组。另可含 `facts` 合成事实、`pair_id`。
- `reviews`: `review_id, method_id, method_version, content_sha256, kind, outcome, reasons, unresolved_issues, reviewer`。`kind` 为 `source/case/agent/human`，`outcome` 为 `passed/failed`，理由和未解决事项均为字符串数组。Agent 审阅另记录 `model,prompt_version`。`source` 与 `case` 审阅必须存在且绑定内容身份，程序不能代替内容审阅。不得生成虚构的人工通过记录。

方法的 `required_inputs` 每项为 `{input_id,kind,concept,requirements,binding_status}`；`kind` 为 `metric/text`，`requirements` 可含 `period,unit,scope`。指标输入可另给 `metric_id,definition_version,definition_sha256,expected_definition`；只有实际定义身份、版本和必要口径均匹配才返回 `matched`，否则返回 `pending/mismatch`。原文输入须有 `locator_requirement`，返回 `text_evidence`，不强造指标 ID。

`evidence_requirements` 每项为 `{evidence_id,importance,description,supports,missing_effect}`；`importance` 为 `required/supplementary`。

`rules` 每项为 `{rule_id,statement,source_refs}`；`source_refs`（案例亦同）每项为 `{source_id,version,locator,support}`。重要规则不可仅给来源首页；定位和支持关系须由来源审阅核实。自行综合可以增加 `derivation,assumptions`。

`applicability` 为 `{include_industries,exclude_industries,requires_context,conditions}`。前两者是行业字符串数组，`requires_context` 如 `["industry"]`，`conditions` 是条件说明数组；银行建议同时列 `bank/banking/银行`。

## Python 接口

```python
from analysis.knowledge import KnowledgeService, method_identity

service = KnowledgeService.from_catalog(catalog_path, store_root)
check = service.validate_candidate()  # {valid,errors,warnings}
identity = service.method_identity(method_id)  # 用于内容审阅绑定
bundle = service.build_candidate(bundle_id="candidate-v1")
result = service.read("ES02.Q04", bundle_id=bundle["bundle_id"], context={"industry":"industrial"})
expanded = service.expand(result["methods"][0]["detail_ref"])
coverage = service.coverage(bundle["bundle_id"])
impact = service.record_change("source", "source-id", "更正理由", change_id="correction-1")
```

`read(question_id,bundle_id=None,context=None,detail=False,include_drafts=False,max_chars=None)` 默认只读取完整默认包。不存在默认包返回 `no_default_release`；指定候选包不自动发布。未知问题为 `unknown_question`、未知版本为 `unknown_version`、已知无可用方法为 `content_pending`、可用为 `available`。`detail=True` 返回冻结全文，短结果保留步骤、关键证据、反例、限制、缺口和展开引用。长度不够时返回 `complete=false`、`omitted_sections` 和 `continue_ref`，不删除限制后声称完整。

`status` 描述内容存在与成熟度；顶层 `context_status` 单独为 `applicable/needs_context/not_applicable`。`question_complete` 表示本题所有必需通用路径已具备有效内容，`executable` 只有本题完整且选择上下文明确适用时才为 true；它不保证公司事实已取得。篇幅不足的结果 `executable=false`。CLI 接受 `--industry`、`--business-type`、`--period-type`、`--model-purpose` 这四种选择条件，不接收公司财务事实。

`coverage(bundle_id=None)` 返回 `bundle_id,bundle_kind,total=54,mapped_count,available_count,historical_published_count,available_question_ids,pending_question_ids,industry_gaps,default_published`。通用可用按已发布、有效且依赖完整的方法路径计数；只计题一次。题内仍未覆盖的必需部分须由作者保留为待补方法，不得仅映射一个局部条目便宣称整题完成。

`record_change(kind,object_id,reason,change_id=None)` 支持 `source/rule/input/method`，输出受影响方法、问题和案例，并追加更正记录。固定历史读取保留原文并附提示，受影响路径不计当前可用覆盖；无关方法不变。`publish_default(bundle_id,release_evidence)` 需要完整 54 题和单独的整体验收证据。

方法身份覆盖方法字段、正文、所引用来源、案例和实际指标定义；审阅数组不进入自身身份。不得只改版本字符串、复用旧审阅授权变化后的正文。
