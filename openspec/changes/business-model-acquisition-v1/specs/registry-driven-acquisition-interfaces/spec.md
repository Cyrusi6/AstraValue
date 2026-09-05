## Purpose

本能力把来源注册表、采集运行和结构化状态贯通到 AdapterManager、FastAPI、CLI、在线 smoke 与旧同步兼容层，使用户和下游能启动、观察、复核采集而不依赖硬编码来源或自由文本结果。

## ADDED Requirements

### Requirement: 正式采集统一直连并冻结路由约定
正式 runtime 和 registry-bound transport 自建的 HTTP 客户端 MUST 使用直连、禁止继承环境与 Windows 系统代理且保留 TLS 验证。API、CLI、smoke 新计划 MUST 在 run 中冻结 `http_route_policy=direct-v1`，不得记录代理凭据。恢复执行 MUST 校验持久化约定，未完成旧运行缺失约定时 MUST 在租约、DNS、source gate 和 HTTP 前拒绝静默切换；旧 finalized 结果和离线重放仍可读取。

#### Scenario: 系统代理不影响新运行
- **WHEN** Windows 系统代理或 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY 环境配置存在且创建正式采集客户端
- **THEN** 对所有已批准来源的有效客户端路由 SHALL 仍为直连，环境变化不触发自动出口切换

#### Scenario: 路由未知的旧运行保持证据原样
- **WHEN** 未完成持久化 run 缺少路由约定且操作员要求恢复执行
- **THEN** 系统 MUST 给出明确的旧运行路由未知错误并要求新运行，零外部 I/O 且不改写旧 run

#### Scenario: 来源受限不结束独立修复工作
- **WHEN** 某来源返回明确限制且其他已批准来源仍有可执行工作
- **THEN** 系统 SHALL 保留并报告限制事实，继续其他未受影响工作；自动受限请求不被密集重试，无法取得目标正文时交由操作者讨论下一步，不将失败改写为无数据

### Requirement: AdapterManager 由注册表解析执行计划
`AdapterManager` SHALL 从运行固定的注册表版本解析 adapter、来源定义和查询计划，而不是维护独立硬编码来源集合或按任意字符串构造 adapter。对于 `business_model` v1，baseline/incremental/reconcile 的执行时间与问题范围 MUST 只由 mode、company anchor、checkpoint、registry/question 版本和 as_of 决定；调用方的时间/问题筛选只用于只读展示。显式 ad-hoc 子集运行 MUST 标记 `run_kind=ad_hoc`，不得推进 production checkpoint、不得成为默认可消费批次，也不得声称完整。

#### Scenario: 注册表增加新批准版本
- **WHEN** 一个合法来源通过新注册表版本启用且具有已安装 adapter
- **THEN** Manager SHALL 无需修改独立 provider 常量即可在新运行中解析它

#### Scenario: 请求包含未知 provider 字符串
- **WHEN** 兼容请求传入无法解析到已批准来源 alias 的字符串
- **THEN** 系统 SHALL 拒绝其进入执行计划或创建待审核 candidate，且不得临时实例化 adapter 或正式证据

#### Scenario: 调用方试图收窄 baseline
- **WHEN** 调用方在 baseline 执行请求中只选择近期年份或一个问题
- **THEN** 系统 SHALL 拒绝该执行范围或创建不可推进 checkpoint 的 ad-hoc run，且不得把它标记为 baseline 完整

### Requirement: 采集 API 暴露结构化运行与审计结果
本地 API MUST 支持以 ticker、模式、`as_of` 和问题清单版本启动采集，并支持读取/筛选运行、逐查询 attempts、资源 observations、覆盖清单、来源 checkpoints、资源快照及其 integrity events 元数据和 source candidates。启动响应 SHALL 返回 run ID；执行端点 MUST 通过持久租约保证单执行器，并以明确的 404 not found、409 active lease/source review conflict、422 validation、503 storage busy 和 500 integrity/internal 类别响应，不能把所有存储异常压成 404。所有状态字段 MUST 使用规范枚举，且原始内容、凭据、owner token 和本地绝对敏感路径不得通过 API 泄露。

