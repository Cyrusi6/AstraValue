## Purpose

本能力建立公司内部可审计的人员身份、别名和任期语义，并用显式、追加式的跨公司链接决定防止同名、相似履历或 Codex 推断导致错误人员合并。

## ADDED Requirements

### Requirement: 公司内人员 ID 是权威身份
每个治理人员 MUST 具有包含 company_id 命名空间的稳定 person_id。姓名、曾用名、英文名、职务写法和履历相似度只能作为公司内解析证据或别名，不得单独形成跨公司统一身份。

#### Scenario: 两家公司都有张三
- **WHEN** 两家公司各自正式披露一名“张三”担任相同职务
- **THEN** 系统 SHALL 创建两个公司命名空间内的 person_id，不得自动合并

#### Scenario: 同公司姓名写法变化
- **WHEN** 同一公司后续披露使用该人员的英文名或不同空格写法且有确定性任职链支持
- **THEN** 系统 SHALL 保留稳定 person_id 并追加带证据的 alias，不得新建无法关联的重复人员

### Requirement: 人员别名和履历声明保留证据边界
每个姓名别名和正式履历声明 SHALL 引用公司、原始 snapshot、字段级 evidence span、available_at 和抽取版本。无法确定属于哪个公司内人员的履历片段 MUST 保持候选，不能靠同名自动附着。

#### Scenario: 年报披露多人同姓同名
- **WHEN** 同一材料中出现两个同名人员且履历和职务不同
- **THEN** 系统 SHALL 保存两个公司内身份及各自证据，不得把履历混合到一个 person

#### Scenario: 第三方人物页提供履历
- **WHEN** 非正式人物页给出与公司披露更详细的经历
- **THEN** 该内容不得进入权威 BiographyClaim；经时点校验后最多作为 Codex contextual evidence

### Requirement: 任期支持一人多职与多人同职
任期记录 MUST 分别保存 person_id、规范 role code、原始职务文本、机构范围、valid_from、valid_to、acting 状态、任命和终止证据。系统 SHALL 允许一人同时拥有多个职务，也 SHALL 允许依法或披露上可多人并存的职务同时存在；不能用新任记录无条件关闭所有同名或同类任期。

#### Scenario: 一人同时担任董事和总经理
- **WHEN** 正式披露明确同一人员同时担任两个职务
- **THEN** 系统 SHALL 创建两个可独立起止的 RoleTenure，并共享同一公司内 person_id

#### Scenario: 多名副总经理并存
- **WHEN** 完整名册列出多名副总经理
- **THEN** 系统 SHALL 保留所有任期，不得因 role code 相同互相关闭

#### Scenario: 代理职务转为正式任职
- **WHEN** 一名人员先代理职务、后被正式聘任
- **THEN** 系统 SHALL 保留代理与正式阶段的可追溯关系和各自有效区间，不得丢失代理事实

### Requirement: 任期结束必须有明确证据
valid_to 为空 MUST 只表示“没有结束证据”，不得等同于“确认仍在任”。辞任、免职、换届或接任只有在正式证据能明确人员、职务范围和生效语义时才能确定性关闭任期；歧义文本必须形成候选、gap 或 conflict。

#### Scenario: 辞去相关职务
- **WHEN** 公告只写某人“辞去相关职务”且没有列举全部职务
- **THEN** 系统 MUST 不自动关闭该人员全部 RoleTenure，并 SHALL 暴露待解析候选或冲突

#### Scenario: 新董事长接任
- **WHEN** 正式决议明确新董事长自 D 日起接任且前任同时卸任
- **THEN** 系统 SHALL 在 D 日闭合前任对应任期并开始新任期，保留两条证据链

#### Scenario: 查询 current
- **WHEN** 客户端请求某时点的在任人员
- **THEN** current 状态 SHALL 由双时点重建派生并附完整性状态，不能只用 valid_to 为空过滤

### Requirement: 跨公司身份只能通过显式链接决定
系统 SHALL 用 PersonLinkCandidate 表达跨公司可能同一人的建议，并 MUST 用 append-only PersonLinkDecision 表达 approved 或 rejected 决定。只有在 snapshot 的 known_at 当时已经可得、且查询时最新有效的 approved 决定可以影响跨公司聚合；决定自身 MUST 保留依据、available_at 和版本关系。决定不得改写公司内 person_id，普通报告 Codex 的建议不得直接批准链接。

#### Scenario: 相同姓名和高度相似履历
- **WHEN** 两家公司人员姓名、毕业院校和任职年份高度相似但没有已批准链接
- **THEN** 系统 SHALL 只生成 PersonLinkCandidate，跨公司查询仍视为不同人员

#### Scenario: 链接被明确批准
- **WHEN** 一个具有决策来源和证据的 approved PersonLinkDecision 生效
- **THEN** 跨公司视图 SHALL 通过链接组展示两个人员，但各公司 person_id 和历史 snapshot 保持不变

#### Scenario: 后续发现链接错误
- **WHEN** 新证据证明已批准链接错误
- **THEN** 系统 SHALL 追加 superseding rejected 决定，后续查询停止聚合，旧查询和旧报告仍按当时决定可复现

#### Scenario: 链接决定晚于历史 known_at
- **WHEN** 一个 approved 决定的 available_at 晚于历史 snapshot 的 known_at
- **THEN** 该历史 snapshot MUST 不使用该决定，只有满足双时点可见性的后续或 reconstructed 查询才能应用

### Requirement: 身份冲突不阻断报告
未决别名、未决跨公司链接和任期冲突 SHALL 作为结构化状态进入治理 snapshot 旁路和 Codex 输入。它们不得静默合并人员，也不得成为报告生成的人工审批门禁。

#### Scenario: 报告时链接仍待定
- **WHEN** Codex 生成报告时存在 pending PersonLinkCandidate
- **THEN** Codex SHALL 能读取候选与双方证据并继续生成报告，权威公司内画像保持分离
