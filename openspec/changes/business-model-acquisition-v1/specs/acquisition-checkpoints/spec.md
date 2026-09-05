## Purpose

本能力用来源级安全水位线、查询分区位置和重叠回看窗口驱动增量同步，确保新增、修订、未变化及乱序资料可被识别，同时任何失败都不会造成永久跳数。

## ADDED Requirements

### Requirement: 正文 validator 兼容与 checkpoint 兼容分离
新定义 SHALL 以可选 `content_validator_compatible_from_versions` 显式授权同来源旧版正文的条件复用，未声明时只查当前版本，历史 registry canonical hash MUST 不变。复用 MUST 校验相同来源/上游、正文 canonical 与时间语义、确切资源 URL、同 namespace、原始与当前许可、完整字节哈希及未隔离状态；validator 只来自成功观测。兼容旧正文不代表兼容旧 checkpoint，亦不得将 ad_hoc 改成 production。

#### Scenario: 新生产查询复用 ad_hoc 取得的正文
- **WHEN** 全新 production discovery 再次发现相同正文，合格旧快照的 Last-Modified 条件请求获得 304
- **THEN** 系统 SHALL 重新检查锚点并创建当前来源版本的新 attempt/observation，引用旧快照及其版本；原快照、旧 run 与 creating observation 不变，生产覆盖依据本次 HTTP discovery 与 fetch 证明

#### Scenario: 旧正文不具备兼容条件
- **WHEN** 版本未声明、canonical/URL/时间语义冲突、许可禁止、字节缺失或锚点被隔离
- **THEN** 系统 MUST 不发送该锚点的条件头；无有效条件请求的 304 MUST 保留失败屏障，不能制造 unchanged

#### Scenario: 条件请求返回完整正文
- **WHEN** 合格旧版正文的条件请求返回 200
- **THEN** 系统 SHALL 记录实际正文 SHA-256 和长度；同哈希复用旧 snapshot 并追加当前版本 unchanged observation，新哈希创建当前来源版本 snapshot 和 changed observation，以 validator snapshot/version 记录跨版本前驱。各来源定义内部版本链独立保留；替换字节的 available_at MUST 使用 retrieved_at，同时保留原发布日期及其精度，不能取得旧正文的历史可用时间。

### Requirement: 主采安全水位线与补充来源隔离
incremental 的安全 checkpoint 前置条件 SHALL 应用于已启用适用主采来源，按需来源不属于默认必需分区。按需补缺和已有目录归档 SHALL 使用 ad_hoc，不能推进 production checkpoint，也不能清除另一来源的 barrier。新来源版本没有显式声明的兼容关系时 MUST 保持需 baseline 或 reconcile 的前置条件。

#### Scenario: 巨潮有安全水位线但上交所未全采
- **WHEN** 唯一适用主采巨潮已有当前合同兼容的安全 checkpoint，上交所配置为 on_demand
- **THEN** 默认 incremental SHALL 允许从主采水位线规划；若巨潮没有安全 checkpoint 则 MUST 在创建 run 或 I/O 前拒绝

#### Scenario: 补充材料成功不更改主采屏障
- **WHEN** 上交所 ad_hoc 补缺取得一份定期报告
- **THEN** 新报告 SHALL 保留上交所来源身份，巨潮原运行、覆盖和未解决 barrier MUST 保持不变

### Requirement: 来源级 checkpoint 的可追溯结构
系统 SHALL 为公司、来源定义 ID/版本和问题清单版本维护版本化 `SourceCheckpoint`。checkpoint partition SHALL 按物理 query definition/`execution_key`、来源分区和时间范围建立，不得因一个物理查询映射多个业务问题而复制或重复推进。checkpoint MUST 包含安全水位线、各物理查询分区的连续完成位置/游标、重叠回看配置、最近成功运行、已知 validator 及其快照锚点、阻塞位置及 checkpoint 版本；时间型安全位置 MUST 使用来源可证明的时间上界与 canonical resource ID 构成稳定全序，避免同时间戳资源被跳过。来源级安全水位线 MUST 是所有必需查询分区可证明连续完成位置的保守下界。