#### Scenario: 启动 baseline 并查看覆盖
- **WHEN** 客户端为 `600519` 提交 `mode=baseline`
- **THEN** API SHALL 返回可持久查询的 run ID，并允许按该 ID 取得固定注册表版本、attempts、覆盖状态、checkpoint 结果和快照引用

#### Scenario: 查询不存在的运行
- **WHEN** 客户端读取未知 run ID
- **THEN** API SHALL 返回明确的 404，而不是空覆盖或伪造的 no_data

#### Scenario: 活跃执行租约冲突
- **WHEN** 一个 run 已有未过期租约且另一 API/CLI 调用请求执行
- **THEN** 执行端点 SHALL 在任何额外来源 I/O 前返回 409 和不含 owner token 的租约到期摘要；租约过期后的调用 SHALL 能按更高 epoch 接管而不是永久冲突

#### Scenario: SQLite 暂时繁忙
- **WHEN** API 在有限 busy timeout 内无法取得运行控制或 finalize 所需写锁
- **THEN** 系统 SHALL 返回可重试的 503/storage_busy 且不把该状态伪装成 unknown run、no_data 或来源网络失败

### Requirement: 单一运行时绑定数据库与数据根
每个 API 服务进程或 CLI invocation MUST 只构建一个注入式 composition root，统一绑定数据库、`data_root`、storage namespace/layout version、固定注册表加载器、adapter factory、snapshot store 和 orchestrator；不同进程不共享 Python 对象，但指向同一证据库时 MUST 通过相同 namespace/SQLite 协调。组件不得自行构造独立默认 Manager、使用模块级 raw 根或按请求接受任意写入路径。数据库与数据根 MUST 保存相互匹配且不包含绝对路径的 namespace 标识；已有 run 再次执行时必须解析到相同 namespace。namespace 不匹配 MUST 在创建 attempt、外部 I/O 或文件写入前失败关闭。blob、derived、manifest、backup、quarantine 等证据/数据路径只可保存为绑定根内的相对标识；首次绑定所需的 DB 邻接 sidecar 是唯一控制面例外，其内容不得包含绝对路径、不得被当作证据或通过 API 返回。API 不得返回任何本机绝对路径。

同一工作区内不同进程和不同 evidence namespace 对同一来源/host 发起网络请求时 MUST 经过共同的跨进程来源门禁。v1 来源定义 MUST 将最大来源并发限制为 1，并按固定最小请求间隔串行放行；进程崩溃后下一执行者也必须等待完整间隔再请求。无法在 deadline 内取得门禁时 MUST 记录 `rate_limited: local_source_gate_timeout` 并形成 barrier，不能由各进程独立的内存 limiter 绕过。

#### Scenario: API 注入隔离运行时
- **WHEN** 测试应用绑定临时数据库和临时 data root 后执行 acquisition 与已批准手工文档入口
- **THEN** 只有该临时 namespace 中的数据库/blob/derived/manifest 发生变化，项目默认数据库与默认 raw 目录哈希 SHALL 保持不变

#### Scenario: 已有数据库绑定错误数据根
- **WHEN** 调用方以不同 storage namespace/data root 尝试执行数据库中的已有 run
- **THEN** 系统 SHALL 在状态写入和联网前返回 namespace mismatch，且不得创建 attempt、租约或正式文件

#### Scenario: API 与 CLI 使用同一存储绑定
- **WHEN** API 和 CLI 使用同一 database/data-root namespace
- **THEN** 各进程 SHALL 通过各自唯一 composition root 读取同一 run、checkpoint 和 snapshot；使用不同 namespace 的实例 SHALL 完全隔离且不得交叉解析相对路径

#### Scenario: 两个独立进程请求同一来源
- **WHEN** 两个不同 DB/data-root runtime 进程同时准备请求同一 v1 来源/host
- **THEN** 工作区跨进程来源门禁 SHALL 只放行一个请求，另一个等待至少固定间隔或在 deadline 内失败为 `rate_limited: local_source_gate_timeout`；不得出现重叠请求

