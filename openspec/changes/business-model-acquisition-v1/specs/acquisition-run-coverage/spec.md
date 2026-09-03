## Purpose

本能力定义首次全历史、后续增量和缺口对账三类采集运行，并以逐查询持久状态和覆盖清单证明“哪些来源、哪些业务问题、哪个时间范围已被尝试以及实际结果是什么”。

## ADDED Requirements

### Requirement: 可审计的采集运行身份与生命周期
每个 `AcquisitionRun` SHALL 具有稳定 run ID，并固定目标公司、股票代码、`baseline|incremental|reconcile` 模式、`as_of`、创建/开始/结束时间、业务问题清单版本、注册表版本与哈希、请求范围、父运行或修复目标、运行状态和覆盖结果。运行 MUST 在首次外部请求前持久化；终态运行只能追加审计注记，不得改写其输入、尝试、覆盖项或输出引用。

#### Scenario: 请求开始前建立运行
- **WHEN** 系统接受一次有效采集请求
- **THEN** 系统 SHALL 先持久化带固定注册表和问题版本的运行，再发起第一个来源查询

#### Scenario: 终态运行被重复提交
- **WHEN** 调用方尝试用同一 run ID 修改 `as_of`、模式或来源版本
- **THEN** 系统 SHALL 拒绝覆盖，并要求创建显式关联的新运行

### Requirement: baseline 起点与全历史分片
`baseline` SHALL 先确定可追溯的公司历史起点：有经核验招股书日期时取招股书日期与上市日中的较早者；否则取上市日；两者均不可得时使用注册表声明且可说明依据的来源最早可得日。每个来源/查询的有效查询起点 SHALL 为公司历史起点与该来源/查询最早可得日中的较晚者；此前不可查询的区间 MUST 作为 `policy_skipped: source_not_available` 覆盖项保留。系统 MUST 将有效起点至 `as_of` 的全部区间和分页纳入计划，不得受现有“五年/十年”默认窗口截断。

#### Scenario: 招股书早于上市日
- **WHEN** 公司有经核验的招股书发布日期且早于上市日
- **THEN** baseline 的公司历史起点 SHALL 使用招股书发布日期，并为各来源按其最早可得边界形成连续覆盖

#### Scenario: 来源只提供部分历史
- **WHEN** 公司历史起点为 2001 年而某 IR 来源最早只提供 2010 年资料
- **THEN** 2001 至 2010 的该来源覆盖 SHALL 明确记录为 `policy_skipped: source_not_available`，2010 至 `as_of` SHALL 被完整查询

### Requirement: 三种运行模式的可观察语义
首次没有可用 checkpoint 的正式分析 MUST 使用 `baseline`；已有安全 checkpoint 的常规更新 MUST 使用 `incremental`；修复失败区间、验证历史修订、处理注册表不兼容变更或审计覆盖缺口 MUST 使用 `reconcile`。系统不得把一次有限窗口查询标记为 baseline 完成，也不得由失败的 incremental 隐式创建新 baseline。

#### Scenario: 首次分析请求 incremental
- **WHEN** 目标公司和来源不存在兼容且安全的 checkpoint
- **THEN** 系统 SHALL 拒绝直接 incremental，并返回需要 baseline 或明确 reconcile 的可诊断结果

#### Scenario: 修复历史缺口
- **WHEN** 先前运行在某时间分片发生限流且其他分片已有成功材料
- **THEN** reconcile SHALL 以该缺口和必要重叠区间为目标，保留与原运行的关联，而不是删除或重写原尝试

### Requirement: 逐查询尝试的完整状态分类
每个可执行物理查询/时间分片或资源抓取 SHALL 在外部 I/O 前持久化 `AcquisitionAttempt`。attempt MUST 记录来源定义版本、`physical_query_plan_item_id`、稳定 `execution_key`、`attempt_kind=discovery|fetch`、查询定义或资源引用、时间范围、分页/游标位置、父 discovery/资源关系、开始时间、请求的非敏感摘要、retry group 和重试序号；问题级覆盖通过 plan item 的 `PhysicalQueryCoverageLink` 解析，不得为共享查询伪造多次 I/O。具有协议结果的 attempt 终态只描述该次 discovery 或 fetch 执行本身，MUST 恰为 `success`、`unchanged`、`no_data`、`restricted`、`paywalled`、`login_required`、`rate_limited`、`timeout`、`network_failed`、`parse_failed`、`policy_skipped` 或 `partial_success` 之一，并记录结束时间、机器可读原因、HTTP/协议摘要、产出/复用证明或快照 ID 和下一安全位置。下载后内容校验、原子归档、哈希复核或 snapshot 元数据提交失败 MUST 记为 `parse_failed`，并使用 `archive_write_failed|integrity_mismatch|snapshot_commit_failed` reason code 区分，不得新增未定义结果状态或误记 success。自由文本只能作为补充说明，不能替代状态。

