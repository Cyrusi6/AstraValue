## Purpose

本能力把冻结的正式材料转换为带字段级证据、抽取版本和时间血缘的治理声明与类型化事实，并以确定性准入和候选隔离防止歧义或 LLM 输出污染权威画像。

## ADDED Requirements

### Requirement: 每次抽取运行和声明均不可变且可复算
系统 SHALL 为每次原始快照抽取保存不可变运行身份、原始 snapshot ID 与内容 hash、问题 ID、抽取器种类与版本、schema 版本、输入输出声明、时间、错误和运行 hash。重新运行、规则升级、模型变化或修复 MUST 创建新运行，不得覆盖旧结果。

#### Scenario: 相同输入和版本重复抽取
- **WHEN** 两次运行使用相同原始 snapshot、抽取器版本、schema 和规范参数
- **THEN** 除运行身份和运行时间外，声明内容、排序和 canonical hash SHALL 完全一致

#### Scenario: 抽取规则升级
- **WHEN** 确定性表格规则发布新版本并重新处理旧 PDF
- **THEN** 系统 SHALL 保留两次运行及其声明关系，引用旧运行的 snapshot 和报告不得漂移

### Requirement: 字段级证据和时间血缘完整
每个治理声明 MUST 保存公司、问题、主体、谓词、类型化值、单位或币种、目标记录类型、reference_at、effective_at、valid interval、announced_at、available_at、retrieved_at、原始 snapshot、字段级 evidence span、上游材料和独立性组、抽取器与版本以及 supersedes 关系。Evidence span MUST 能定位到页码、表格/段落、字符范围或等价稳定位置，并保留摘录 hash。

#### Scenario: 从年报表格抽取薪酬
- **WHEN** 系统抽取某高管年度薪酬
- **THEN** 声明 SHALL 同时保存人员主体、年度、金额、币种、表格行列、页码或稳定定位、原始 snapshot ID 和可得时点

#### Scenario: 缺少字段定位
- **WHEN** 抽取器给出一个具体数值但无法定位原文证据
- **THEN** 该声明 MUST 校验失败或保持候选，不能进入权威事实

### Requirement: 抽取、校验和复核状态相互独立
每个声明 SHALL 分别记录 extraction_status、verification_status 和 review_status。extraction_status MUST 取 complete、partial、failed；verification_status MUST 取 passed、failed、conflicted、not_applicable；review_status MUST 取 not_required、pending、approved、rejected。系统不得用单一 confidence 或 approved 字段代替三轴状态，也不得因人工阅读过材料而自动把 partial、failed 或 conflicted 解释为完整。

#### Scenario: 抽取完整但单位校验失败
- **WHEN** 字段均被抽出但金额单位无法与表头和合计一致
- **THEN** extraction_status 可为 complete，verification_status MUST 为 failed，声明不得自动进入权威事实

#### Scenario: LLM 结果等待复核
- **WHEN** LLM 生成具有证据 span 的完整候选
- **THEN** extraction_status 可为 complete、verification_status 可记录机器校验结果，但 review_status SHALL 为 pending 且 canonical_eligible=false

### Requirement: 自动权威准入必须同时满足全部条件
系统 MUST 仅在来源角色为 official_disclosure 或 regulator_exchange、原始 hash 与血缘完整、字段证据可定位、抽取器为确定性、extraction_status=complete、verification_status=passed、review_status=not_required 且无未决冲突时自动生成权威事实。canonical_eligible MUST 由这些条件派生而不能由调用方或维护者直接填写；任一条件不满足 MUST 进入候选、冲突或失败状态，不能由高置信度替代。

#### Scenario: 无歧义表格通过全部校验
- **WHEN** 正式年报中的名册表格由固定版本确定性规则抽取并通过所有校验
- **THEN** 系统 SHALL 自动创建引用该声明和证据 span 的权威类型化记录，review_status=not_required

#### Scenario: 正则匹配正文存在歧义
- **WHEN** 正则从“辞去相关职务”提取出具体职务范围但原文没有明确说明
- **THEN** 系统 MUST 将结果保留为候选或冲突，不得关闭全部任期或进入权威画像

#### Scenario: 聚合来源数值通过校验
- **WHEN** AKShare 数值在类型和合计上均能通过校验
- **THEN** 系统 MUST 仍将其限制为 discovery lead，不能因校验成功自动进入权威事实

