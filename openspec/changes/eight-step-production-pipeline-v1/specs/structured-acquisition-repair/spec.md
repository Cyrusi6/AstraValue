## MODIFIED Requirements

### Requirement: Repair 状态和证据完整可核对
系统 MUST 输出 manifest hash、冻结 revision/namespace、目标和实际 attempt 数、按公司/数据集/原因的成功、空结果、失败、耗尽、未执行及来源状态。输出不得包含凭据或原始敏感正文；生产补采完成也不得自动标记独立人工验收通过。已完成结构化运行 MAY 被后续事实物化层读取，但物化 MUST 只引用该运行实际提交的记录、快照和冻结来源版本，并单独保存物化版本与覆盖结果。

#### Scenario: 部分补采完成
- **WHEN** 一批 repair item 中同时出现成功、空结果、来源不可用和耗尽
- **THEN** 状态输出逐类列出数量和 job 标识，保留 pagination/row-key/safe-through 结果，不以部分成功报告整批完成

#### Scenario: 物化只消费已提交结果
- **WHEN** 事实物化请求引用一个已完成结构化运行
- **THEN** 只有该运行的已提交 page/record/field 进入事实，失败 attempt、别的 namespace 或当前配置中的未冻结字段均不会进入报告

#### Scenario: 显式后补字段解释
- **WHEN** 已提交记录通过单独版本化、有来源依据且声明适配原冻结字段的解释合同被重新物化
- **THEN** 新投影保存原采集版本与新增解释版本，不修改原运行、字段或快照，不把新解释冒充原采集时已存在的配置，仍遵守事实物化的可得时间约束