### Requirement: 存储 namespace 首次绑定可恢复
正常采集 runtime 构造前 MUST 由不会自动迁移数据库的引导流程完成只读 preflight，并按稳定顺序同时取得基于 database identity 与 data-root identity 的跨进程锁；相同 DB/不同 root 或相同 root/不同 DB 均 MUST 互斥，不能只锁组合 pair。首次绑定 SHALL 先在 DB 邻接位置原子发布持久 `StorageBindingIntent` sidecar，保存 namespace ID、binding nonce、layout version、数据库/根的不可逆 identity hash、初始源数据库指纹/版本、当前 bootstrap stage 及已验证 backup/migration manifest 哈希，但不得保存绝对路径；再在目标 data root 原子发布 matching `pending` marker。若在二者之间崩溃，只有 identity hash 匹配的原 root 可用同 nonce 补齐并恢复，其他 root MUST 失败关闭。DB-side intent SHALL 是绑定完成前的权威 journal，root marker 镜像身份并确认阶段；完成所需备份/迁移后，系统 SHALL 在 v6 数据库事务中写入 matching bound namespace row，再把 root marker 原子发布为 `bound`，最后标记完成或安全退役 sidecar。v4 路径 MUST 按 `preflight -> backup_v4_verified -> migrated_v5 -> backup_v5_verified -> committed_v6 -> marker_bound` 推进，每个文件阶段原子替换、每个数据库 migration 使用独立事务；若崩溃发生在 migration commit、intent 更新或 root 确认之间，只有同 nonce/身份匹配且数据库恰为 journal 允许的本阶段或唯一下一版本、migration/schema/payload 与备份证明均有效时才可前滚，不得重跑 0005、跳过 v5 recovery point 或接受其他中间状态。单边缺失、指向其他 root 的 pending intent、非预期中间状态或 ID/nonce/fingerprint 冲突 MUST 失败关闭并要求显式 repair。`acquisition-db backup` 可使用只读 preflight 与显式目标写备份，但不得创建 binding intent、构造正常 runtime 或触发迁移；成功完成 bound pairing 后才可构造正常 runtime、创建 run 或联网。

#### Scenario: 写 matching intent 和 pending marker 后数据库提交前崩溃
- **WHEN** DB 邻接 sidecar 与 data root 已有 matching intent/pending marker，而数据库仍为原 fresh/v4/v5 状态且没有 namespace row
- **THEN** 下一次引导 SHALL 在验证双方 nonce、identity hash、数据库指纹和已有备份后恢复同一绑定流程，不生成新 namespace 或把该状态当永久 mismatch

#### Scenario: DB-side intent 创建后另一 data root 尝试抢绑
- **WHEN** root A 的持久 binding intent 已创建而进程在 root marker 或 namespace row 完成前崩溃，调用方随后以同一数据库和 root B 引导
- **THEN** root B SHALL 因 identity hash 不匹配在任何 marker/数据库写入前失败；只有 root A 可用原 nonce 补齐或恢复绑定，不得把崩溃后的数据库自动绑定到新空 root

#### Scenario: 数据库 namespace 提交后 marker finalize 前崩溃
- **WHEN** v6 数据库已有 matching bound namespace row，而 DB-side intent 和 data root 仍为同 nonce 的 pending 状态
- **THEN** 下一次引导 SHALL 验证三方后只把 root marker 原子 finalize 为 bound 并完成/退役 intent，不重复迁移、备份或创建 namespace row

#### Scenario: v4 迁移链在中间阶段崩溃
- **WHEN** 同 nonce pending journal 已有经验证 v4 备份，而数据库恰在 0005 提交后或 v5 recovery point 写入后崩溃
- **THEN** 下一次引导 SHALL 依据 migration row、schema、旧 payload 与备份 manifest 把 journal 恢复到唯一合法阶段，确保 0005 不重复且 v5 recovery point 不跳过，再继续 0006；任何不符合唯一下一状态的数据库必须失败关闭

#### Scenario: 单边 marker 丢失或身份冲突
- **WHEN** 数据库已有 bound namespace 但 data root marker 缺失，root 已 bound 但数据库无匹配 row，pending intent 指向另一 root，或任意参与方 ID/nonce/fingerprint 不一致
- **THEN** 系统 SHALL 在任何正常状态写入/联网前失败关闭并要求显式 repair，不得把任意空 root 自动重新绑定

