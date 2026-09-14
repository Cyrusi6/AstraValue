## Purpose

本能力定义治理画像的双时点查询、完整披露锚点与公告增量重建、缺口冲突表达和不可变快照，使历史报告默认只使用当时已经可得的信息，并允许显式、可审计的事后重建。

## ADDED Requirements

### Requirement: 双时点和 perspective 具有严格默认语义
每次治理画像查询 MUST 接受 state_at、known_at 和 perspective。只提供 state_at 时，系统 SHALL 令 known_at=state_at 且 perspective=strict；strict MUST 要求两个时点相等。known_at 大于 state_at 时，调用方 MUST 显式请求 perspective=reconstructed；known_at 小于 state_at 在 v1 MUST 返回参数错误。

#### Scenario: 默认严格查询
- **WHEN** 调用方只提交 state_at=T
- **THEN** 系统 SHALL 以 state_at=T、known_at=T、perspective=strict 执行，并产生与显式提交三项参数逐字节等价的规范结果

#### Scenario: 隐式请求事后知识
- **WHEN** 调用方提交 state_at=T1、known_at=T2 且 T2 晚于 T1，但省略 reconstructed
- **THEN** 系统 MUST 返回 422 类参数错误，不得静默使用未来知识

#### Scenario: 显式事后重建
- **WHEN** 调用方提交 state_at=T1、known_at=T2 且 T2 晚于 T1，并显式指定 reconstructed
- **THEN** 系统 SHALL 允许使用不晚于 T2 的后来披露，并在响应标记 future_knowledge_used=true

#### Scenario: known_at 早于 state_at
- **WHEN** 调用方提交 known_at 早于 state_at
- **THEN** v1 MUST 返回 422 类参数错误，不得把未来状态预测混入历史重建

### Requirement: 证据可见性由可证明 available_at 决定
严格和事后查询均 MUST 在任何事实、标题、摘要、数值或推断进入计算前验证 available_at 不晚于 known_at。系统 SHALL 保存 UTC aware 时间、原始时区和精度；只有日期而无时分秒的来源 MUST 使用保守可得边界，不能猜测日内发布时间。无法证明历史可得性的可变网页 MUST 以首次成功 retrieved_at 作为 available_at。

#### Scenario: 迟披露任职
- **WHEN** 任职在 T1 生效但材料在 T2 才公开
- **THEN** strict T1 查询 MUST 不可见该任职；只有 known_at 不早于 T2 的 reconstructed 查询才能使用

#### Scenario: 来源只提供公告日期
- **WHEN** 来源只给出 Asia/Shanghai 日期且查询截止在该日期的日内时点
- **THEN** 系统 SHALL 以下一本地日界作为保守可得上界，并不得提前纳入证据

#### Scenario: 当前网页声称较早生效日期
- **WHEN** 系统今天首次归档一个当前管理层网页，页面称人员多年前就任
- **THEN** 系统可保存其业务 effective_at 声明，但 available_at MUST 不早于首次 retrieved_at，不能污染过去 strict 查询

### Requirement: 重建先选择可见版本再执行锚点加增量
系统 SHALL 按固定顺序重建每类状态：先筛选 available_at 不晚于 known_at 的证据；再解析在 known_at 当时已知的更正、撤回和 supersedes 版本；选择 reference_at 不晚于 state_at 的最近完整锚点；最后只应用 effective_at 不晚于 state_at 且 available_at 不晚于 known_at 的合格增量。乱序到达不得改变业务排序，所有采用和排除的增量 MUST 有稳定理由。

#### Scenario: 完整名册加任免增量
- **WHEN** 可见集合包含完整年度名册、两项任命、一项辞任和一项代理任职
- **THEN** 系统 SHALL 从最近完整名册出发，按生效时间和稳定 tie-breaker 应用合格增量，并列出每个已应用事件

#### Scenario: 公告乱序到达
- **WHEN** 较早生效的公告晚于较晚生效公告被系统抓取
- **THEN** 重建结果 SHALL 依据 effective_at、版本关系和规范稳定顺序，而不是抓取顺序

#### Scenario: 更正尚未公开
- **WHEN** 更正已存在于证据库但其 available_at 晚于本次 known_at
- **THEN** 版本选择 MUST 完全忽略更正的实质内容，并继续使用当时可知旧版本

