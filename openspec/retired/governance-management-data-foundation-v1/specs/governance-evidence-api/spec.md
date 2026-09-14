## Purpose

本能力提供稳定、可分页且不泄漏未授权内容的治理画像、事件、覆盖、候选、冲突、血缘和证据查询接口，使人类客户端与 Codex 可以基于同一不可变快照按需读取。

## ADDED Requirements

### Requirement: 查询接口显式回显双时点和快照身份
GET /api/companies/{ticker}/governance-view SHALL 只读选择与规范化双时点、perspective 和固定版本匹配的已有最新可消费 snapshot；它不得发起外部 I/O、启动采集或抽取、晋升候选、构造临时画像、隐式冻结或改变 latest snapshot。POST /api/companies/{ticker}/governance-snapshots MUST 是创建不可变 snapshot 的唯一治理画像入口；GET /api/governance-snapshots/{snapshot_id} SHALL 按固定 ID 原样读取。所有成功响应 MUST 回显规范化 company identity、state_at、known_at、perspective、question set 与版本、evidence manifest ID、snapshot ID/hash、completeness_status 和 future_knowledge_used。

#### Scenario: 读取默认 strict view
- **WHEN** 客户端对公司提交仅含 state_at 的治理 view 请求
- **THEN** 响应 SHALL 选择已有的精确 strict snapshot 并回显 known_at=state_at、perspective=strict、完整性和所有固定版本，不得产生未请求的新 snapshot

#### Scenario: 尚无匹配 view
- **WHEN** 客户端请求的双时点和版本不存在已有可消费 snapshot
- **THEN** GET governance-view SHALL 返回明确 snapshot_not_found，而不是在只读请求中重建、写入或返回无 ID 的临时画像

#### Scenario: 显式创建快照
- **WHEN** 客户端引用已冻结 evidence manifest 和合法双时点提交 snapshot 创建请求
- **THEN** 系统 SHALL 返回新不可变 snapshot ID、hash 和重建清单，后续 GET 可按 ID 原样读取

#### Scenario: 未知快照
- **WHEN** 客户端读取不存在的 governance snapshot ID
- **THEN** API SHALL 返回明确 404，不得返回空画像或伪造 no_data

### Requirement: 类型化状态和事件提供独立只读查询
API SHALL 提供固定路径 GET /api/governance-snapshots/{snapshot_id}/roles、ownership、control-graph、pledges、events、coverage、conflicts 和 candidates，并可通过明确的类型过滤或子资源查询薪酬与激励、关联方、审计与内控、监管/诉讼/承诺以及治理制度。GET /api/governance-records/{record_id}/lineage 和 GET /api/evidence-spans/{span_id} SHALL 提供字段证据链。事件响应 MUST 表达变化，类型化响应 MUST 表达时点状态；二者须通过稳定记录和证据 ID 关联。

#### Scenario: 查询管理层名册
- **WHEN** 客户端读取固定 snapshot 的 roles 资源
- **THEN** API SHALL 返回公司内 person ID、规范和原始职务、有效区间、acting 状态、证据引用和记录完整性

#### Scenario: 查询控制图
- **WHEN** 客户端读取固定 snapshot 的 control graph
- **THEN** API SHALL 返回节点、直接/间接关系、有效区间、chain path 和证据，不得把未批准跨公司人员链接当成控制关系

#### Scenario: 事件与状态相互追溯
- **WHEN** 客户端从质押存量读取导致变化的 event ID
- **THEN** API SHALL 能返回对应事件、claim 和原始证据血缘，且不要求解析自由文本摘要

### Requirement: canonical 与不确定性旁路明确分离
默认治理状态响应 MUST 只在 canonical records 中返回权威事实，并 MUST 在独立命名字段或端点中暴露 active gaps、conflicts、pending candidates 和 question-level coverage。候选、冲突值或 discovery lead 不得混入 canonical 集合；客户端也不得因某旁路集合为空推断业务负面事实。

#### Scenario: 快照包含 LLM 候选
- **WHEN** 固定 snapshot 旁存在尚未批准的 LLM 管理层候选
- **THEN** 默认 roles canonical 列表 SHALL 不包含它，candidates 端点 SHALL 返回候选状态和证据引用