### Requirement: 正式 CLI 支持运行与审计
项目 CLI SHALL 提供采集 start/list/show/execute 与 `smoke-sources` 命令，并允许显式选择 `baseline`、`incremental` 或 `reconcile`、ticker、`as_of` 和只读筛选。reconcile MUST 接受明确 `--from-run` 或确定性解析并回显父运行的 `--from-latest-run`。所有会写数据的采集与 smoke 命令 MUST 同时要求显式 `--db` 与 `--data-root`；缺少任一参数必须在创建 run 或联网前以参数错误退出。CLI MUST 输出结构化 run ID、终态、coverage_accounted、缺口数、checkpoint 是否推进及规范状态汇总，并固定使用：0=运行成功且默认可消费，2=参数/注册表/namespace 错误，3=运行终结但有材料缺口或不可默认消费，4=integrity/internal 失败，5=可重试的 active lease conflict 或 storage busy。JSON MUST 保留 code 5 的细分类别；不得覆盖现有 serve、validate、demo、export 和 list 命令。

`acquisition-db backup` 同样 MUST 要求显式 `--db` 与 `--data-root`，但 data root 只作为显式备份输出根；该命令不得创建/修改 binding intent、namespace、migration 或 run。

reconcile 的 `--from-run` 与 `--from-latest-run` MUST 调用 acquisition-checkpoints 规格定义的同一权威 selector。显式父 run 未 finalized 时必须拒绝；`--from-latest-run` 只能选择同 ticker/scope 的最新 finalized 非 reconcile production run，并 MUST 在输出中明确列出被排除的更新但未 finalized run，不得把它们当父项或静默恢复。CLI JSON SHALL 回显 resolved parent、selection strategy、精确 barrier/work position、source/query/partition、父时间片和 effective overlap range；API 与 CLI 对同一父 run/`as_of` 必须得到相同目标。`incremental` 在任一 enabled 且适用来源缺少兼容安全 checkpoint 时 MUST 在创建 run/attempt 与外部 I/O 前拒绝，不能为了完成验收而生成无安全起点的增量运行。

#### Scenario: CLI 重复 incremental
- **WHEN** 用户在 baseline 后连续两次运行 `600519` incremental
- **THEN** CLI SHALL 分别输出独立 run ID，并让用户观察第二次运行的 `unchanged`、新增/变化、缺口和 checkpoint 结果

#### Scenario: CLI 请求未知模式
- **WHEN** 用户传入不属于三种模式的值
- **THEN** CLI SHALL 在联网前返回参数错误且不创建伪运行

#### Scenario: 隔离试点运行
- **WHEN** 用户为 acquire 或 smoke 同时指定独立 `--db` 与 `--data-root`
- **THEN** CLI SHALL 只构建一次经过 namespace 校验的运行时，所有 SQLite、blob、derived、manifest、backup 和 quarantine 写入 SHALL 保持在该隔离根内，默认工作库与默认 raw 目录不得改变

#### Scenario: 写入命令缺少隔离参数
- **WHEN** 用户执行 acquire start/execute 或 smoke 但缺少 `--db` 或 `--data-root` 任一参数
- **THEN** CLI SHALL 在创建 run 和外部 I/O 前以退出码 2 拒绝请求，且默认数据库与默认数据目录不得发生变化

#### Scenario: CLI 遇到可重试执行冲突
- **WHEN** execute 遇到同 run 活跃租约或 SQLite 在有限 busy timeout 内仍繁忙
- **THEN** CLI SHALL 退出 5，并在 JSON 中分别输出 `active_lease|storage_busy`；不得误报为参数错误、材料缺口或内部失败

#### Scenario: CLI reconcile 选择精确 barrier 而非父运行最早 coverage
- **WHEN** `--from-run` 指向一个 finalized partial run，父覆盖起点早于其最早未解决 barrier
- **THEN** CLI SHALL 使用权威 selector 创建只覆盖目标、必要 dependency 与 overlap 的 reconcile plan，并在 JSON 中回显 barrier ID、opening attempt、work position 和 effective range；不得把父 coverage 的全局最早时间直接用作 reconcile 起点

