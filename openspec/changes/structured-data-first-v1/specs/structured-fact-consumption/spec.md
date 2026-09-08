## Purpose

允许定义明确并通过程序检查的供应商结构化数据直接进入研究，同时明确区分其取得方式、数据性质、质量状态、版本和时间依据，保持已有正式披露、双源核验、历史报告与严格时点输入的原有语义。

## ADDED Requirements

### Requirement: Provider facts are usable without formal disclosure
系统 SHALL 为有效供应商直采建立独立状态，表达实际取得方式、客观/确定性性质、质量、策略版本及可消费性。缺少正式 PDF、页码或第二源不得单独构成不可用理由；不得伪写为双源一致或权威单源。

#### Scenario: Sole structured revenue source
- **WHEN** 指定东方财富营收字段具有有效值、期间、单位、来源和快照定位且通过整理
- **THEN** 同步、公式输入及报告可消费该值，状态显示供应商直采；没有强制 PDF 或第二源请求

#### Scenario: Forged or unbound accepted field
- **WHEN** 输入声称供应商直采但缺少实际来源、策略/字段定义或响应定位
- **THEN** 系统拒绝该准入状态或记录具体质量缺口，不仅凭调用者填写的状态放行

### Requirement: Immutable provenance and revisions
系统 MUST 保留真实上游、接口/参数摘要、取得时间、报告期、来源发布日期、快照 hash、响应行键和字段路径。新版本追加保存；晚到记录不能覆盖新版本，已知矛盾不平均，受影响字段单独记录冲突。

#### Scenario: Supplier revises an already used value
- **WHEN** 相同行键出现新响应版本且字段值改变
- **THEN** 追加版本与修订关系，旧记录、旧同步和旧报告保持可查询；当前研究显式选择适用版本

### Requirement: Separate current research and strict historical availability
系统 SHALL 允许当前研究使用现有供应商历史序列；严格历史输入必须有相应 available_at/历史版本依据。无法证明当时可得的值不能回填为当时已知，但不因此阻断当前研究。

#### Scenario: Historical value first retrieved today
- **WHEN** 今天取得过去报告期的当前供应商值且没有当时版本证明
- **THEN** 当前研究可用，available_at 不伪造为过去；严格历史查询在该可得时间前排除该值

### Requirement: Consumption and presentation use the same quality rules
报告、API、导出和前端 SHALL 使用同一事实质量与来源状态，不把有效供应商值继续列作“缺少正式披露”或将新状态当未知错误。平台标签、预测、文本与估计独立保存，不能进入历史实际金额的确定性运算。

#### Scenario: Report and exports use provider facts
- **WHEN** 从已保存的结构化同步生成报告并导出
- **THEN** 各输出引用相同事实 ID、数值、期间及来源状态；覆盖提示区分可消费字段、缺失字段与旧双源数量

### Requirement: Legacy compatibility is explicit
系统 MUST 保留旧七种核验状态、旧双源算法与历史报告语义。新默认来源策略不重新标记旧保存行；显式旧同步/核验路径可复现原行为，新增调用按新策略执行并记录版本。

#### Scenario: Load an old report after upgrade
- **WHEN** 按固定 ID 读取升级前的报告、来源或快照
- **THEN** 返回其原记录和来源策略语义，不补造供应商准入、第二来源、历史时间或人工复核
