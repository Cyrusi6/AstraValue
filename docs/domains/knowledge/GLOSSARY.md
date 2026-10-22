# 知识域术语

负责文献、方法、适用条件、内容身份和发布。公司资料与判断由[研究域](../research/GLOSSARY.md)处理；方法输入需要的事实口径见[事实域](../facts/GLOSSARY.md)，实际公式执行见[计算域](../calculation/GLOSSARY.md)。

## 文献来源

知识目录 `sources` 保存作者、题名、版本、原始链接、精确定位、核实与访问状态及正文哈希，支持方法规则的出处引用。公司的事实来源由[来源记录](../facts/GLOSSARY.md#来源记录)保存。依据：[知识目录合同](../../knowledge/catalog-contract.md)。

## 知识方法

目录 `methods` 的条目按 `method_id` 和版本保存操作步骤、必需输入、证据要求、规则、反例、限制、行业缺口与依赖。它向研究者提供分析方法及适用边界。依据：[目录合同](../../knowledge/catalog-contract.md)、[KnowledgeService](../../../src/analysis/knowledge/service.py)。

## 知识输入绑定

`required_inputs` 描述方法需要的指标或原文，并核对定义身份、版本、期间、单位和范围。绑定状态保留 matched、pending 或 mismatch；输入类型和完整字段以目录合同为准。公司实际材料是否取得另看[逐题资料覆盖](../research/GLOSSARY.md#逐题资料覆盖)。依据：[输入绑定合同](../../knowledge/catalog-contract.md)、[KnowledgeService._binding](../../../src/analysis/knowledge/service.py)。

## 适用条件

`applicability` 和 `context_status` 描述方法适用的行业、场景及所需上下文。内容可用状态与本次上下文适用性分别评价，必需上下文未明确时保留 needs_context。依据：[适用性合同](../../knowledge/catalog-contract.md)、[适用性检查](../../../src/analysis/knowledge/service.py)。

## 内容成熟度

`content_status` 使用 skeleton、draft、reviewed、published 表达方法正文的成熟程度。skeleton 是结构骨架，实质方法内容和审阅证据仍需补齐；已发布内容的当前有效性继续受身份、依赖和更正记录约束。依据：[内容状态合同](../../knowledge/catalog-contract.md)、[知识库说明](../../knowledge/README.md)。

## 内容身份

`method_identity` 固定方法字段、正文、引用来源、案例和实际指标定义的内容身份，用于绑定审阅及识别变更影响。正文或引用变化后检查身份，审阅数组独立保存。依据：[身份合同](../../knowledge/catalog-contract.md)、[身份与冻结实现](../../../src/analysis/knowledge/service.py)。

## 知识包

`bundle_id` 标识冻结的知识版本集合，保存方法、引用和正文以支持固定版本读取。`build_candidate` 生成候选；默认发布由独立入口处理。该知识包的身份与研究资料的[研究快照](../research/GLOSSARY.md#研究快照)分别管理。依据：[知识包合同](../../knowledge/catalog-contract.md)、[知识包实现](../../../src/analysis/knowledge/service.py)。

## 知识覆盖

`coverage` 按研究问题统计有效、已发布且依赖完整的方法路径，并保留待补问题与行业缺口。`question_complete` 评价题内必需方法是否完整，`executable` 还要求上下文适用及返回内容完整；公司材料供给另查[研究域](../research/GLOSSARY.md#逐题资料覆盖)。依据：[覆盖合同](../../knowledge/catalog-contract.md)、[KnowledgeService.coverage](../../../src/analysis/knowledge/service.py)。

## 默认发布

`publish_default` 按当前完整问题集和独立整体验收证据设置默认知识包。没有默认包时 `read` 返回 `no_default_release`；显式读取候选保留候选身份。发布条件与记录查现有合同和验收文件。依据：[发布合同](../../knowledge/catalog-contract.md)、[发布验收说明](../../knowledge/acceptance/README.md)、[发布实现](../../../src/analysis/knowledge/service.py)。

## 知识更正

`record_change` 保存来源、规则、输入或方法的更正理由及受影响方法、问题和案例。历史固定版本读取保留原文并附更正提示，当前有效覆盖按影响重新评价。依据：[更正合同](../../knowledge/catalog-contract.md)、[更正实现](../../../src/analysis/knowledge/service.py)。