discovery attempt 完整验证全部分页并返回非空规范资源集合时为 `success`，以有效旧 discovery 锚点证明响应未变化时可为 `unchanged`，完整成功且有终止证明、零资源时才为 `no_data`；已有页 segment 提交后续页失败时为 `partial_success`。fetch attempt 取得 `new|changed` 内容时为 `success`，取得有效 304 或相同哈希时为 `unchanged`；资源 disposition 保存在独立 observation。`PhysicalQuery/CoverageResolution` 才聚合 discovery proof 与所有 required fetch 的当前解决状态，附件失败不得反向改写已终结 discovery attempt。每次协议重试 MUST 新建 attempt 并共享 retry group；进程丢失只给旧 attempt 追加 `abandoned` lifecycle closure，新 attempt 以不可变 `supersedes_attempt_id` 关联旧 attempt。`abandoned` 不属于上述 12 个来源结果终态、不得解析为 `timeout`/失败/no_data，也不得自行完成 coverage。

状态分类 MUST 确定性映射安全与解析异常：首个 discovery page 在 schema/proof 校验前失败为 `parse_failed`，已有成功 page segment 后再发生同类失败则整个 discovery attempt 为 `partial_success`；没有有效本地 snapshot anchor 的 304 fetch 为 `parse_failed`，reason 为 `validator_anchor_missing|invalid_304`；越界重定向、HTTPS 降级、未批准 private target 或响应硬上限触发本地策略停止时，尚无已提交 segment 的 attempt 为 `policy_skipped`，reason 为 `redirect_not_allowlisted|transport_target_forbidden|response_size_exceeded`，已有前序 segment 的 discovery attempt 则为 `partial_success` 并保存具体原因。`restricted` 只表示来源侧访问限制/验证码/挑战等已分类响应。上述映射不得由 adapter 自选另一 outcome。

#### Scenario: 共享物理查询保留问题级覆盖
- **WHEN** 一个 discovery attempt 的 `execution_key` 同时服务四个业务问题覆盖项
- **THEN** 系统 SHALL 只记录一次实际查询及其分页 segment；attempt 引用一个 plan item，四个 coverage entry 通过 PhysicalQueryCoverageLink 关联该 plan item并解析到相同 discovery/后续 fetch attempts

#### Scenario: 请求返回限流
- **WHEN** 来源返回 429 或明确的速率限制信号
- **THEN** 尝试 SHALL 持久化为 `rate_limited`，保留允许的 `Retry-After` 摘要，且不得改写成失败、无数据或未披露

#### Scenario: Retry-After 超过运行 deadline
- **WHEN** 来源给出的 Retry-After 晚于固定 run/attempt deadline 或超过注册表退避上限
- **THEN** 系统 SHALL 终结本次 attempt 为 `rate_limited` 并保留 barrier，不得睡眠到 deadline 之外、无限重试或临时提高请求速率

#### Scenario: 分页中途失败
- **WHEN** 一个查询前两页成功而第三页网络失败
- **THEN** 尝试 SHALL 为 `partial_success`，列出已冻结产物和失败页位置，并保持尚未覆盖的后续区间可见

#### Scenario: 一次查询包含混合资源结果
- **WHEN** 一个查询完整返回一份新增资源、一份变化资源和三份未变化资源
- **THEN** discovery attempt SHALL 为 `success`，各 fetch attempt/observation SHALL 分别保留 `new|changed|unchanged`、snapshot 和 validator，聚合 PhysicalQuery/CoverageResolution SHALL 在全部 required fetch 解决后为完整

#### Scenario: discovery 成功但必需抓取未完成
- **WHEN** discovery 分页完整并发现三个 required attachment，其中两个成功冻结而第三个 fetch 为 `restricted`
- **THEN** discovery attempt SHALL 保持自身 `success`，第三个 fetch SHALL 保留独立 `restricted` 终态，PhysicalQuery/CoverageResolution SHALL 为 partial/blocked 并显示一个材料缺口；不得改写 discovery attempt，也不得由另一个来源镜像成功覆盖本来源状态

#### Scenario: 归档完整性失败
- **WHEN** 响应体已下载但发布后完整哈希复核失败
- **THEN** attempt SHALL 为 `parse_failed` 且 reason code 为 `integrity_mismatch`，不得生成可消费 snapshot 或推进 checkpoint

#### Scenario: 崩溃遗留 started attempt
- **WHEN** 执行进程在 attempt 已 started 但尚未取得可分类协议结果时退出
- **THEN** 租约过期后的恢复者 SHALL 追加 `abandoned` 生命周期事件并创建同 retry group 的新 attempt；旧记录不得被改写为 12 个结果状态之一，也不得计为覆盖完成

### Requirement: no_data 的严格语义边界
`no_data` MUST 只用于来源成功执行了完整、合法且可解析的 discovery 查询，分页终止条件得到证明，并以允许保留的原始 discovery snapshot 或响应哈希/长度/schema/总数/分页摘要确认结果集合为空。HTTP 错误、缺少必需 schema/总数、空白挑战页、登录/付费/许可限制、限流、超时、网络错误、解析错误、提前截断或未知响应不得转换为 `no_data`，也不得由 `no_data` 自动生成事实层的“未披露”或“暂无该数据”。

