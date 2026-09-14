## Purpose

本能力让项目大脑 Codex 从紧凑、可审计的治理快照开始按需查证，在缺口出现时受控调用独立联网任务，并在不受业务缺失或人工审批阻断的情况下形成结构化报告和可读 trace。

## ADDED Requirements

### Requirement: Codex 从紧凑且完整索引的输入包开始
每次治理报告运行 MUST 固定一个 Codex input pack，包含公司身份、state_at、known_at、perspective、snapshot/manifest ID 与 hash、问题级 coverage 和完整性、重要 canonical 事实与事件索引、全部活动 gap/conflict/pending candidate ID、可用工具 schema、补采预算、时点规则和报告输出 schema。系统 SHALL 不默认塞入全部原文或无关浏览记录。

#### Scenario: 不完整快照进入报告
- **WHEN** snapshot 同时包含两个 gap、一个 conflict 和三个 pending candidates
- **THEN** input pack SHALL 包含全部活动 ID 和状态，不能为了压缩上下文而静默遗漏

#### Scenario: 初始包大小受控
- **WHEN** snapshot 引用大量公告和证据片段
- **THEN** input pack SHALL 提供稳定摘要与索引，Codex 可通过工具按需读取原文，而不是预加载全部正文

### Requirement: Codex 证据工具按固定快照只读
Codex 工具 SHALL 支持查询治理概览、人员任期、控制与质押、薪酬激励、关联方、审计内控、监管诉讼承诺、事件、coverage/gap/conflict、record lineage 和 evidence excerpt。每个工具响应 MUST 包含固定对象 ID、版本、hash 和引用；工具不得修改 snapshot、批准候选或写入判断。

#### Scenario: Codex 深挖一项任命
- **WHEN** Codex 从概览取得 event ID 并请求 lineage 与 evidence excerpt
- **THEN** 工具 SHALL 返回同一固定 snapshot 可见的结构化链路和获授权片段，并记录本次实际读取

#### Scenario: 工具尝试批准候选
- **WHEN** 报告 Codex 请求把 LLM candidate 升级为 canonical
- **THEN** 只读工具 MUST 拒绝该变更；报告仍可引用候选状态并继续判断

### Requirement: 会话清单记录实际读取而不保存隐藏推理
每次报告运行 MUST 生成不可变 Codex session manifest，记录会话与报告 run、模型/工具协议版本、input pack ID/hash、按顺序发生的工具及规范参数、精确返回 payload 或不可变 artifact ID/hash、实际打开的记录/claim/span/原始版本、子任务与补采、snapshot adoption、最终引用集合、报告 hash 和 schema 校验。清单 MUST 不保存 token、Cookie、owner token、浏览器资料、未脱敏请求或隐藏 chain-of-thought。

#### Scenario: 多次按需查询
- **WHEN** Codex 先读名册、再读一项冲突双方、最后读取两个原文片段
- **THEN** session manifest SHALL 按顺序记录每次调用、参数、返回版本和实际片段，使相同证据上下文可以复算

#### Scenario: 运行失败
- **WHEN** 报告因 schema 或 integrity 错误未完成
- **THEN** 系统 SHALL 仍保存截至失败点的 session manifest 和机器可读原因，不得丢弃审计链

### Requirement: 缺口可触发受控补采并受预算约束
Codex SHALL 能为明确问题和 gap 发起受控 acquisition 或 fresh-context research task。每项任务 MUST 固定公司、问题、双时点、已有证据最小集合、允许来源、输出 schema、最大轮数/请求/并发/时间和递归深度。预算耗尽、网络失败或未找到资料 SHALL 形成软缺口并返回报告流程；系统不得无限递归或把任务失败解释为没有该事项。

#### Scenario: Codex 发现名册锚点缺失
- **WHEN** Codex 针对活动 gap 请求补采正式年报
- **THEN** broker SHALL 创建可审计任务和预算，成功材料重走采集/冻结/抽取流程，失败或耗尽预算后父流程继续

#### Scenario: 子任务达到请求上限
- **WHEN** fresh-context 子任务达到固定网络请求上限仍未找到正式原文
- **THEN** 任务 SHALL 终止为 budget_exhausted，保留 unresolved gap，父 Codex继续形成报告

#### Scenario: 子任务尝试递归派发
- **WHEN** v1 子 Codex 在最大递归深度 1 下请求再创建子任务
- **THEN** broker MUST 拒绝递归并返回机器可读预算状态，不得影响父任务继续