#### Scenario: 多查询分区进度不同
- **WHEN** 同一来源的公告查询完成至 2026-09-01，但定期报告查询只完成至 2026-08-20
- **THEN** 来源级安全水位线 SHALL 不晚于 2026-08-20，并分别保留两个查询分区位置

#### Scenario: 一个物理查询映射多个业务问题
- **WHEN** 同一 `execution_key` 的定期报告查询同时服务三个业务问题
- **THEN** checkpoint SHALL 只维护一个物理查询分区和一次连续位置推进，三个 coverage entry 通过关联共享该进度而不得各自产生可漂移水位线

### Requirement: incremental 重叠回看
`incremental` MUST 从每个查询分区安全位置减去来源定义固定的重叠回看窗口后开始，并查询至运行 `as_of`。窗口大小和计算方式 SHALL 随运行固定的来源定义版本保存，调用方不得通过缩短窗口绕开修订检查。有限重叠窗口不代表全部历史会被持续复核；每个可覆盖旧 URL/资源的来源定义 MUST 另行声明周期性历史 reconcile/资源复核策略或明确无法保证发现窗口外静默替换的限制。

#### Scenario: 按七天窗口重复查询
- **WHEN** 分区安全水位线为 9 月 1 日且定义的回看窗口为七天
- **THEN** 9 月 2 日的 incremental SHALL 至少重新检查 8 月 25 日至 9 月 2 日的适用范围，并将重复命中判为变化或未变化

#### Scenario: 回看窗口之外的历史资源复核
- **WHEN** 来源允许同一历史 URL 被覆盖且七天 overlap 无法发现更早的静默替换
- **THEN** 系统 SHALL 按固定注册表版本生成周期性 reconcile/资源复核范围，或在覆盖结果中保留无法持续检测该类变化的明确限制，不得宣称 incremental 已检查全部历史版本

### Requirement: 新增、变化和未变化判定
系统 MUST 优先使用来源声明的 canonical resource ID 识别同一逻辑资源，使用由同一兼容来源/canonical 或 URL 的已提交、未隔离且完整性有效快照产生的 ETag 与 Last-Modified 发起合规条件请求，并以完整内容 SHA-256 作为版本判定的最终依据。新 canonical ID 为新增；同一 canonical ID/URL 返回新哈希为变化并创建新版本；304 只有在能引用上述 validator 快照锚点时才可判为 `unchanged`，重新取得完整内容时则以相同哈希判为 `unchanged`。validator 与哈希冲突时 MUST 以实际内容哈希为准并记录异常。validator 来自旧自由文本、已隔离快照、不兼容定义或本地没有对应字节时，系统不得由 304 制造快照或 unchanged；应记录机器可读异常并按固定策略建立一次无条件 fetch attempt 或形成 barrier。

#### Scenario: 条件请求返回 304
- **WHEN** 来源对已知 ETag 返回 304 Not Modified
- **THEN** 尝试 SHALL 校验 ETag 所绑定的现有快照仍可用后标记 `unchanged`、引用该资源快照，并且不得创建伪造的新文档版本

#### Scenario: 304 没有有效快照锚点
- **WHEN** 条件响应为 304，但 validator 没有对应已提交快照、对应快照已隔离或 canonical 规则不兼容
- **THEN** fetch attempt SHALL 终结为 `parse_failed: validator_anchor_missing|invalid_304`，不得创建 snapshot/unchanged；只有新的无条件 fetch 成功并追加 BarrierResolution 或 reconcile 明确解决后才可解除 barrier