### Requirement: 无锚点、断档和冲突返回可解释状态
当不存在完整锚点、锚点后 coverage 断档、必要数字不足或存在无法裁决的权威冲突时，系统 SHALL 返回所有可支持事实以及 GapRecord 或 ConflictRecord，并将画像标记为 incomplete 或 conflicted。系统不得为追求完整响应而猜测事实，也不得把这些业务状态转换成技术错误。

#### Scenario: 只有零散任免公告
- **WHEN** 查询范围没有完整名册锚点但存在若干合格任免事件
- **THEN** 系统 SHALL 返回事件和能证明的局部任期，标记 incomplete 并说明缺少锚点

#### Scenario: 两个权威持股数冲突
- **WHEN** 同一基准日存在无法由更正链裁决的两个正式持股数
- **THEN** 系统 SHALL 返回 ConflictRecord 和双方证据，不得输出一个确定持股存量

#### Scenario: coverage 受限
- **WHEN** 锚点后的某段公告查询因访问限制未完成
- **THEN** 系统 SHALL 将该时间段作为 gap 保留，不能把当前可见事件集合称为完整

### Requirement: 任期和数值状态不得过度推导
valid_to 为空 MUST 只表示没有结束证据；current 必须是重建查询的派生状态。多人职位不得互相关闭，通常单一职位的无依据重叠 MUST 产生 conflict。股权、质押、解押和增减持只有数量、单位和计算基数足以守恒时才能更新精确存量，否则只保留事件并将精确状态标为 incomplete。

#### Scenario: 部分解押缺少数量
- **WHEN** 正式公告说明部分解押但无法抽取可靠数量或基数
- **THEN** 系统 SHALL 保存解押事件，不得自行计算剩余质押数量，并标记相应存量 incomplete

#### Scenario: 接任关系没有卸任说明
- **WHEN** 新人员被任命为通常单一职务但材料没有说明前任是否卸任
- **THEN** 系统 MUST 不凭常识关闭前任，并 SHALL 产生重叠 conflict 或缺口

#### Scenario: valid_to 为空
- **WHEN** 某任期没有结束证据但锚点后 coverage 不完整
- **THEN** API MUST 不把该人员无条件标为“确认在任”，并 SHALL 暴露不完整性

### Requirement: GovernanceSnapshot 不可变且可确定性复算
显式冻结的治理 snapshot MUST 固定公司、双时点、perspective、acquisition_scope、question_set_id、question_set_version、source_registry_version、query_pack_version、抽取/重建版本、证据 manifest、锚点、采用和排除的增量、canonical 记录、活动 gap/conflict/candidate、问题级 coverage、完整性、future_knowledge_used、supersedes 关系和 canonical hash。补采、更正或新链接决定只能创建新 snapshot；旧 snapshot、输入包和报告不得变化。

#### Scenario: 相同冻结输入运行两次
- **WHEN** 两次冻结使用相同证据 manifest、规则版本和查询参数
- **THEN** 排除创建 ID 与时间后，规范内容、排序和 canonical hash SHALL 一致

#### Scenario: 补采后形成新快照
- **WHEN** 受控补采增加合格证据并解决一个 gap
- **THEN** 系统 SHALL 创建带 supersedes_snapshot_id 的新 snapshot，旧 snapshot 和引用旧 snapshot 的报告 hash 保持不变

#### Scenario: 更正发布后回看旧严格快照
- **WHEN** 后来更正已经入库但客户端读取更正前冻结的 strict snapshot
- **THEN** 系统 SHALL 原样返回旧 snapshot，不得按当前知识动态重算或替换内容

### Requirement: 未来信息泄漏和血缘损坏失败关闭
若 strict 重建检测到任何实质内容的 available_at 晚于 known_at，或 snapshot 引用存在 hash 不匹配、断裂血缘、跨 scope/namespace 记录或非法时间关系，系统 MUST 阻止 snapshot 成为可消费结果并保存机器可读失败诊断。

#### Scenario: future claim 被注入 strict manifest
- **WHEN** 一个 available_at 晚于 known_at 的 claim 被故意加入 strict snapshot 输入
- **THEN** 完整性校验 MUST 失败并阻止 snapshot 发布，不能仅标记为普通 gap

#### Scenario: 原始字节 hash 不匹配
- **WHEN** 重建引用的原始 snapshot 内容与固定 hash 不一致
- **THEN** 系统 MUST 将运行标为 integrity failure，保留失败审计且不得输出完成画像