### Requirement: 子任务结果按来源角色回流
fresh-context 子任务 MUST 返回结构化 research result bundle，至少区分 authoritative_source_candidates、contextual_evidence、discovery_leads 和 unresolved_gaps。任何子任务叙述都不得直接创建 canonical 事实；正式原文候选 MUST 重新经过来源注册、受控获取、字节冻结、hash、available_at、抽取和校验，非正式材料永远不得进入权威画像。

#### Scenario: 子任务找到交易所公告
- **WHEN** 子任务返回一个交易所正式公告 URL
- **THEN** 系统 SHALL 将其作为正式来源候选重走采集门，只有完整验证后的记录才能进入新 snapshot

#### Scenario: 子任务找到新闻和 AKShare
- **WHEN** 子任务同时返回新闻、AKShare 数值和搜索摘要
- **THEN** 系统 SHALL 分别分类为 contextual 或 discovery，不得把任一结果直接写入 canonical facts

#### Scenario: result bundle schema 非法
- **WHEN** 子任务返回无法校验、缺失 task ID 或 hash 不一致的 bundle
- **THEN** 系统 MUST 将该任务标记为技术失败并阻止内容回流，父报告保留失败 trace

### Requirement: strict 父 Codex 不得接触未来实质信息
strict 报告中，fresh-context 子任务的 bundle MUST 在父上下文之外先通过来源、available_at 和 known_at gate。只有能够证明 available_at 不晚于 known_at 的内容才可进入父 Codex；不合格或无法证明时点的项目 MUST quarantine。父 Codex只能获知隔离数量和非实质状态，不得看到会泄漏未来信息的标题、摘要、数值或结论。strict 父 Codex 的任何开放联网访问 MUST 通过该 governed gateway 返回，不能把未校验网页直接注入父上下文。

#### Scenario: 子任务找到后来发布的更正
- **WHEN** strict T1 子任务找到 T2 才公开的更正且 T2 晚于 known_at
- **THEN** 父 Codex MUST 不收到更正标题、内容、数值或总结，只能收到一项因时点不合格被隔离的状态

#### Scenario: 非正式背景材料晚于 known_at
- **WHEN** 新闻被分类为 contextual_evidence 但发布时间晚于 strict known_at
- **THEN** 该新闻同样 MUST quarantine，不能因其不属于权威事实而绕过未来信息门

#### Scenario: 显式 reconstructed 报告
- **WHEN** perspective=reconstructed 且 later evidence 的 available_at 不晚于显式 known_at
- **THEN** 系统 SHALL 允许其按来源角色回流，并在 input pack 与报告标记 future_knowledge_used=true

### Requirement: 补采结果只能通过显式 snapshot adoption 生效
补采成功 MUST 生成新的 evidence manifest 和 GovernanceSnapshot。父 Codex SHALL 继续绑定原 snapshot，直到显式 adopt 新 snapshot；adoption MUST 记录旧/新 ID、hash、supersedes 关系、时点校验和采用位置。旧 input pack、工具返回、报告草稿及完成报告不得漂移。

#### Scenario: 新证据解决 gap
- **WHEN** 补采产生一个合格新 snapshot
- **THEN** 父 Codex只有在显式 adoption 后才能查询新事实，session manifest SHALL 记录切换边界

#### Scenario: 新材料未通过时点门
- **WHEN** 补采材料的 available_at 晚于 strict known_at
- **THEN** 系统 MUST 不提供可采用 snapshot 给父 Codex，父流程继续使用原 snapshot

### Requirement: 业务不确定性不阻断 Codex 自主报告
complete、incomplete、conflicted、no_data、pending candidate、未决人员链接、来源受限、子任务失败或预算耗尽 MUST 均允许报告进入生成阶段。Codex MUST 获得这些状态和证据，并可自主决定其结论；系统不得强制降级评级、强制固定措辞、强制“无法判断”或等待人工批准。

#### Scenario: incomplete 快照报告
- **WHEN** 名册缺少完整锚点但有若干正式任免证据
- **THEN** Codex SHALL 收到局部事实和 gap，并能生成 generation_status=completed 的结构化报告

#### Scenario: conflict 快照报告
- **WHEN** 正式来源对持股数存在未解决冲突
- **THEN** Codex SHALL 收到冲突双方和引用并继续判断，事实层不得提供伪确定数值