#### Scenario: URL 和 ETag 未变但内容变化
- **WHEN** 同一 URL 返回与旧快照不同的完整 SHA-256，即使 ETag 未变
- **THEN** 系统 SHALL 记录 validator 异常、创建新资源/文档版本并保留旧版本

#### Scenario: ETag 变化但内容相同
- **WHEN** ETag 或 Last-Modified 变化而完整内容哈希仍相同
- **THEN** 尝试 SHALL 标记 `unchanged`，保留本次观测与新 validator，但不得复制同内容证据版本

### Requirement: 失败屏障与原子水位线推进
checkpoint 仅可在对应查询区间的 discovery attempt、发现证明、全部 required fetch attempt、资源观测和允许的快照元数据已持久化后推进。注册表声明为 `metadata_only` 的查询可仅凭完整发现证明完成；其他查询中任何必需附件未取得均不得由列表成功替代。`restricted`、`paywalled`、`login_required`、`rate_limited`、`timeout`、`network_failed`、`parse_failed`、运行时 `policy_skipped`、`partial_success` 或 `abandoned` SHALL 为精确 work position/retry group 建立 append-only barrier；系统只按当前未解决 barrier 计算安全位置，不得因历史失败记录仍存在而永久阻塞已被合法后继 attempt 修复的位置，也不得删除或改写旧失败 attempt。系统不得越过最早未解决 barrier 推进该分区或来源级水位线。`success`、`unchanged` 和合法 `no_data` 才可作为 barrier resolution 的证明。市场不适用和来源尚不存在等在计划期确定的静态 policy disposition 不建立 attempt、不属于 required partition，因此不阻塞其后的有效查询区间。

#### Scenario: 中间日期限流但后续页成功
- **WHEN** 8 月 20 日位置发生限流，而系统已取得 8 月 21 日之后的若干结果
- **THEN** 已成功冻结的快照 SHALL 保留，但查询分区和来源级水位线 MUST 停在 8 月 20 日之前，后续运行 SHALL 再覆盖该阻塞位置

#### Scenario: 快照提交失败
- **WHEN** 内容已下载但资源快照元数据或哈希复核未成功提交
- **THEN** checkpoint SHALL 保持原版本，且该内容不得被视为已完成覆盖

#### Scenario: 列表成功但必需附件受限
- **WHEN** 一页 discovery 成功发现 A、B、C 三个必需附件，A/B 已冻结而 C 返回 403
- **THEN** A/B 快照和 discovery success SHALL 保留，C fetch 为 `restricted`，PhysicalQuery/CoverageResolution SHALL 为 partial/blocked，barrier SHALL 停在 C 所属发现位置之前；另一来源取得 C 的镜像也不得清除本来源 barrier

#### Scenario: 来源尚不存在的早期区间
- **WHEN** 公司历史起点为 2001 年而来源最早可得日为 2010 年
- **THEN** 2001 至 2010 年 SHALL 作为静态 `source_not_available` disposition 排除出 required partition，2010 年后的完整成功区间 SHALL 能正常推进 checkpoint

#### Scenario: 运行时许可状态变为不明
- **WHEN** 一个原本 enabled 的适用查询在执行前发现当前许可状态无法确认
- **THEN** 系统 SHALL 创建 `policy_skipped` attempt 与 runtime policy reason，该位置 MUST 形成 barrier，直到新 registry 版本或 reconcile 明确处理

#### Scenario: 来源挑战后的熔断跳过仍阻塞 checkpoint
- **WHEN** 一个来源 attempt 以 `restricted: upstream_bot_challenge` 打开 barrier，且当前 run 中该来源的后续工作均以 `policy_skipped: source_access_halted` 无 I/O 终结
- **THEN** 每个跳过位置 SHALL 保留独立 barrier 和 opening challenge 因果引用，来源级安全水位线不得越过其中最早位置；运行可完成 coverage accounting，但不得获得默认消费资格或把熔断当作静态不适用

