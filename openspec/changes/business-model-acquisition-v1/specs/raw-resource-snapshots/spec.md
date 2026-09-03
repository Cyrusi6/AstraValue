## Purpose

本能力把每份允许保留的网络或人工材料先冻结为不可变、可校验、具 point-in-time 语义的原始资源与证据快照，确保解析器和 Codex 永远不能直接消费未归档网页或临时响应。

## ADDED Requirements

### Requirement: 先归档校验后供下游使用
对允许保留的材料，系统 MUST 先以代码读取原始字节、计算完整 SHA-256 与长度、写入运行绑定 `data_root` 下内容寻址的临时文件、原子发布并重新读取校验，之后才可创建 `RawResourceSnapshot`。快照只保存相对于该数据根的归档标识，路径解析不得接受调用方任意本地目标或模块级默认 raw 目录。除“许可禁止保留 discovery body”的受限原子验证流程外，任何列表/正文解析、文本提取、索引或 Codex 输入生成 MUST 只接受已提交 snapshot/proof/resource ID，不得直接接受 URL、响应对象、任意本地路径或未校验字节；该受限流程也不得把临时 body 暴露给后续组件或 LLM。

#### Scenario: 成功冻结 PDF
- **WHEN** 已批准来源返回合法 PDF 且归档许可允许保存
- **THEN** 系统 SHALL 在解析前完成原始字节归档、完整哈希复核和快照提交，并由后续步骤仅引用快照 ID

#### Scenario: 归档后复核失败
- **WHEN** 原子发布后的字节长度或 SHA-256 与下载结果不一致
- **THEN** 系统 SHALL 隔离该文件、把尝试记为 `parse_failed: integrity_mismatch`、禁止下游读取且不推进 checkpoint

#### Scenario: 调用方提供任意输出路径
- **WHEN** API、CLI、adapter 或手工入口试图让快照写入运行绑定 `data_root` 之外的绝对/相对穿越路径
- **THEN** 系统 SHALL 在写文件前拒绝请求并记录验证错误，默认 raw 目录及目标外路径不得改变

### Requirement: 发现响应先形成证明再解析
每个列表/API page MUST 先有已持久化的 discovery attempt/observation start。许可允许保留响应原文时，系统 SHALL 将确切响应字节冻结为 `resource_role=discovery_response` 的 `RawResourceSnapshot`，纯解析步骤只能从该 snapshot ID 产生规范化 `DiscoveredResource`；每个资源 MUST 保留父 discovery snapshot、页/游标、row locator 或稳定 row hash。许可禁止保留原文但允许最小审计与结构化资源引用时，唯一例外流程 SHALL 是：有界内存响应 → 确定性 `validate_and_normalize_without_retention` → 在一个事务中提交 `DiscoveryObservation + DiscoveryProof + DiscoveredResource[]` → 丢弃响应字节。proof 至少包含响应完整 SHA-256、字节长度、HTTP/MIME、parser/schema 版本与校验结果、页/游标、上游 declared total/page count、规范化行数和终止标记；资源行保留 proof ID 与 row hash，不保留被禁止的正文。该 proof 只证明当时由固定 parser/schema 得到的校验与规范化结果，不能在缺少 body 时声称可独立重放原响应，也不得进入 Codex。若来源合同要求原响应可独立重放而许可禁止保留，查询 MUST `policy_skipped`。缺少任一必需页证明、总数不闭合或 schema 未验证时不得生成合法 `no_data` 或完整覆盖。

#### Scenario: 许可允许冻结列表响应
- **WHEN** 已批准 discovery API 返回合法 JSON 且许可允许保存原始响应
- **THEN** 系统 SHALL 先冻结 discovery response snapshot，再由纯解析步骤产生带父 snapshot 和 row locator/hash 的资源引用；解析器不得直接消费临时 HTTP response

#### Scenario: 许可禁止保留列表正文
- **WHEN** 来源允许自动查询和保存最小审计摘要但禁止归档完整列表响应
- **THEN** 系统 SHALL 在有界内存中一次性验证/规范化，并原子提交 DiscoveryObservation、DiscoveryProof 与 DiscoveredResource 后才丢弃正文；后续只消费已提交 proof/rows，proof 不得被描述为可独立重放原响应、成为 LLM 内容或伪装成正式正文快照

