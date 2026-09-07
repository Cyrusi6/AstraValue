# 业务证据章节定位与事实表

入口为 `python -m analysis.business_evidence.cli`，读取已有、绑定正确的采集库；所有输出显式写入独立路径。源码工作树若没有安装为 editable package，先设 `PYTHONPATH=src`。该入口不联网、不上传 MinerU，也不写采集库。

`config/business_evidence/topic_routes.v1.json` 维护十主题章节对应表与口径检查。`route` 输出完整候选页、邻页、章节字符位置、页文本哈希和 snapshot/artifact 引用，文本只保存一次并共享引用。无命中为 `no_route_requires_fulltext_search`；有命中也只是候选。跨页长表必要时继续扩展阅读范围，不能以候选页代替全文验收。

|主题|优先章节|
|---|---|
|Q01 起源与盈利模式|招股公司沿革、公司基本情况、年报主营业务与经营模式|
|Q02 产品地区经济|收入成本分析、分产品/地区/渠道表、更正公告|
|Q03 客户供应商渠道|主要客户、供应商、经销商及渠道|
|Q04 价格与单位经济|产品价格调整、交易定价、渠道收入销量|
|Q05 产能与产销存|行业经营信息、产能产销存表及表下注释|
|Q06 资本开支|在建工程、投资项目、现金流量表及建设公告|
|Q07 研发投入产出|研发投入、人员、支出附注、科技成果|
|Q08 产业链|采购模式、供应商管理、关联交易及销售渠道|
|Q09 战略变化|管理层讨论、经营计划、募投变更及经营事件|
|Q10 竞争力证据|核心竞争力、创新、质量、知识产权；保留公司自述状态|

使用 `config/business_evidence/review.schema.json` 准备复核 JSON。一个 record 只记录一个原子值，`value_type=decimal` 使用无千分逗号的字符串，单位和尺度显式保存；不自动换算。`text` 值摘录原文，解释写入复核说明文件。`subject_role` 区分本公司、子公司、拟投资或其他主体。`event_stage` 区分计划、批准、实施中、完成、一般披露、公司声明和会计重分类。

去重身份为公司、主体/主体关系、期间、metric、dimensions、unit、basis、event_stage 和 value_type。身份加 value/revision 构成事实版本 ID。question_ids 不进入身份，同一事实可支持多个问题。请统一指标代码和口径标签，勿在 basis/dimensions 中混入评价性说明；不同措辞的同义指标需要复核后统一，不由程序猜测语义等价。JSON 引用保留来源 URL、标题、PDF 一基页码（HTML 为 null）、章节、原文、manifest/hash、snapshot/hash 和 artifact/hash。重复引用不当成多个独立证明。

同身份不同值保留 conflict，只有 `corrections` 中明确连接 previous_record_id/replacement_record_id 且有真实更正引文才更新当前视图。replacement 的某条 citation 必须与 correction.citation 完全一致；不要把模型复核状态修正放入 corrections。后续恢复到曾出现的值时使用新的 revision，避免把新版本合并回旧版本。旧行不更新或删除，禁止版本环及相互矛盾的分叉。

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
python -m analysis.business_evidence.cli route --db <已有采集库> --data-root <绑定目录> --manifest <manifest-id> --output <新目录/routes.json>
python -m analysis.business_evidence.cli import --db <已有采集库> --data-root <绑定目录> --manifest <manifest-id> --review <复核JSON> --facts-db <独立目录/facts.db>
python -m analysis.business_evidence.cli query --db <已有采集库> --data-root <绑定目录> --manifest <manifest-id> --facts-db <独立目录/facts.db> --company 600519 --question Q07 --as-of 2026-09-07T12:00:00Z --output <新目录/q07.json>
```

route 可用重复 `--snapshot` 限定样本；manifest 内有多个 text artifact 时以 `--text-selection` 指定 `{snapshot_id: artifact_id}` JSON 映射。query 生成 JSON 与 Markdown，输出路径不可覆盖不同内容；重跑时使用同一明确 as_of 可验证幂等。`current` 仅代表在该时点没有已知更正或冲突，仍是 AI 复核、非人工黄金，不证明定价权、护城河或投资价值。对样本之外主题的覆盖不做推断。

本轮新消费冻结 `business_model_no_audit_english_annual_v1`，不再把独立审计 PDF 和英文年报选入新事实输入；历史 manifest 与旧引用不回写。英文 ESG、中报、中文年报和更正公告仍按原范围保留。