#### Scenario: 无人工参与
- **WHEN** 一次报告输入含 pending LLM candidates 且没有人执行 ReviewDecision
- **THEN** 报告流程 SHALL 不等待人工队列，decision_author SHALL 为 codex

### Requirement: 技术完整性违规阻断完成报告
原始或 snapshot hash 不匹配、断裂血缘、strict 未来信息泄漏、非法时间关系、跨 namespace/scope 污染、ResearchResultBundle 或 research/session manifest 无法持久化或 hash/schema 非法、最终报告 schema 无效 MUST 阻断 generation_status=completed。系统 SHALL 保留失败 trace，且不得把技术失败降级成普通业务缺口。

#### Scenario: strict 会话读到未来证据
- **WHEN** validator 发现 session manifest 包含 available_at 晚于 known_at 的实质 payload
- **THEN** 系统 MUST 阻止完成报告并标记 future_leakage integrity failure

#### Scenario: 最终 JSON 不符合 schema
- **WHEN** Codex 返回缺少必填 snapshot ID 或引用格式错误的报告
- **THEN** 系统 MUST 保存失败状态和校验诊断而非发布完成报告

#### Scenario: session manifest 持久化失败
- **WHEN** 工具返回已被 Codex读取但系统无法持久化对应 artifact/hash
- **THEN** 系统 MUST 中止完成报告，因为无法证明实际读取内容

### Requirement: 报告判断与权威事实分层
结构化治理报告 MUST 固定 company、双时点、perspective、snapshot、session manifest、generation status、decision author、引用、数据限制和 technical validation。Codex 对治理质量、管理层诚信或能力的评价 MUST 标记为 Codex judgment 并引用实际读取证据；报告不要求治理总分，任何判断不得反写为权威 GovernanceClaim。

#### Scenario: Codex 形成诚信判断
- **WHEN** Codex 根据监管材料和更正历史评价管理层诚信
- **THEN** 报告 SHALL 把该内容标为 judgment、列出引用和不确定性，事实层保持原始监管事项与更正记录

#### Scenario: Codex 不输出总分
- **WHEN** 报告 schema 校验一份只有定性分节结论的有效报告
- **THEN** 系统 SHALL 接受报告，不得要求填充治理总分或固定评级字段

### Requirement: 每次运行生成八份确定性可读 trace
每次报告运行 SHALL 从权威 JSON/SQLite 对象自动生成 acquisition coverage、evidence manifest、extraction results、entity resolution、governance reconstruction、Codex input pack、Codex assessment 和 report validation 八份可读 trace。它们 MUST 稳定排序并与机器对象的 ID、hash、时点、状态、数量和纳入/排除原因一致；人工修改不得回写权威数据。

#### Scenario: 生成正常报告 trace
- **WHEN** 一次报告运行完成
- **THEN** 八份 trace SHALL 全部存在，且 reconstruction 逐项列出锚点、已应用/排除增量，input pack 列出全部活动 gap/conflict/candidate

#### Scenario: 生成技术失败 trace
- **WHEN** 报告因 hash 或 schema 错误失败
- **THEN** 已可生成的 trace SHALL 保留，report validation MUST 将 hard checks 与 soft states 分开显示

#### Scenario: 相同输入重复渲染
- **WHEN** 使用相同权威对象和 renderer 版本重复生成 trace
- **THEN** 除明确允许的运行身份字段外，UTF-8 内容和排序 SHALL 确定性一致，且不得包含凭据、隐藏推理或私有绝对路径

### Requirement: 三类验收状态独立记录
项目 MUST 分别记录自动化确定性门、真实联网样本门和开发期人工黄金门，任一通过不得自动标记其他门通过。真实联网结果只能证明执行时指定来源与样本；人工黄金验收只检查证据和投影，不成为运行时报告审批。

#### Scenario: 离线测试通过但尚未联网
- **WHEN** 所有冻结夹具测试通过而在线与人工验收未执行
- **THEN** 状态 SHALL 显示“自动化 passed、在线 pending、人工 pending”，不得声称 v1 全部验收完成

#### Scenario: 单公司在线样本通过
- **WHEN** 600519 的正式来源样本成功
- **THEN** 系统 SHALL 只记录该公司、来源、时间和限制，不能外推为全 A 股或全历史通过