#### Scenario: CLI 拒绝未 finalized reconcile 父项
- **WHEN** `--from-run` 指向人工中止且没有 final event 的运行
- **THEN** CLI SHALL 在创建子 run、attempt 或联网前以参数/状态错误退出，保留旧运行不变，并不得把 execute/resume 作为隐式替代

#### Scenario: CLI 在安全 checkpoint 不齐时拒绝 incremental
- **WHEN** baseline 因 CNINFO prerequisite timeout 或 SSE challenge 没有为所有 enabled 且适用来源形成安全 checkpoint
- **THEN** 后续 incremental 命令 SHALL 在创建 run 和联网前拒绝，列出缺少安全 checkpoint 的来源，并保持真实联网门 pending

### Requirement: 在线 smoke 与正式状态同源
在线 smoke SHALL 从注册表枚举 `smoke_enabled` 且适用的来源/探针查询，复用同一 adapter 解析和规范 attempt 状态，并持久化独立的 smoke run 与 attempts。smoke MUST 使用安全、最小请求，不得推进生产 checkpoint；某来源失败不得被其他来源成功掩盖，也不得通过中文消息子串推断状态。

#### Scenario: 一个来源超时
- **WHEN** smoke 中巨潮成功而上交所超时
- **THEN** 输出 SHALL 分别保留 `success` 与 `timeout`，总体标记有缺口，并且两个尝试均可按 smoke run ID 查询

#### Scenario: smoke 通过
- **WHEN** 所有适用探针均取得允许的成功、未变化或合法空结果
- **THEN** smoke SHALL 标记该次探针通过，但 MUST 不推进正式来源 checkpoint，也不得声称 baseline 或黄金样本通过

### Requirement: SyncResult 使用结构化权威结果并兼容旧读者
同步结果 SHALL 以 acquisition run、attempt、coverage、checkpoint 和 snapshot 引用作为权威字段。旧 `provider_results` SHALL 在兼容期保留，但 MUST 由结构化 attempts 确定性派生、标记 deprecated，且不得反向驱动状态、checkpoint 或证据。旧 `SyncResult` JSON 缺少新字段时 MUST 继续可读，并以 `legacy_unassessed` 表示无可证明的历史采集状态，禁止从自由文本推断 `no_data` 或成功。

#### Scenario: 读取旧同步 JSON
- **WHEN** 数据库包含只有 `provider_results` 的旧 `SyncResult`
- **THEN** 系统 SHALL 保持原对象可读，将新采集关联留空/标记 `legacy_unassessed`，并且不制造 attempts、coverage 或 checkpoints

#### Scenario: 新结果生成旧摘要
- **WHEN** 新采集运行终结
- **THEN** 兼容 facade SHALL 返回确定性 provider 摘要，且审计 API SHALL 始终展示原始规范 attempts 与覆盖项

### Requirement: 旧同步入口与现有适配器兼容但不绕过注册表
现有 `POST /api/companies/{ticker}/sync` 和已知旧 provider 名 SHALL 保持可用的兼容路径；已知 alias MUST 映射到固定、已批准的来源定义或明确标记为现有非 `business_model` 范围的 legacy 定义。`business_model` v1 覆盖只计算四个固定的版本化来源定义，并按各自 `enabled|pending_policy` 状态决定查询或静态处置；本 change 不删除现有财务适配器、不把付费 Tushare 纳入新范围，也不允许旧入口绕过来源许可、快照或 attempt 持久化门禁。

#### Scenario: 旧 official alias
- **WHEN** 旧调用方为沪市公司执行非 `business_model` 的既有 scope 并请求 `providers=["official"]`
- **THEN** 兼容层 SHALL 将其解析为适用的巨潮与上交所批准定义，保留结构化 attempts，并继续返回旧调用方可读取的同步结果字段

#### Scenario: business_model 请求使用 official alias
- **WHEN** 调用方为 `600519` 的 business_model 运行传入旧 `official` alias
- **THEN** alias SHALL 只作为兼容输入且不得收窄计划，系统仍 SHALL 执行巨潮与 SSE 的适用查询、为 SZSE 生成 `market_not_applicable` 静态处置，并为贵州茅台 IR 执行 enabled 查询或生成 `pending_policy` 静态处置，完整保留四个 v1 来源的覆盖

