# 茅台研究链术语核查

核查日期：2026-10-08。目的：用已有研究链检查六域的对象归属、输入输出及术语引用。

## 核查材料

本次读取当前工作区已有冻结包 `var/research-snapshots/600519/2026-10-02/lite-pack-137846d22f7c5c65b2776fcb/`，核对 manifest、核心包和逐题资料覆盖。报告端依据仓库中的[2026-10-03 成品验收说明](../acquisition/moutai-golden-report-acceptance.md)，获取与采用过程参照[输入恢复记录](../acquisition/moutai-input-restoration-20261002.md)。这些记录分别对应自身的资料和报告版本。

## 对象归属

| 链路位置 | 所属域与定义 | 核查依据 |
|---|---|---|
| 公司、截止日和问题范围 | 研究：[研究任务](research/GLOSSARY.md#研究任务)、[研究范围](research/GLOSSARY.md#研究范围) | 工作区任务状态与范围注册 |
| 获取、归档和恢复 | 采集：[采集运行](acquisition/GLOSSARY.md#采集运行)、[来源快照](acquisition/GLOSSARY.md#来源快照)、[完整空查询](acquisition/GLOSSARY.md#完整空查询) | 输入恢复记录；冻结包的 source_inputs 描述符 |
| 值、口径和证据链 | 事实：[数值事实](facts/GLOSSARY.md#数值事实)、[事实血缘](facts/GLOSSARY.md#事实血缘) | 核心包中 `F198` 对应收入事实，保留事实 ID、来源、累计期间与 CNY 单位 |
| 固定研究输入 | 研究：[研究资料包](research/GLOSSARY.md#研究资料包)、[研究快照](research/GLOSSARY.md#研究快照) | manifest、核心包与目录的 `pack_id` 一致；输出文件保存哈希 |
| 问题资料与分析状态 | 研究：[逐题资料覆盖](research/GLOSSARY.md#逐题资料覆盖)、[研究判断与完成](research/GLOSSARY.md#研究判断与完成) | 逐题文件独立保存材料 `state` 与 `analysis_status` |
| 假设与估值执行 | 研究：[研究假设](research/GLOSSARY.md#研究假设)；计算：[正式计算](calculation/GLOSSARY.md#正式计算)、[情景](calculation/GLOSSARY.md#情景) | 研究计算接口接受显式假设并执行公式；报告验收文档另记录 DCF 延期项 |
| 版本、导出与成品验收 | 报告：[报告版本](reporting/GLOSSARY.md#报告版本)、[报告状态](reporting/GLOSSARY.md#报告状态)、[成品审阅与验收](reporting/GLOSSARY.md#成品审阅与验收) | 成品验收说明保留通过记录、草稿状态和未确认评级字段 |
| 按需方法读取 | 知识：[知识方法](knowledge/GLOSSARY.md#知识方法)、[知识覆盖](knowledge/GLOSSARY.md#知识覆盖)、[默认发布](knowledge/GLOSSARY.md#默认发布) | [知识目录合同](../knowledge/catalog-contract.md)；公司事实供给由研究链处理 |

## 冻结包检查结果

- manifest 声明的 9 个输出文件哈希一致，81 个 source_inputs 文件描述符指向的文件可读取；现有 reporting bridge 的 `_validate_pack` 对冻结包及输入描述符的只读校验通过。
- 核心包包含 325 个指标条目，均为 ready；`F198` 对应 `operating_income`，期间为 2023 年全年累计，单位为 CNY，保留来源引用。
- 逐题文件覆盖 54 个问题、1,897 个期间输入。材料状态为 372 ready、198 source_text_available、1,216 pending、27 not_disclosed、84 not_applicable。
- 同一文件的分析状态为 139 completed、1,758 not_started；该计数按期间输入行统计。由此可直接检查“指标条目就绪”“问题资料状态”和“分析状态”的不同范围。

报告端此次核对仓库中的验收说明；本工作区没有其引用的原始 v5 验收 JSON。本次没有重新采集、计算、生成或审阅报告成品。术语核查与文档接入的检查范围单独保存，历史业务验收记录保留原版本。