#### Scenario: 成功空结果
- **WHEN** 来源以成功协议响应完成全部分页且可验证结果条数为零
- **THEN** 尝试 SHALL 记录 `no_data`，关联可复核的 discovery 证明，覆盖项可标记该查询已完成，但事实层不自动生成“未披露”

#### Scenario: 验证页看似空列表
- **WHEN** 返回内容是验证码/挑战页或无法按声明 schema 解析的空页面
- **THEN** 尝试 SHALL 记录 `restricted` 或 `parse_failed`，不得记录 `no_data`

### Requirement: 来源问题时间覆盖清单
每个运行 MUST 生成不可变覆盖清单；每个条目 SHALL 以来源定义版本、业务问题 ID、查询 ID 和时间范围唯一定位，并通过显式多对多 link 列出所有实际贡献的 discovery/fetch attempt ID、最终覆盖状态、缺口/重叠、发现证明、快照引用与跳过理由。共享一次物理查询不得合并或省略任何问题级条目。“完整”仅表示所有计划组合与时间区间均存在明确终态且 `coverage_accounted=true`，不得暗示所有材料均已取得或业务问题已有答案。

#### Scenario: 全部组合都有终态但部分受限
- **WHEN** 所有来源 × 问题 × 时间分片都有终态，其中一个来源为 `restricted`
- **THEN** 运行 SHALL 标记 `coverage_accounted=true`，同时 MUST 标记材料结果为有缺口并列出受限条目，不得声称全部资料取得

#### Scenario: 存在未终结尝试
- **WHEN** 任一计划条目没有终态 attempt 或静态 `policy_skipped` 理由
- **THEN** 运行 SHALL 保持覆盖不完整，且不得成为可供下游消费的完成批次

#### Scenario: 静态不适用区间不发起 I/O
- **WHEN** 来源在公司历史起点之后才存在，或来源市场与公司不匹配
- **THEN** 覆盖清单 SHALL 直接记录带 `source_not_available|market_not_applicable` 的静态 policy disposition，不创建 AcquisitionAttempt，并将该区间排除出 required checkpoint partition

### Requirement: 尝试即时持久化与可恢复性
尝试状态、分页 segment、发现证明和已完成快照引用 MUST 在每个分页或边界结果产生后持久化，而不是仅在整个同步结束时汇总。进程异常中断时，已启动记录 SHALL 保持可审计；恢复流程 MUST 在原执行租约过期后从最后提交的安全位置接管，把没有协议结果的未终结 attempt 追加标记为 `abandoned` 并建立新 retry attempt。只有实际超过已记录请求 deadline 的 I/O 才可终结为 `timeout`；任何恢复均不得删除已冻结产物、改写旧 attempt 或跨越未证明完成的位置。

#### Scenario: 进程在第三页崩溃
- **WHEN** 前两页已持久化而采集进程在第三页退出
- **THEN** 原租约过期后系统 SHALL 找回 run、旧 attempt、前两页发现证明/快照和第三页安全位置，追加 abandoned 事件并用新 attempt 从该位置恢复，且 checkpoint 不得越过该位置

### Requirement: 单运行持久执行租约
所有会发起外部 I/O 的 production、smoke 或 ad-hoc run 执行 MUST 通过持久租约协调；纯 plan/list/show 不需要租约。租约至少固定 run ID、不可猜测 owner token、单调 lease epoch、取得/过期时间和 heartbeat。任一时刻同一 run 只能有一个未过期执行者；取得、续租、释放和过期接管 MUST 原子完成并追加对应运行审计事件。租约是可更新的运行控制记录，不得替代 append-only run/attempt 事实；执行器失联后只有在租约明确过期时才可接管。

#### Scenario: 两个执行器同时领取同一运行
- **WHEN** 两个 API/CLI 执行器并发尝试领取同一 run
- **THEN** 系统 SHALL 只允许一个原子取得新 lease epoch，另一个得到明确 conflict 且不得发起来源 I/O

#### Scenario: 执行器失联后接管
- **WHEN** 持有者停止 heartbeat 且租约到期，但 run 尚未终结
- **THEN** 新执行器 SHALL 以更高 lease epoch 接管、追加审计事件并从已提交安全位置恢复；旧 owner 后续提交 MUST 因 epoch 不匹配被拒绝

### Requirement: 采集结果不产生定性研究结论
运行、尝试和覆盖清单 SHALL 只描述采集行为、材料可得性和证据状态，不得在本能力内生成业务文本抽取结果、护城河/定价权/战略可信度判断、投资评级或估值结论。失败、受限或 no_data 也不得被解释为公司没有披露或相关事项不存在。

#### Scenario: 所有查询均 no_data
- **WHEN** 一个业务问题的所有适用查询都以合法 `no_data` 完成
- **THEN** 输出 SHALL 仅陈述本次查询未返回材料，不得自动形成“公司未披露”或任何业务判断