#### Scenario: 现有财务同步
- **WHEN** 旧调用方执行不属于 `business_model` v1 的既有财务 scope
- **THEN** 系统 SHALL 保持现有能力可用并通过明确的 legacy 注册定义路由，不得把这些来源误列为 v1 商业模式正式来源

### Requirement: 手工文档入口遵守来源与快照门禁
手工文档 API/CLI MUST 要求引用已批准来源定义和可验证上游身份，并经过同一归档、哈希、point-in-time 与快照冻结流程。提供注册表外 URL/来源名称时 SHALL 只生成 source candidate 或返回待审核结果，不得直接创建 `official-document`、正式 `SourceRecord` 或可供报告消费的文档。

#### Scenario: 上传已批准官方 PDF
- **WHEN** 用户上传文件并选择有效官方来源定义且元数据通过校验
- **THEN** 系统 SHALL 先冻结原始资源快照，再创建兼容文档引用

#### Scenario: 自由填写新网站名称
- **WHEN** 用户只提供未注册的 source_name/source_url
- **THEN** 系统 SHALL 创建待审核 candidate 或拒绝正式入库，不得把材料自动标为权威来源

### Requirement: 只有可消费运行可成为下游最新批次
只有 registry 校验通过、证据门禁通过、没有未终结/未解决 abandoned attempt、覆盖清单可解释且所有 discovery proof 与 required fetch 均无阻塞缺口的运行，才可标记 `default_consume_eligible=true` 并成为报告/解析器的“最新可用”同步批次。某来源 required attachment 失败时，另一镜像取得相同材料不能清除该来源 barrier。失败、受限或部分运行 MUST 保留供审计，但不得遮蔽同 scope 的较早可消费批次。

#### Scenario: 新 incremental 部分失败
- **WHEN** 最新 incremental 包含 `partial_success` 且旧 baseline 仍可消费
- **THEN** 审计查询 SHALL 显示新运行，但下游默认最新批次 SHALL 继续选择旧 baseline，除非调用方显式请求审阅部分运行

### Requirement: 三类验收门独立呈现
系统和项目验收记录 MUST 分别呈现自动化测试门、真实联网样本门和人工黄金样本门；任何一门通过不得自动设置另一门通过。v1 真实联网试点若要标记 passed，MUST 使用全新隔离 namespace 对 `600519 贵州茅台` 执行一次自动 finalized 的 baseline，并仅在所有 enabled 且适用来源均形成兼容安全 checkpoint 后执行至少两次重复 incremental，再对 finalized 父运行执行一次精确目标 reconcile；逐一观察四个 v1 来源的适用/跳过/失败状态。旧的未 finalized run 只能作为不可变诊断证据保留，不得 resume、修改或用作 reconcile 父项。若外部许可、网络或来源侧 challenge 在系统遵守分类、零绕过、来源熔断、完整 coverage accounting 和 checkpoint barrier 合同的前提下阻止安全 checkpoint，incremental SHALL 按前置条件拒绝，真实联网门保持 `pending`；不得用无效 incremental 补齐次数，也不得把正确受控的外部不可达自动记为 `failed` 或 `passed`。只有状态误分类、挑战后继续 I/O、依赖失败被伪装为解析错误、未解释 coverage、越过 barrier、证据门禁失效或其他软件合同违规才使该次真实联网门为 `failed`。人工黄金样本 MUST 独立核对覆盖、状态语义、版本链和抽样原文。单公司结果不得命名为“全部 A 股验证完成”。

#### Scenario: 自动化全绿但未联网
- **WHEN** 所有离线测试通过而真实联网与人工黄金检查尚未执行
- **THEN** 状态 SHALL 分别显示“自动化通过、在线未执行、人工未验收”，不得显示 change 全部验收通过

#### Scenario: 贵州茅台试点通过
- **WHEN** `600519` baseline、重复 incremental 和人工抽样均达到各自门槛
- **THEN** 系统 SHALL 仅记录“贵州茅台采集试点通过”及其时间点/来源限制，不得外推为全部 A 股或完整商业模式分析通过