#### Scenario: 禁止正文但合同要求可重放
- **WHEN** 查询审计合同要求未来重新执行 schema/行解析，而来源许可禁止保存原始响应
- **THEN** 系统 SHALL 将该查询终结为 `policy_skipped` 并记录许可冲突，不得用仅含 hash 的 proof 冒充可重放原文

#### Scenario: 空结果缺少终止证明
- **WHEN** 返回列表看似为空但缺少声明的总数、终止标记或必需页 discovery proof
- **THEN** 首个/唯一页尚无已提交成功 segment 时 attempt SHALL 为 `parse_failed`，已有前页成功 segment 时 SHALL 为 `partial_success`；两者都形成 barrier，不得记录 `no_data` 或完成覆盖

### Requirement: 原始资源快照的完整审计字段
每个 `RawResourceSnapshot` SHALL 表示一个来源上的确切字节版本，并记录不可变 snapshot ID、`resource_role=content|discovery_response`、来源定义 ID/版本、creating observation ID、内容类型、字节长度、完整 SHA-256/content blob ID、`available_at`/basis、归档相对路径、storage namespace、版本/前一版本 ID、创建时合规决定和创建时间。`content` 角色还 MUST 记录 canonical resource ID、upstream material ID、canonical URL、原始发布时间字段、`published_at`、`published_at_precision=instant|date|unknown` 和 source timezone；`discovery_response` 角色改为记录 physical query plan item、页/游标和确定性 query-page canonical，可将 upstream material ID/published_at 留空，不得制造公告身份、兼容 `DocumentRecord` 或自动进入 LLM manifest。凭据、Cookie、授权头、令牌和完整个人浏览器信息 MUST 被剔除。同一字节跨不同来源/canonical 取得时 SHALL 复用 content blob，但 MUST 建立各自的 snapshot/observation provenance。

#### Scenario: 审计快照来源
- **WHEN** 审计者读取一个正式资源快照
- **THEN** 系统 SHALL 能从快照追溯到固定来源定义和具体尝试，并能用保存的哈希验证本地原始字节

#### Scenario: discovery response 不伪造公告字段
- **WHEN** 一个已冻结列表页没有上游公告身份或披露发布时间
- **THEN** 快照 SHALL 使用 `resource_role=discovery_response` 与确定性 query-page canonical，允许 upstream material/published_at 为空，并禁止投影为正式文档或默认加入 LLM manifest

### Requirement: 每次访问使用不可变资源观测留痕
每次资源请求 MUST 创建 append-only `ResourceObservation`（或等价 attempt resource result），记录 observation ID、attempt ID、父 discovery/fetch work、来源定义版本、可空 snapshot ID、资源 disposition、原始/最终 URL、逐跳脱敏 redirect chain、脱敏请求/响应摘要、HTTP 状态、ETag、Last-Modified、validator 来源 snapshot ID、observed_at、retrieved_at 和 reason code。成功取得或复用正式内容时 disposition MUST 为 `new|changed|unchanged` 并引用 snapshot；受限、网络失败或在快照提交前失败时 snapshot ID MUST 为空并引用对应 attempt 终态，不得制造正式内容 disposition。后续相同哈希请求 SHALL 新增 observation 并引用既有 snapshot；不得为更新 validator 或观测时间修改 snapshot。

#### Scenario: 受限响应没有正式快照
- **WHEN** 一个资源请求返回登录页或验证码而未形成合法原始内容
- **THEN** 系统 SHALL 追加 snapshot ID 为空且关联 `login_required|restricted` attempt 的 ResourceObservation，不得伪造 `unchanged` disposition 或正式快照

#### Scenario: 相同资源再次被观察
- **WHEN** 新 attempt 返回与既有来源/canonical snapshot 相同的内容哈希但新的 ETag 和观测时间
- **THEN** 系统 SHALL 创建新的 `unchanged` ResourceObservation 并引用既有 snapshot，原 snapshot 字段 SHALL 保持不变

#### Scenario: 两个镜像返回相同字节
- **WHEN** 巨潮和上交所取得相同 SHA-256 的官方 PDF
- **THEN** 两个来源 SHALL 各有 snapshot/observation provenance 且共享 content blob，并通过相同 upstream material ID 表明不独立