#### Scenario: 权威数值冲突
- **WHEN** 持股记录存在未解决 ConflictRecord
- **THEN** ownership 响应 SHALL 在 conflicts 中给出双方引用，并不得在 canonical position 中选择或计算单一数值

#### Scenario: 覆盖为空
- **WHEN** 某问题 coverage=no_data
- **THEN** API SHALL 返回执行和终止证明，不得附加“无该事项”的事实结论

### Requirement: 血缘和证据片段查询受限且完整
每个 canonical、candidate、conflict 和报告引用 MUST 可查询到声明、抽取运行、原始 snapshot、来源观测、available_at、内容 hash、evidence span 与版本关系。证据片段接口 MUST 只接受登记过的 span ID、执行来源 LLM/访问策略和脱敏规则，并且不得接受任意本地路径或任意 URL。

#### Scenario: 查询记录血缘
- **WHEN** 客户端读取一个薪酬记录的 lineage
- **THEN** API SHALL 返回从类型化记录到 claim、抽取版本、span、原始 snapshot 和来源观测的完整 ID/hash 链

#### Scenario: 请求任意文件路径
- **WHEN** 客户端把本地路径或未登记 URL 作为 evidence 请求
- **THEN** API MUST 返回校验错误且不得读取或泄漏目标内容

#### Scenario: 来源禁止 LLM
- **WHEN** Codex 工具请求一个来源策略禁止 LLM 处理的 span
- **THEN** API MUST 拒绝返回正文并记录策略状态，不能用摘要绕过

### Requirement: 序列化、分页和错误状态稳定
所有治理查询 MUST 使用版本化响应 schema、UTC aware RFC3339 时间、稳定字段排序、稳定集合排序和不可变分页游标。相同 snapshot 与参数 SHALL 产生等价 payload hash。参数错误、未知资源、版本冲突、完整性失败、活跃写冲突和暂时存储繁忙 MUST 使用不同机器可读错误，不能伪装成空结果。

#### Scenario: 重复分页
- **WHEN** 客户端对同一 snapshot、过滤和 page cursor 重复请求
- **THEN** 返回项目、顺序、下一游标和 payload hash SHALL 一致

#### Scenario: 非法双时点
- **WHEN** API 收到 known_at 早于 state_at 或隐式事后重建参数
- **THEN** API SHALL 返回机器可读 422，且不得创建 snapshot 或改变状态

#### Scenario: 存储暂时繁忙
- **WHEN** 显式 snapshot 创建在有限 busy timeout 内无法取得写锁
- **THEN** API SHALL 返回可重试 storage_busy，而不是 404、no_data 或重建缺口

### Requirement: 现有事件和报告读者保持兼容
新增治理 API 和 schema SHALL 为增量扩展。现有 EventRecord、document、lineage、sync 和旧报告必须继续可读；旧数据缺少新治理身份或血缘时 MUST 标为 legacy_unassessed，而不得反向制造治理 claim、coverage、snapshot 或 available_at。

#### Scenario: 读取旧 EventRecord
- **WHEN** 数据库包含迁移前只有自由 event terms 的管理层事件
- **THEN** 旧事件 SHALL 原样可读，新治理状态 SHALL 将其视为 legacy_unassessed，不能自动升级为 canonical 任期

#### Scenario: 关闭治理功能
- **WHEN** governance_management_v1 feature flag 关闭
- **THEN** 现有事件级第三步和旧 API SHALL 继续工作，新治理写入口停止且已冻结数据不被删除

### Requirement: API 不泄漏运行秘密和私有路径
治理、证据、研究和会话接口 MUST 不返回 token、Cookie、owner token、浏览器配置、未脱敏请求、数据库/data-root 绝对路径或隐藏模型推理。对审计所需内容 SHALL 返回稳定相对 artifact ID、hash 和已授权摘录。

#### Scenario: 返回会话清单
- **WHEN** 客户端读取 Codex session manifest
- **THEN** 响应 SHALL 包含工具、参数、artifact ID/hash 和引用，但 MUST 不包含凭据、隐藏 chain-of-thought 或本机绝对路径
