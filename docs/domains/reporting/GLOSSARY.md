# 报告域术语

负责章节保存、引用解析、报告版本、成品组装与导出。研究内容及假设见[研究域](../research/GLOSSARY.md)，引用的数值与证据见[事实域](../facts/GLOSSARY.md)，估值和探索结果见[计算域](../calculation/GLOSSARY.md)。

## 研究草稿

工作区草稿保存章节 Markdown、判断、引用和结论信息，并绑定研究及快照 ID。修改研究成果先更新对应草稿，再构建报告版本。依据：[Drafts](../../../src/analysis/research/drafts.py)、[写作接口](../../../src/analysis/research/authoring.py)。

## 八步章节

`ReportSection` 对应按 1–8 排序的分析正文。结论卡和审计附录各自呈现；章节内容由研究者形成，模板负责显示与组装。依据：[章节模型及顺序校验](../../../src/analysis/models.py)、[研究主提示](../../../config/prompts/buy_side_research.md)。

## 结论卡

`ConclusionCard` 汇总评级、当前价格及其日期、价值区间、论点、风险和跟踪事项。`rating_confirmed` 等字段按本版实际记录解释，研究评级的形成见[研究判断与完成](../research/GLOSSARY.md#研究判断与完成)。依据：[ConclusionCard](../../../src/analysis/models.py)、[草稿结论保存](../../../src/analysis/research/drafts.py)。

## 报告桥接

reporting bridge 将冻结研究资料与分析请求接入统一报告服务，校验包文件、输入身份、选中事实和来源血缘。桥接位置同时涉及事实与报告，改变输入消费时补读[事实域](../facts/GLOSSARY.md)。依据：[reporting_bridge](../../../src/analysis/structured/reporting_bridge.py)、[报告语义](../../../src/analysis/structured/report_semantics.py)。

## 报告版本

`ReportVersion` 绑定报告 ID、版本与父版本、截止日、数据快照、方法包、八步章节、结论及审计数据。新研究结果形成相应版本，历史报告保留原输入和状态。依据：[ReportVersion](../../../src/analysis/models.py)、[报告服务](../../../src/analysis/service.py)。

## 正文引用

事实、证据、计算和图表通过短引用接入正文，组装时解析为实际数值、注号或图表。未知引用阻止构建；引用结果的期间、单位和快照遵循其所属域定义。引用语法见现有工具合同。依据：[报告组装](../../../src/analysis/research/reports.py)、[工具运行说明](../../acquisition/research-workspace-runbook.md)。

## 审计附录

`AuditAppendix` 收纳来源、方法包、假设、缺失与冲突、版本变化、核验记录和计算运行，供成品追溯。正文保持研究解释，完整追溯信息按报告结构进入附录。依据：[AuditAppendix](../../../src/analysis/models.py)、[报告呈现](../../../src/analysis/reporting.py)。

## 格式导出

Markdown、HTML、JSON、Excel 和 PDF 从同版报告导出，保持核心内容、数字和引用一致，各格式使用适合阅读的版式。检查项目包括实际页面、工作表及图表，具体覆盖写入验收记录。依据：[导出实现](../../../src/analysis/exports.py)、[报告构建](../../../src/analysis/research/reports.py)。

## 报告状态

`ReportStatus` 保存系统报告的“草稿”或“已复核”状态。成品验收记录、研究资料覆盖和评级确认分别保存；例如茅台 v5 的授权代人工验收通过，同时保留 `status=草稿` 和 `rating_confirmed=false`。依据：[状态模型](../../../src/analysis/models.py)、[茅台成品验收记录说明](../../acquisition/moutai-golden-report-acceptance.md)。

## 成品审阅与验收

审阅包括研究逻辑、数字与引用检查及实际渲染成品的阅读，记录对应版本、检查主体、结果和延期项。模型审阅、用户人读以及经用户授权的代人工验收按各自证据保存。依据：[审阅提示](../../../config/prompts/buy_side_review.md)、[工作区验收约定](../../acquisition/research-workspace-runbook.md)、[茅台验收](../../acquisition/moutai-golden-report-acceptance.md)。