### Requirement: 保守的 point-in-time 资格
`available_at` MUST 表示“该确切内容版本可被证明已公开”的最早安全时间，并保存其计算依据；若来源只有本地日期而没有时分秒，PIT 资格 MUST 使用该来源时区下所述日期结束后的日界作为保守上界，不能伪造成当天 00:00；若来源没有可验证的版本发布时间或不可变版本标识，系统 SHALL 保守使用本次 `retrieved_at`。只有 `available_at <= run.as_of` 的快照才可进入该运行的冻结证据；原始发布时间、精度、观测时间和获取时间不得相互替代或被未来时间回填。

#### Scenario: 当前下载的历史页面没有版本时间
- **WHEN** 页面声称内容属于 2020 年但系统无法证明当前字节版本何时公开
- **THEN** 快照 `available_at` SHALL 使用当前获取时间，且不得进入截止于 2020 年的历史运行

#### Scenario: 来源提供可验证公告发布时间
- **WHEN** 官方公告具有可验证发布时间且 canonical 版本未发生不明覆盖
- **THEN** 系统 SHALL 以该可验证发布时间作为 `available_at`，同时仍保存本次 observed/retrieved 时间

#### Scenario: 来源只提供当地日期
- **WHEN** 来源只声明 `2026-09-03` 且来源时区为 Asia/Shanghai，没有可验证时分秒
- **THEN** 快照 SHALL 保存 `published_at_precision=date` 并按下一本地日界计算保守 PIT 上界；截止 `2026-09-03 10:00` 的运行不得把它当作当时已可得

### Requirement: 同 URL 多版本与历史不可变
同一 canonical resource ID 或 URL 出现新内容哈希时，系统 SHALL 创建新的 `RawResourceSnapshot` 和新的文档版本，以 `supersedes_snapshot_id`/版本号关联前一版本；旧字节、旧派生物、旧 `DocumentRecord`、旧数据快照和旧报告 MUST 保持逐字节及引用不变。相同哈希 MUST 复用同一内容 blob，但每次尝试的观测记录不得丢失。

#### Scenario: 公司替换原 URL 的 PDF
- **WHEN** 同一 IR URL 在第二次运行返回新 SHA-256
- **THEN** 系统 SHALL 创建新快照和新文档版本、保留旧版本，并让旧报告继续指向旧版本

#### Scenario: 重复获取同一字节
- **WHEN** incremental 再次获取与已有快照相同的完整 SHA-256
- **THEN** 系统 SHALL 复用 blob 和已有快照引用，并且 SHALL 以新 ResourceObservation 保留 `unchanged` disposition 及本次 validator/观测时间

### Requirement: 供未来 Codex 使用的冻结证据清单门禁
任何未来拟提交给 Codex 或其他 LLM 的材料批次 MUST 先生成不可变 `EvidenceSnapshotManifest`，固定 run ID、问题清单版本、允许 LLM 的来源定义版本、原始资源快照 ID、所用派生文本/页图版本与哈希、`as_of`、排除项和清单哈希。门禁 MUST 逐项验证归档存在、哈希匹配、point-in-time 合格、许可允许且未隔离；任一验证失败时不得部分静默放行。本 change 只实现 manifest 构建与校验合同，不实现实际 LLM 调用、文本抽取或定性推断。

#### Scenario: 合格材料取得未来 LLM 消费资格
- **WHEN** 所有选中材料均已归档、哈希匹配、`available_at` 不晚于 `as_of` 且 `llm_processing=allowed`
- **THEN** 系统 SHALL 冻结证据清单并标记其通过门禁；未来调用方只能通过该 manifest ID 解析版本化材料，本 change 不得据此实际调用 Codex

#### Scenario: 一份材料不允许 LLM
- **WHEN** 候选清单中一份材料的来源定义禁止 LLM 处理
- **THEN** 系统 SHALL 将其列为排除项并拒绝将其内容交给 Codex；不得通过复制文本或更换 URL 绕过策略

### Requirement: 受限响应不得成为正式证据
登录页、验证码、JavaScript 挑战、付费墙、robots/许可禁止的正文、非预期 MIME、截断文件、错误页、越界重定向和超过注册表硬上限的响应 MUST 被识别并停止处理。系统 MUST 在发起每一跳请求前重新校验 scheme/host/port/path，禁止 HTTPS 降级、URL 凭据和未批准的 private/loopback/link-local 目标；Content-Length 超限时在读取正文前停止，长度缺失/不可信时按流式压缩与解压上限停止。系统 SHALL 记录 attempt 状态、脱敏 redirect chain 和在许可范围内的最小诊断元数据/响应哈希，但不得把这些响应归档为正式资源快照、解析为正文、无限重试或尝试绕过访问限制。