#### Scenario: 外部挑战被正确控制时在线门保持 pending
- **WHEN** 新 baseline 遇到 SSE 明确 challenge，系统将 opening attempt 记为 restricted、对该来源后续工作零 I/O 熔断、完整写入 barriers 并自动 finalize 为不可消费的 partial run
- **THEN** 自动化门可独立通过，但真实联网门 SHALL 记录外部阻断及 run/attempt/checkpoint 证据并保持 pending；不得把该结果称为在线通过，也不得仅因来源拒绝访问把软件合同判为 failed

#### Scenario: 挑战后继续请求构成在线门失败
- **WHEN** 真实运行把明确 challenge 误记为普通 MIME 解析失败、继续请求该来源后续附件或推进越过该位置的 checkpoint
- **THEN** 真实联网门 SHALL 标记 failed 并保留诊断证据；其他来源成功、离线测试全绿或人工终止进程均不得掩盖该合同违规

### Requirement: additive 迁移与可恢复回滚
采集存储初始化 MUST 显式支持以下迁移矩阵：经检查确为空且 `user_version=0` 的 fresh 数据库在单一事务中 bootstrap 到完整 v6；v4 先生成并验证 v4 备份、事务化执行既有 0005，确认 v5 后生成经 SHA-256、integrity_check 和外键检查验证的 v5 recovery point，再以独立事务执行 0006；v5 必须先生成上述已验证备份再事务化执行 0006；v6 重开 MUST 严格 no-op。`user_version=0` 但已有未知用户表、低于受支持版本、迁移记录与版本冲突或高于 v6 的数据库 MUST 在任何 DDL/DML 前失败关闭。0005/0006 每项只能成功记录一次，旧表、旧 payload、旧报告和旧快照必须逐字节不变；任一阶段失败 MUST 回滚当前事务并保留最近已验证恢复点。版本/备份检查不得通过构造会自动迁移的存储对象完成。功能回滚 SHALL 优先关闭新入口而保留 v6 数据；若必须运行旧版本程序，MUST 先停止服务并把匹配版本的已验证备份恢复到独立路径，禁止旧程序直接打开 v6 工作库。

#### Scenario: fresh 空数据库初始化
- **WHEN** 数据库 `user_version=0`、没有未知用户表且通过空库检查
- **THEN** 系统 SHALL 在单一显式事务中创建完整 v6 schema，不制造 legacy 备份或历史 payload，并在故障时回滚为空库状态

#### Scenario: v4 经 v5 升级到 v6
- **WHEN** 一个受支持 v4 数据库通过完整性检查并启动迁移
- **THEN** 系统 SHALL 依次生成已验证 v4 备份、只执行一次 0005、生成已验证 v5 recovery point、再只执行一次 0006；任何旧 payload 与报告内容不得改变

#### Scenario: v6 重开不产生写入
- **WHEN** 一个一致的 v6 数据库再次由当前程序打开
- **THEN** 初始化 SHALL no-op，不重写 migration row、事件、payload、checkpoint 或快照

#### Scenario: dirty v0 或未来版本
- **WHEN** `user_version=0` 但存在未知用户表，或者数据库版本高于 v6/不在支持矩阵中
- **THEN** 系统 SHALL 在备份以外的任何 DDL/DML、run 创建或外部 I/O 前失败关闭，不猜测迁移路线

#### Scenario: v6 迁移中途失败
- **WHEN** 创建新表或索引时发生异常
- **THEN** SQLite SHALL 回滚整个 0006 事务、保持 v5 user_version 与全部旧 payload 不变，且 acquisition v1 保持禁用

#### Scenario: 恢复 v5 备份给旧程序
- **WHEN** 操作员必须从新二进制回退到旧程序
- **THEN** 系统 SHALL 停止写入、验证备份 SHA-256/integrity/外键、恢复到独立 v5 路径并完成旧只读 smoke；v6 工作库和新 snapshot SHALL 保留且不得由旧程序打开或删除

#### Scenario: 历史自由文本同步数据迁移
- **WHEN** v5 数据只有 `provider_results` 或缺少可验证 raw bytes
- **THEN** 迁移 SHALL 保留原记录并标记 `legacy_unassessed`/unresolved，不得推断 attempts、no_data、available_at、snapshot 或 checkpoint
