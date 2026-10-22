# 事实域术语

负责把证据整理为带定义、口径、时间、核验状态和血缘的事实；公司治理取证及历史重建也在本域维护。输入见[采集域](../acquisition/GLOSSARY.md)，假设与判断见[研究域](../research/GLOSSARY.md)，执行公式见[计算域](../calculation/GLOSSARY.md)。

## 来源记录

`SourceRecord` 是事实、陈述和审计引用的来源描述，保留上游、发布时间、获取时间、来源版本及原始快照标识。原始内容版本由[来源快照](../acquisition/GLOSSARY.md#来源快照)承载。依据：[SourceRecord](../../../src/analysis/models.py)。

## 数值事实

`FactRecord` 记录某公司、指标和期间的值、单位、币种、合并范围、来源及核验状态。消费时同时检查定义、期间和准入依据；`value=null` 表达缺值，零值有独立含义。依据：[FactRecord](../../../src/analysis/models.py)、[事实物化](../../../src/analysis/structured/materialization.py)。

## 维度事实

`DimensionalFactRecord` 保存产品、地区、客户或供应商等维度下的业务记录及证据定位。汇总和比较须检查维度、期间、范围和披露形式。依据：[维度模型](../../../src/analysis/models.py)、[业务证据合同](../../acquisition/business-evidence-facts.md)。

## 事件事实

`EventRecord` 保存事项、生命周期、时间、数值与原文定位，如股利预披露和实施分配。公告、预案、实施等阶段保留各自身份；历史使用检查当时的可得时间。依据：[EventRecord](../../../src/analysis/models.py)、[事件状态](../../../src/analysis/event_state.py)。

## 结构化准入

`StructuredFactAdmission` 记录取得方式、数据性质、质量、来源与字段版本，以及快照、行键、字段路径等凭证。`programmatic_eligible` 按当前谓词计算；供应商估计、预测、标签和来源文字保留各自性质。依据：[准入模型](../../../src/analysis/models.py)、[字段准入](../../../src/analysis/structured/records.py)。

## 期间与口径

年度、累计、单季、TTM、期末存量和行情日按字段语义区分，并结合合并范围、币种和定义消费。`reported_period` 只确认报告期标识，窗口未确认时保留状态。单季和 TTM 由满足期间条件的输入推导。依据：[期间模型与推导](../../../src/analysis/structured/records.py)、[字段注册表](../../../config/structured_data/fields.v1.json)。

## 单位与显示

单位说明存储数值的量纲与尺度，显示格式另负责“元／亿元”“比例／百分比”“倍”等表达。换算依据字段定义，并保留原始值与单位；显示改变不重定义存储数值。依据：[normalize_numeric](../../../src/analysis/structured/records.py)、[字段解释](../../../src/analysis/structured/interpretation.py)、[数据字典](../../methodology/data_dictionary.md)。

## 核验状态

`VerificationStatus` 表达当前记录的来源或核验性质，包含供应商直采、程序计算、待核验等。`StructuredFactAdmission` 另保存取得方式、性质和质量，二者共同参与消费检查；历史枚举按原语义保留。依据：[事实模型](../../../src/analysis/models.py)、[证据规则](../../methodology/evidence_policy.md)。

## 事实血缘

血缘连接事实到真实来源、原始快照、行与字段；派生事实还连接输入事实及公式版本。报告和计算沿这些标识追溯，引用时保留完整链路。依据：[FactRecord.validate_lineage](../../../src/analysis/models.py)、[记录标识与定位](../../../src/analysis/structured/records.py)。

## 时间与研究截止

`period_start/period_end` 描述业务期间，披露或生效时间描述事项发生，`available_at` 及其依据说明历史上何时可用于研究，`retrieved_at` 记录本地获取时间。研究截止日筛选可得资料；实际行情日独立保存。本地采集时间字段 `__retrieved_at` 只作 provenance。依据：[记录选择](../../../src/analysis/structured/records.py)、[采集快照模型](../../../src/analysis/acquisition/models.py)、[时间字段修复记录](../../acquisition/moutai-structured-api-acceptance-20261002.md)。

## 缺失与不适用

来源空查询、获取失败、字段未映射、定义未知、阅读待处理、过期和公式缺输入按具体原因记录。“未披露”需明确材料依据，“不适用”需规则或材料依据。原文表格明确留白可保存 `disclosed_blank` 与定位，值仍为 null。依据：[输入就绪状态](../../../src/analysis/structured/coverage.py)、[研究可用性核查](../../../src/analysis/research/availability.py)、[证据规则](../../methodology/evidence_policy.md)。

## 公司治理事实

本域的专项概念包括主体解析、任期、持股、控制关系、事件重建、双时点快照与脱敏。治理模型按自身合同解释，治理评价由[研究判断](../research/GLOSSARY.md#研究判断与完成)承载；治理取证通过共享采集清单绑定研究工作区。依据：[治理模型](../../../src/analysis/governance/models.py)、[重建逻辑](../../../src/analysis/governance/reconstruction.py)、[治理接入合同](../../acquisition/governance-research-workspace-binding.md)。
