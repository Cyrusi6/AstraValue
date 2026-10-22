# 研究域术语

负责公司任务、问题范围、资料采用、补充阅读、假设和判断。材料来自[采集](../acquisition/GLOSSARY.md)、[事实](../facts/GLOSSARY.md)与按需[知识](../knowledge/GLOSSARY.md)，数值交[计算](../calculation/GLOSSARY.md)执行，成稿交[报告](../reporting/GLOSSARY.md)组装。

## 研究任务

`research_id` 标识工作区中的公司研究，关联截止日、采用的资料快照、任务状态与产物。恢复和后续操作使用同一工作区配置及状态根定位任务。依据：[ResearchWorkspace](../../../src/analysis/research/workspace.py)、[工作区运行说明](../../acquisition/research-workspace-runbook.md)。

## 研究范围

范围由公司、截止日、期间、主体或同行、问题与所需材料共同限定。ES01–ES08 和问题 ID 用于组织分析及资料需求；追加材料说明它服务于哪个问题。依据：[范围注册](../../../src/analysis/structured/scope.py)、[数据与文件范围](../../acquisition/eight-step-data-and-document-scope.md)。

## 研究资料包

`ResearchPack` 是研究消费边界的概念名，当前通过轻量包目录、manifest、core-pack、覆盖与证据文件实现，身份使用 `pack_id`。包提供紧凑事实和可继续读取的材料入口，并保留缺口、冲突及限制。依据：[轻量包实现](../../../src/analysis/structured/research_lite.py)、[运行说明](../../acquisition/eight-step-lite-runbook.md)。

## 研究快照

工作区 `snapshot_id` 绑定当前采用的 `pack_id`，用于固定研究输入以及章节、计算和图表的资料版本。补充材料可以形成不可变候选；需要采用候选的路径通过 `adopt_snapshot` 切换，旧产物的使用按当前合同重新检查。获取到的原始材料版本见[来源快照](../acquisition/GLOSSARY.md#来源快照)。依据：[工作区](../../../src/analysis/research/workspace.py)、[补采与采用](../../../src/analysis/research/supplements.py)、[材料继承](../../../src/analysis/research/snapshot_materials.py)。

## 材料请求

`request_materials` 按问题检查已有资料，再按已登记能力获取和处理缺失材料；持久任务由 `resume_task` 恢复。不同请求路径可能形成候选或登记当前快照的补充阅读资料，采用方式按各自合同执行。依据：[研究补采](../../../src/analysis/research/supplements.py)、[工具运行说明](../../acquisition/research-workspace-runbook.md)。

## 证据读取

`read_evidence` 等读取入口返回带来源、页码或文本位置、上下文及续读位置的材料。研究按需继续读取；原文中的数字进入正式计算前检查[事实准入](../facts/GLOSSARY.md#结构化准入)。依据：[证据读取](../../../src/analysis/structured/reading.py)、[研究工具](../../../src/analysis/research/tools.py)。

## 研究陈述

`ClaimRecord` 保存陈述文本及支持它的事实、来源与证据引用。`ClaimKind` 区分披露事实、代码推导、分析判断和情景假设；判断类陈述还记录未知项、置信度及失效条件。依据：[ClaimRecord 与 ClaimKind](../../../src/analysis/models.py)。

## 已核验来源结论

材料目录的 `reviewed_filing` 投影接入 `user_confirmed`（用户确认）和 `primary_source_verified`（一手来源核验）两种依据，同时检查可消费状态、原件哈希及研究截止日。公司说明会答复和平台行情保留各自来源性质、期间及规格。候选资料通过读取核验后，仍按[研究快照](#研究快照)的采用流程切换。依据：[来源结论目录](../../../src/analysis/research/reviewed_topics.py)。

## 研究假设

`AssumptionRecord` 及计算接口中的显式 assumptions 保存研究者提出的增长、利润率、倍数等参数、理由、情景与有效条件。假设的提出和依据由研究负责；参数结构、公式和运算检查见[计算域](../calculation/GLOSSARY.md)。依据：[假设模型](../../../src/analysis/models.py)、[计算参数合同](../../../src/analysis/research/calculation_schema.py)、[产品职责](../../../项目总计划.md)。

## 逐题资料覆盖

覆盖按研究问题、要求和期间评价输入是否就绪、原文可读、待处理或不适用，并保留原因和引用。底层 `CoverageState` 与轻量包逐题投影的状态按各自 schema 解释。该覆盖衡量材料供给；分析完成另查[研究判断与完成](#研究判断与完成)。依据：[覆盖模型](../../../src/analysis/structured/coverage.py)、[逐题投影](../../../src/analysis/structured/research_coverage.py)。

## 研究判断与完成

研究者解释证据、形成论点、提出假设、选择适用方法并给出评级。完成研究需有明确判断、必要核查以及对影响结论的未解决问题的处理；材料状态与报告版本状态各自记录。具体交付要求遵循统一研究和审阅提示。依据：[产品职责与验收](../../../项目总计划.md)、[研究主提示](../../../config/prompts/buy_side_research.md)、[审阅提示](../../../config/prompts/buy_side_review.md)。
