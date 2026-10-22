# 采集域术语

负责来源登记、请求计划、获取、归档和恢复，交付原始快照、派生产物及获取状态。事实准入见[事实域](../facts/GLOSSARY.md)，资料是否足够回答问题见[研究域](../research/GLOSSARY.md)。

## 来源定义

`SourceDefinition` 固定一个来源版本的身份、请求协议、访问策略和查询能力。请求与历史回放保留所使用的定义版本。依据：[采集模型](../../../src/analysis/acquisition/models.py)、[来源注册表](../../../src/analysis/acquisition/registry.py)。

## 采集计划

`AcquisitionPlan` 将公司、截止日、问题及来源范围展开为查询与覆盖项；计划阶段可只冻结范围，执行阶段才获取材料。结构化字段计划的实现另见 [structured planner](../../../src/analysis/structured/planner.py)。依据：[采集计划模型](../../../src/analysis/acquisition/models.py)。

## 采集运行

`AcquisitionRun` 记录一次计划的执行上下文、来源版本、模式和父运行。`baseline`、`incremental`、`reconcile` 的具体范围遵循各运行时合同；运行是否终结及是否有材料缺口分别记录。依据：[采集模型](../../../src/analysis/acquisition/models.py)、[结构化运行说明](../../acquisition/structured-data-runtime-v1.md)。

## 请求尝试

`AcquisitionAttempt` 记录一次 discovery 或 fetch 的执行及结果。同一运行可以有多次尝试，超时、网络失败、受限等按实际原因保存。依据：[采集模型](../../../src/analysis/acquisition/models.py)、[采集运行时](../../../src/analysis/acquisition/runtime.py)。

## 来源快照

`RawResourceSnapshot` 是已归档原始响应或材料的版本记录，绑定来源版本、内容哈希、归档位置及可得时间。研究采用的资料集合见[研究快照](../research/GLOSSARY.md#研究快照)。依据：[快照模型](../../../src/analysis/acquisition/models.py)、[快照存储](../../../src/analysis/acquisition/snapshots.py)。

## 派生产物

`DerivedArtifact` 是从原始材料解析或转换得到的结果，如带页码的正文和表格。它保留原始快照、解析器与版本引用；数值进入计算还需满足[事实准入](../facts/GLOSSARY.md#结构化准入)。依据：[采集模型](../../../src/analysis/acquisition/models.py)、[MinerU 解析说明](../../acquisition/mineru-precision-parser.md)。

## 证据清单

`EvidenceSnapshotManifest` 固定截至某日纳入与排除的来源快照、派生产物和覆盖摘要，并保存清单哈希。其门禁针对该清单范围；研究成果的状态见[研究判断与完成](../research/GLOSSARY.md#研究判断与完成)。依据：[清单模型](../../../src/analysis/acquisition/models.py)、[清单构建](../../../src/analysis/acquisition/manifests.py)。

## 采集覆盖

`CoverageEntry` 与 `CoverageResolution` 表达计划查询的静态处置、执行结果、相关凭证及材料缺口。覆盖描述的是登记的来源、查询和时间范围；逐题输入是否就绪另查[逐题资料覆盖](../research/GLOSSARY.md#逐题资料覆盖)。依据：[覆盖模型](../../../src/analysis/acquisition/models.py)、[采集仓储](../../../src/analysis/acquisition/repository.py)。

## 安全检查点

`SourceCheckpoint` 保存来源及分区的安全推进位置、固定 overlap 范围与未解决 barrier。恢复和增量从已证明的位置继续，安全水位受必需分区及未解决缺口约束。依据：[检查点模型](../../../src/analysis/acquisition/models.py)、[检查点逻辑](../../../src/analysis/acquisition/checkpoints.py)。

## 完整空查询

`no_data` 表示本次 discovery 查询成功完成结构校验、总数闭合与分页终止证明，结果为空。它只说明该来源和查询范围的返回；“未披露”的证据要求见[缺失与不适用](../facts/GLOSSARY.md#缺失与不适用)。失败、受限、解析异常按各自结果记录。依据：[DiscoveryProof](../../../src/analysis/acquisition/models.py)、[当前采集说明](../../../README.md)。