### Requirement: 全部 LLM 抽取先进入候选层
任何 extractor_kind=llm 的输出 MUST 先写入候选层并与权威记录隔离。若候选来自合格正式原文、证据定位与所有校验完整，可选数据维护可以通过 append-only ReviewDecision 批准生成新的权威记录；未批准候选不阻断治理快照查询或 Codex 报告，且普通报告 Codex 无权自我批准候选。

#### Scenario: LLM 正确识别复杂任免
- **WHEN** LLM 从正式公告正确识别多人、多职务和生效日
- **THEN** 结果 SHALL 作为 pending candidate 对 Codex 可见，但在 ReviewDecision 批准前不得出现在 canonical records

#### Scenario: 可选维护批准候选
- **WHEN** 维护者核对正式原文并提交 approved ReviewDecision
- **THEN** 系统 SHALL 追加决定和新的权威记录，保留原候选及旧 snapshot，不得原地改写历史

#### Scenario: 没有人处理候选
- **WHEN** pending candidate 长期未被人工维护
- **THEN** 系统 SHALL 在快照旁路和 Codex 输入中持续标明其状态，但报告流水线不得等待批准

### Requirement: 类型化治理事实覆盖规定对象
权威事实层 MUST 能表达完整名册与任期、正式履历、股权存量与控制链、质押存量、薪酬、关联关系与交易、激励计划/授予/归属条件、审计聘任与意见、内控意见/缺陷/整改、监管与问询、正式披露的重大诉讼、承诺和治理制度版本。变化事件与时点状态 MUST 分离但保持可追溯关联；自由键值字段不能成为权威存量的唯一表示。

#### Scenario: 任免事件更新名册状态
- **WHEN** 一个合格任免事件进入状态重建
- **THEN** 系统 SHALL 保留事件本身，并通过可追溯类型化任期表达状态变化，而不是只修改自由 event terms

#### Scenario: 一项激励计划包含多期条件
- **WHEN** 正式披露包含多个归属期和不同考核阈值
- **THEN** 系统 SHALL 分别保存计划、授予和各期条件及其单位/期间，不得压缩为一个无法复算的文本结论

#### Scenario: 诉讼没有外部法院数据
- **WHEN** v1 只有发行人正式披露的诉讼进展
- **THEN** 系统 SHALL 保存“正式披露所述阶段”和来源范围，不得推断法院完整案卷或未披露进展

### Requirement: 更正、撤回和冲突使用追加版本
更正、撤回或新材料 SHALL 创建新声明和新事实版本，通过 supersedes 或 conflict 关系连接旧版本。系统 MUST 按查询 known_at 选择当时已知版本；无法依据更正、法定层级或时间关系裁决的正式来源差异 MUST 保存 ConflictRecord，并阻止冲突精确值进入确定性 canonical 状态。

#### Scenario: 后续公告更正高管生效日
- **WHEN** T2 公开的更正把 T1 任职生效日由 D1 改为 D2
- **THEN** 系统 SHALL 追加更正版本；known_at 早于 T2 的历史查询保留 D1，之后的查询按 perspective 规则解析 D2

#### Scenario: 两份正式材料给出不同持股数
- **WHEN** 同一 reference_at 的两份正式材料数值冲突且没有可证明的替代关系
- **THEN** 系统 SHALL 保留两项声明和 ConflictRecord，不能任意平均、择一或产生确定存量

### Requirement: 事实层不得承载治理与管理层判断
治理事实 schema MUST 不包含治理总分、治理评级、管理层诚信或能力判断字段，也不得将 Codex 评价、新闻观点或缺失状态序列化成权威事实。可观察的披露、事件、数值和关系可以进入事实层，解释性判断只能存在于明确标注的报告层。

#### Scenario: Codex 评价管理层执行力
- **WHEN** Codex 基于任期、激励和公开业绩形成“执行力较强”的判断
- **THEN** 该文本 SHALL 只作为 Codex judgment 保存在报告中，并引用实际证据，不得创建 management_ability 权威 claim

#### Scenario: 没有找到处罚
- **WHEN** 相关 coverage 为 no_data、restricted 或 incomplete
- **THEN** 系统 MUST 不创建“诚信良好”或“无处罚”的权威事实