#### Scenario: 前置查询不可用后的依赖跳过仍阻塞 checkpoint
- **WHEN** prerequisite bootstrap 以 504 timeout 终结，所有依赖计划以 `policy_skipped: dependency_unavailable` 无 I/O 终结
- **THEN** prerequisite 与 downstream barriers SHALL 保留共同 causal group 和各自精确 work position，checkpoint 不得因下游没有实际发出请求而越过这些位置

### Requirement: 屏障只能由同一工作位置的后继证据解决
每个 barrier MUST 固定来源定义版本、物理查询分区、时间/页/游标位置、可选 canonical resource/required fetch work、retry group 和 opening attempt。只有显式后继 attempt 精确覆盖同一工作位置、持有兼容来源/查询语义，并提交该位置要求的完整 discovery proof 或有效 snapshot/observation 后，系统才可追加不可变 `BarrierResolution`；原失败/受限/abandoned attempt 必须保留。其他来源镜像、不同时间片、较宽但无法证明包含该位置的查询或仅有自由文本成功均不得解除 barrier。

#### Scenario: 限流后同位置重试成功
- **WHEN** 某 work position 首次 attempt 为 429，后继 attempt 在同一 retry group 完整取得该位置 discovery proof
- **THEN** 系统 SHALL 保留原 `rate_limited` attempt并追加 BarrierResolution，随后可按连续性重新计算 checkpoint；不得覆盖原状态或因历史 429 永久阻塞

#### Scenario: 受限附件后续取得
- **WHEN** required attachment C 首次 fetch 为 403，后继 fetch 对同来源/canonical/work position 成功冻结有效 snapshot
- **THEN** 系统 SHALL 保留 403 attempt、追加该 fetch barrier 的 resolution，并重新计算对应 coverage；其他来源的同内容镜像不能代替此 resolution

#### Scenario: 无效 304 后无条件重取
- **WHEN** 缺少有效 snapshot anchor 的 304 先建立 `parse_failed` barrier，后继无条件 fetch 对同位置返回 200 且快照提交成功
- **THEN** 系统 SHALL 追加 BarrierResolution 并使用 200 的快照；旧 invalid-304 attempt 仍可审计且不得被改成 unchanged

### Requirement: reconcile 使用单一确定性目标选择
所有 production API、CLI 与内部运行时 SHALL 使用同一个权威 reconcile target selector，不得各自从父 run 最早 coverage 起点推导范围。selector 只接受已有不可变 final event 的父 run；未 finalized、仍持有活跃租约或缺少可解释 coverage graph 的 run MUST 在创建子 run 和外部 I/O 前被拒绝，不能被隐式恢复、补写 final event 或当作合法 reconcile 父项。finalized 的 partial run 可以作为父项，但其历史 run、attempt、coverage、snapshot 和 checkpoint 不得被修改。

selector MUST 按下列优先级返回一个最早且可精确定位的目标：第一，未解决 barrier 对应的 source definition、query partition 和 work position；第二，没有 opening barrier 的未解决 required coverage；第三，最新完整性状态为 quarantined 的 snapshot 所属 coverage；第四，在前三类均不存在时用于周期性复核的最早已完成 required slice。每类内部 SHALL 先按父计划的时间片/ordinal 和规范 work-position 顺序排序，再以来源定义 ID/版本、partition key、coverage/barrier ID 作稳定 tie-break，不得使用数据库偶然返回顺序或仅按 coverage 全局最小 `time_start`。

返回目标 MUST 至少包含 strategy、resolved parent run ID、source definition ID/版本、query/partition、plan/coverage ID、父时间片、精确 work position，以及适用时的 barrier ID、opening attempt、retry group、canonical resource 或 quarantined snapshot；同时 MUST 回显按固定 registry overlap 计算并受 query 最早可得日、父时间片与新 run `as_of` 约束的 effective reconcile range。reconcile planner SHALL 只安排该目标、为重放该位置必需的 overlap 和 prerequisite 工作；不得因为父 run 更早存在其他已完成 coverage 而退回全历史重跑。