#### Scenario: PDF URL 返回验证码 HTML
- **WHEN** 声明为公告 PDF 的 URL 返回验证码 HTML
- **THEN** 尝试 SHALL 记录 `restricted`，不得创建正式 PDF 快照、文档或 no_data 结果

#### Scenario: 许可禁止保存正文
- **WHEN** 来源允许查询元数据但禁止归档正文
- **THEN** 系统 SHALL 只保存允许的元数据与状态，正文不得落盘或进入正式证据

#### Scenario: 重定向到未批准域名
- **WHEN** 已批准 URL 的任一 Location 指向注册表 allowlist 之外的域名或路径
- **THEN** 系统 SHALL 在请求下一跳前停止，把该请求 attempt 记为 `policy_skipped: redirect_not_allowlisted` 并可生成 SourceCandidate；未批准响应不得被请求或保存为正式证据

#### Scenario: 传输目标违反安全策略
- **WHEN** 下一跳发生 HTTPS 降级、URL 携带凭据或解析到未批准 private/loopback/link-local 目标
- **THEN** 系统 SHALL 在发起请求前终止为 `policy_skipped: transport_target_forbidden`，不得连接目标、创建正式 snapshot 或重试绕过

#### Scenario: 流式响应超过硬上限
- **WHEN** 响应无可信 Content-Length 且读取过程中超过压缩或解压字节上限
- **THEN** 系统 SHALL 终止流并把 fetch attempt 记为 `policy_skipped: response_size_exceeded`，不得发布 blob/snapshot 或针对同一超限响应自动重试；若这是已有成功页后的 discovery page，聚合 discovery attempt SHALL 按统一规则为 `partial_success`

### Requirement: 派生文本和索引版本化
若现有或后续流程从原始快照生成文本、OCR、表格或页图，该派生物 MUST 记录父快照 ID、提取器名称/版本、参数、输出哈希和创建时间。提取器变化或输出哈希变化 MUST 创建新派生版本；搜索索引只可视为可重建缓存，不得覆盖或代表冻结证据，也不得改变旧证据清单引用的派生版本。本 change 只把现有文本/OCR 输出纳入版本约束，不新增业务文本/表格抽取能力。

#### Scenario: OCR 版本升级
- **WHEN** 同一 PDF 使用新 OCR 版本产生不同文本
- **THEN** 系统 SHALL 保存新的派生 artifact 版本并保留旧文本，旧证据清单仍解析到原版本

### Requirement: 快照完整性检查与隔离
系统 SHALL 支持按 snapshot ID 重新读取归档字节并校验完整 SHA-256、长度和父子引用。每次检查 MUST 追加不可变 `SnapshotIntegrityEvent`，记录 `verified|quarantined` 结果、时间和原因；当前可消费性由事件派生，不得修改原 snapshot。缺失、被修改或引用断裂的快照 MUST 追加 quarantined 事件、触发 reconcile 候选且不得用于新报告或新 Codex 清单；历史报告本身不得被改写。

#### Scenario: 原始 blob 被外部修改
- **WHEN** 完整性检查发现归档文件哈希与快照不一致
- **THEN** 系统 SHALL 追加 quarantined integrity event、阻止新消费并建议 reconcile，同时保持原 snapshot 和所有历史报告原样

### Requirement: 本地敏感与生成数据不得进入 Git
原始 PDF/HTML/响应、隔离内容、SQLite/DuckDB/Parquet、令牌、浏览器资料、派生文本和生成报告 MUST 仅写入运行绑定且经 storage namespace 校验的项目已忽略/显式受控本地数据目录，且不得被 OpenSpec 产物或 Git 跟踪。数据库只保存不泄露本机绝对路径的 namespace 与相对归档标识。仓库可提交的夹具 MUST 是经审核、无敏感信息且明确用于测试的最小冻结样本。

#### Scenario: 新增快照目录
- **WHEN** 实现引入新的原始或隔离存储目录
- **THEN** 自动检查 SHALL 证明目录被 Git 忽略且没有令牌、真实数据库或未审核原文进入暂存文件
