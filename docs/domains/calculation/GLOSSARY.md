# 计算域术语

负责指标、估值、情景和探索计算的执行与验证。输入事实由[事实域](../facts/GLOSSARY.md)定义，假设依据由[研究域](../research/GLOSSARY.md#研究假设)维护，结果交[报告域](../reporting/GLOSSARY.md)引用。

## 指标定义

`metric_id` 标识指标，定义包含报表类别、单位、期间及适用口径；派生指标绑定公式和方法版本。具体指标与字段路由分别查机器配置。依据：[数据字典](../../methodology/data_dictionary.md)、[指标配置](../../../config/methods/metrics.json)、[字段注册表](../../../config/structured_data/fields.v1.json)。

## 正式计算

`calculate` 使用当前研究快照的事实引用、明确方法和显式假设执行已支持的运算，返回可引用的 calculation 产物。输入引用、期间、单位和必要数值条件由对应方法校验；缺输入保留缺口或返回具体错误。依据：[Calculations](../../../src/analysis/research/calculations.py)、[计算参数 schema](../../../src/analysis/research/calculation_schema.py)。

## 方法版本

`method_ref`、`formula_version` 等标识计算使用的定义或实现版本，便于复算和追溯。当前工作区计算产物与历史 `MethodSpec`、`MethodBundle` 按各自合同保存身份。方法改变时评估兼容性并维护版本依据。依据：[方法模型](../../../src/analysis/models.py)、[方法注册](../../../src/analysis/registry.py)、[方法版本规则](../../methodology/versioning.md)。

## 模型运行

`ModelRun` 记录一次方法执行的输入、事实与假设标识、输入血缘、结果和失败原因；状态由 `ModelRunStatus` 表达。研究工作区另以 calculation 产物保存当前工具执行记录，报告保留其审计关联。依据：[ModelRun](../../../src/analysis/models.py)、[估值引擎](../../../src/analysis/valuation.py)、[研究计算](../../../src/analysis/research/calculations.py)。

## 估值结果

`ValuationResult` 保存指定方法的每股价值、区间、币种、输入、敏感性和执行状态。输出的含义随方法、基期和假设确定；缺不可替代输入或方法不适用时按引擎结果表达。依据：[ValuationResult](../../../src/analysis/models.py)、[估值引擎](../../../src/analysis/valuation.py)。

## 情景

悲观、基准和乐观情景使用研究者分别提出的参数和依据，由代码执行相同方法。当前 PE 接口中 `scenarios.value` 为三项情景数组，增长参数相对绑定的盈利基期；值和约束查 schema。依据：[情景参数](../../../src/analysis/research/calculation_schema.py)、[研究计算](../../../src/analysis/research/calculations.py)。

## 敏感性

敏感性计算在指定参数网格上重复执行方法，观察价值如何随参数变化。网格范围和选择理由显式记录，并与基准输入、方法和快照保持关联。依据：[NumericGrid 与 PEAssumptions](../../../src/analysis/research/calculation_schema.py)、[计算实现](../../../src/analysis/research/calculations.py)。

## 计算输入血缘

计算记录保存绑定的事实 ID、假设及方法版本；派生值继续连接原始输入。选取输入时核对[事实口径与期间](../facts/GLOSSARY.md#期间与口径)，使同输入与同方法可以复算。依据：[ModelRun.input_lineage](../../../src/analysis/models.py)、[工作区计算产物](../../../src/analysis/research/calculations.py)。

## 探索计算

`run_python_analysis` 的数值探索在绑定研究快照的隔离运行时执行，保存代码、输入、定义、单位和结果。`exploration_` 引用沿独立验证路径使用；进入标准指标库另需方法登记、版本化和回归依据。依据：[探索实现](../../../src/analysis/research/custom_python.py)、[Python 沙盒合同](../../acquisition/research-python-sandbox.md)。

## 探索验证

验证检查定义与适用条件、输入与口径、独立复算或参考结果、重复执行及正常和失败边界。`validated` 表示该探索已满足其验证合同；标准方法的登记与推广另有流程。报告读取通过 `get_validated_exploration` 检查结果身份和当前绑定。依据：[探索验证与读取](../../../src/analysis/research/custom_python.py)、[产品验收约定](../../../项目总计划.md)。