#### Scenario: 最早 coverage 早于最早未解决 barrier
- **WHEN** 父 run 的最早 coverage 从 2001 年开始，但最早未解决 barrier 位于 2024 年某查询分区的第三页
- **THEN** selector SHALL 返回该 barrier ID、query partition、第三页 work position 和相应 effective overlap range；CLI/API 创建的 reconcile 不得把 2001 年作为默认 start 或重跑无关来源/分区

#### Scenario: 未解决 coverage 没有 opening barrier
- **WHEN** finalized 父 run 存在一个 required coverage 尚无 resolution，但没有可关联的 opening barrier
- **THEN** selector SHALL 使用第二优先级返回该 coverage 的精确 source/query/time slice，并明确 strategy 为 unresolved coverage，而不是跳到完整性异常或周期性复核

#### Scenario: 只有隔离快照需要修复
- **WHEN** 父 run 没有未解决 barrier/coverage，但某已完成 coverage 引用的 snapshot 最新完整性事件为 quarantined
- **THEN** selector SHALL 返回该 snapshot 及其 coverage/time slice 作为第三优先级目标，并保留原 snapshot 与父 run 不变

#### Scenario: 未 finalized 父运行被拒绝
- **WHEN** 调用方把一个没有 final event 的人工中止 baseline 指定为 reconcile 父运行
- **THEN** 系统 SHALL 在创建 reconcile run、attempt 或发起 I/O 前拒绝，并提示先使用新的运行取得可审计终态；不得修改或继续该旧运行

### Requirement: 乱序、迟到与修订资料处理
在重叠窗口或 reconcile 中发现发布时间早于当前水位线的新 canonical ID、晚到更正或新内容哈希时，系统 SHALL 保存为新增/变化版本并关联原始资源。迟到资料本身不得让水位线倒退；若它证明既有区间覆盖不完整，系统 MUST 建立待 reconcile 缺口并在修复前阻止相应消费资格。

#### Scenario: 水位线之前出现迟到公告
- **WHEN** incremental 在回看窗口发现一份发布时间早于水位线但此前未见的公告
- **THEN** 系统 SHALL 归档其快照、记录迟到原因并重新评估相关覆盖区间，而不是丢弃为重复

#### Scenario: 历史公告被更正
- **WHEN** reconcile 发现同一 canonical 资源有新哈希且标记为更正版本
- **THEN** 新旧版本 SHALL 通过版本链关联，旧报告和旧证据快照保持不变

### Requirement: registry 版本变化与 checkpoint 兼容
checkpoint 不得自动跨越不兼容的来源定义或查询清单版本。只有新定义显式声明 checkpoint 兼容且系统验证查询边界、canonical 规则和时间语义未改变时，才可创建带迁移依据的新 checkpoint 版本；否则 MUST 要求 baseline 或 reconcile。

#### Scenario: 查询分页语义改变
- **WHEN** 来源新版本改变游标或日期边界语义且未声明兼容
- **THEN** incremental SHALL 拒绝复用旧 checkpoint，并提示执行 reconcile 或新的 baseline

### Requirement: 并发推进不得丢失覆盖
同一公司与来源 checkpoint 的推进 MUST 同时使用父版本比较和当前执行租约 epoch fencing 或等价并发保护。并发运行基于同一旧 checkpoint 完成时，只有持有有效 lease epoch 的一个执行者可提交下一版本；失败或已失去租约的提交者 SHALL 重新评估已提交位置及自身覆盖，不能覆盖较新的 checkpoint、提交迟到终态或静默跳过区间。

#### Scenario: 两个 incremental 同时完成
- **WHEN** 两个运行都基于 checkpoint 版本 7 尝试推进
- **THEN** 系统 SHALL 只允许一个提交版本 8，另一个必须重新对账且不得覆盖版本 8
