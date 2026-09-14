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

### Requirement: Question readiness follows required evidence and applicability
系统 SHALL 对公司、问题、研究期间及冻结需求版本逐项计算“可分析 / 待补 / 不适用”。适用条件未知、必需输入缺失/过期/失败、公式依赖不全、必要章节尚未回答均为待补；只有有依据地判为不适用才可排除。可分析表示该问题的数据输入齐备，方法就绪、假设确认和分析完成必须独立显示；不得以接口成功、无记录、PDF 下载或模型生成答案代替数据覆盖。

#### Scenario: Empty event response cannot prove no event
- **WHEN** 质押接口本次无记录，且没有证明该期间无质押的可用材料
- **THEN** 质押相关问题显示待补和供应商无记录原因，不自动显示不适用或无质押风险

#### Scenario: Complete inputs meet an unfinished method
- **WHEN** 某问题全部数据可用，但关联方法仍为 skeleton 或估值假设未确认
- **THEN** 数据状态可以为可分析，同时明确方法待完善或假设待确认；不得输出整体八步完成或自动确认投资结论

#### Scenario: Applicability is evidenced
- **WHEN** 明确的行业规则或覆盖研究期间的材料证明某项业务不适用
- **THEN** 以规则/材料 ID、时间和理由记录不适用，保留原问题条目；单个空值或接口不支持不是该证明

### Requirement: Coverage preserves required periods and all eight steps
系统 MUST 按请求的分析期间和问题定义展开必需输入，分别报告当前截面、历史趋势和供应商全历史采集状态。默认分析窗口为近五个完整年度及十二个已公布季度，年度同比和 TTM 等另带依赖期；上市前期间可有依据地排除，缺少供应商上市后历史不能排除。八步全部列出，问题部分就绪时不得靠其他问题的可选字段、重复字段或不适用项提高完成率。

#### Scenario: Latest values hide historical gaps
- **WHEN** 最近营收可用但分析窗口内一个必需历史季度缺失
- **THEN** 最新截面可分析、对应历史问题待补，两者分别显示；接口本次成功或全历史任务未结束不改变这一判断

#### Scenario: One ready question in a partially covered step
- **WHEN** 财务步骤十个问题只有部分必需输入齐备
- **THEN** 展示可分析问题与待补问题及缺项清单，该步骤仍为待补并可展示已有分析；八步汇总不隐藏该步骤

### Requirement: Coverage snapshots are traceable and consistent across outputs
系统 SHALL 保存公司身份、行业与同行版本、问题/需求/来源版本、分析范围、输入证据、缺口和判定时间的不可变覆盖快照。API、CLI、报告、前端和导出使用相同结果，区分数据就绪计数、可选增强、采集历史覆盖与研究完成；旧需求快照不得在配置变更后被原地重写。

#### Scenario: A new required field changes current readiness
- **WHEN** 新需求版本增加渠道库存必需项，而旧版本问题已经可分析
- **THEN** 新覆盖如实显示待补，旧快照保持原版本及判定，所有输出可以定位到各自确切版本
