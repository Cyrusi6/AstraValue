## Purpose

本能力把每份允许保留的网络或人工材料先冻结为不可变、可校验、具 point-in-time 语义的原始资源与证据快照，确保解析器和 Codex 永远不能直接消费未归档网页或临时响应。

## ADDED Requirements

### Requirement: 相同正文的解析复用保留各自谱系
MinerU SHALL 对相同 namespace、原始 PDF SHA-256、配置和 extractor 版本使用内容锁，并复用已经完成的同内容解析。consumer MUST 新建以自身 snapshot 为父的派生，以 `parse_reuse` 固定单跳 producer bundle 及各派生 ID；原 parent、snapshot、来源观测与旧文件不得修改。双方 PDF、原始及派生字节、许可、namespace、配置和隔离状态 MUST 验证。text/Markdown 必须与 producer 一致，layout 仅更换 consumer 快照/bundle 身份。缓存读取及 manifest 消费 MUST 复核同一关系；不同字节/配置不复用，在途同内容任务先恢复 producer，不另行上传。

#### Scenario: 两个 URL 取得相同 PDF
- **WHEN** 两个快照的来源身份分别保留、字节与解析配置相同，且第一份已解析完成
- **THEN** 第二份 SHALL 零 allocate/upload 生成自身派生引用，保留原 producer 关系，正文事实不会把镜像计作独立证明

#### Scenario: producer 被隔离或派生被伪造
- **WHEN** producer 原文/派生损坏、已隔离，或 consumer 派生输出与声明的原解析不一致
- **THEN** 复用及正式 manifest 消费 MUST 拒绝，不由已有缓存掩盖异常

### Requirement: 正文选择排除具有正式资源引用
独立审计正文排除 SHALL 进入 `EvidenceManifestExclusion(object_type=resource, object_id=discovered_resource_id, reason_code=excluded_standalone_audit_pdf)`，按资源 ID 去重排序，并在 coverage summary 按理由计数。构建和重新消费前 MUST 验证目录行、父 discovery attempt、本运行 namespace 与冻结 registry/definition 哈希、原 required fetch 和选择理由一致。只有此明确选择理由可作为非阻断排除，不得泛化为任意 policy_skipped。未含该排除的旧 manifest 哈希 MUST 保持。

#### Scenario: 正常停采可追溯且不掩盖失败
- **WHEN** 新运行同时含合格正文、独立审计目录项及真实失败项
- **THEN** manifest SHALL 记录独立审计的资源级排除，归档汇总将其视为 metadata_only 正常终态；真实失败仍保留缺口，不能被停采计数覆盖

#### Scenario: 非本运行或未经冻结的排除
- **WHEN** 传入另一运行资源、未知排除理由、错误标题/MIME、被改动的策略或未发生 true 到 false 选择的记录
- **THEN** 证据清单 MUST 拒绝该排除，不能通过构造排除项使正文消费门放行

### Requirement: MinerU 精准解析与逐页原文对应
系统 SHALL 使用 MinerU 精准解析 API v4，显式选择 vlm 模型并开启扫描识别、表格与公式，替换旧本地 OCR 流程。云解析 SHALL 只接受显式选择、许可允许派生及 LLM 处理、未隔离且完整性有效的已提交 PDF snapshot。原始 PDF、原生文本、旧派生物和人工签署 MUST 保留。新派生 SHALL 冻结父快照、API/服务/提取器版本、配置、任务 ID、ZIP/Markdown/逐页布局/阅读文本和哈希；不得伪造置信度或已核对的财务事实。

#### Scenario: 使用本地保存的密钥上传归档公告
- **WHEN** 用户通过 .env.local 或环境变量配置 MINERU_API/MINERU_API_TOKEN 并选择 PDF snapshot
- **THEN** 系统 MUST 在云 I/O 前校验许可、隔离、字节长度、哈希及官方单文件限制，只向固定 MinerU API 主机发送 Bearer，向已校验的官方签名地址上传对应字节；凭据和签名地址不得出现在 Git、公开日志或派生证据参数中

#### Scenario: 解析过程中断后恢复
- **WHEN** 任务已取得 batch ID 或结果已冻结后进程中断
- **THEN** 系统 SHALL 校验任务与 namespace/snapshot/hash/config 身份，继续查询原任务或验证全部缓存派生物后复用，默认不重新提交或重复上传；显式补传只可在同任务仍 waiting-file 时对原地址执行至多一次，认证或参数被明确拒绝后允许后续显式调用重新提交；提交结果未知且无 batch ID 时 SHALL 显式报告不确定状态

#### Scenario: 页码覆盖或布局合同异常
- **WHEN** 返回 ZIP 路径不安全、展开大小超限、结果缺页/重复页、内容页码越界或页面尺寸不匹配
- **THEN** 系统 MUST 拒绝发布完成阅读文本，保留原始公告与已有诊断状态，不能只解析前部后声称全文完成

#### Scenario: 空页面、表格、签章及原生文字
- **WHEN** MinerU 返回完整页面结构并包含空结果、表格或图像内容
- **THEN** 系统 SHALL 保留零基服务页码与一基 PDF 页码映射、原生文字、内容块与表格 HTML；空结果、数字和图像须明确保留复核状态，未返回置信度时 SHALL 保存 null

#### Scenario: API 限流、认证失败或任务失败
- **WHEN** 服务拒绝凭据、限流、额度不足、网络超时或远端解析失败
- **THEN** 系统 SHALL 记录脱敏失败/待处理状态并保留可恢复任务；不得自动切换轻量 API、本地 OCR 或伪造空正文成功

### Requirement: 公开大附件与流式总预算
显式 registry 1.8.0 / CNINFO 1.7.0 SHALL 将响应、压缩和解压上限固定为 128 MiB、attempt 预算固定为 600 秒，保持 socket timeout 30 秒、直连、TLS、并发 1、5 秒间隔与无自动重试。旧 registry 1.7.0 及其冻结计划 MUST 保留原限制；当前默认 registry 1.9.0 / CNINFO 1.8.0 沿用上述大附件预算；补抓 MUST 使用独立冻结输入和运行，保留旧失败。传输 SHALL 在门禁放行、响应头、流读取前后及最终组装后校验预算和租约；已经过期的响应 MUST 关闭且不得发布成功 envelope。阻塞读取仍服从 socket timeout，不声称精确毫秒取消。

#### Scenario: 持续有数据但总预算已耗尽
- **WHEN** 单次 socket 读取持续成功，但下一块、EOF 或组装完成时已超过 attempt deadline
- **THEN** 传输 MUST 关闭响应并报告 timeout，不归档半份或超预算正文，也不自动重试

#### Scenario: 大文件超过旧合同上限
- **WHEN** 已批准目录的正常公开附件超出旧 64 MiB 限额
- **THEN** 系统 SHALL 保留旧 policy_skipped，在显式新版本与独立补抓计划下校验新上限；原运行、旧配置与生产 checkpoint 不变

### Requirement: 已冻结目录的可验证本地输入
已有目录正文归档 SHALL 在来源库只读核对已终结运行、公司、namespace、原始 discovery proof/observation/snapshot、字节哈希、长度和行定位后，生成携带原始响应字节与出处的不可变本地输入。新 namespace MUST 创建新的输入 observation/proof/resource 身份；原始出处仅作为显式 origin 引用，不能重绑定旧 observation。新输入 MUST 标记 proof_kind=retained_inventory、http_status=null、io_performed=false，总数仅指本地选择。空本地目录 MUST 拒绝，不能证明来源 no_data。

#### Scenario: 历史 HTML 使用新版 MIME 合同归档
- **WHEN** 旧目录行曾以旧 PDF MIME 合同保存，但原始行明确给出 HTML URL
- **THEN** 系统 SHALL 保留旧资源不变，在新目录输入中依据 schema 3 解释该 URL，冻结新来源版本及原始行出处后执行正文请求

#### Scenario: 输入证据被修改
- **WHEN** 本地目录的公司、namespace、标题、URL、原始响应字节、行哈希或谱系不符
- **THEN** 系统 MUST 在正文请求前拒绝该输入并记录错误，不得作为新 HTTP discovery 成功

### Requirement: 分批归档状态闭合与恢复
目录归档 SHALL 先处理定期报告、更正版本与招股材料，再处理其余公告，按 canonical ID 去重但保留各公告版本。每批 SHALL 显式冻结 ad_hoc 计划，并记录每条资源成功、失败或尚未请求；成功正文 SHALL 核对原始哈希并单独保存确定性文本和材料分类派生版本。恢复 SHALL 复用成功 fetch，保留已终态失败且不默认重试。明确访问挑战 MUST 停止本次归档任务的后续来源请求，包括未开始批次，不得借新批次规避来源停止。

#### Scenario: 中断后恢复含已失败资源的批次
- **WHEN** 一条下载已有 timeout 终态，下一条之前进程中断
- **THEN** 恢复 SHALL 保留 timeout、重建其缺口并从未处理位置继续，不能再次自动请求失败条目或将最终结果称为无缺口

#### Scenario: 正文可归档但没有可提取文字
- **WHEN** PDF 原始文件有效但文本层为空
- **THEN** 原始归档 SHALL 保留，原生文本状态 SHALL 明确 requires_review；用户授权的 MinerU 精准解析 SHALL 另建派生版本并保留其机器识别属性

### Requirement: 版本化历史 HTML 正文与派生文本
CNINFO 公告 schema 3 SHALL 从已批准目录行的附件后缀确定 PDF 或 HTML MIME，旧 schema 的重放行为 MUST 保持原样。系统 MUST 先按 HTTP、挑战信号和 MIME 分类，再做有界 HTML 结构/编码检查；错误页、不完整页面和明确拦截不得成为正文快照。原始字节归档及哈希复核后，文本提取 SHALL 仅接受 content snapshot ID，按声明的中文编码严格解码并冻结带提取器版本与哈希的派生文本，不执行脚本或加载外部资源。

#### Scenario: 合法历史 HTML 摘要
- **WHEN** 目录返回 HTML 附件且响应通过分类及结构校验
- **THEN** 系统 SHALL 保存原始字节哈希与来源 lineage，并从已提交快照提取无乱码的公告文本，记录派生哈希；不因非 PDF 而拒绝正常公告

#### Scenario: HTML 地址返回拦截页
- **WHEN** HTML 附件请求返回精确挑战头、验证码或登录信号
- **THEN** 系统 SHALL 保留对应受限终态且不创建 content snapshot，不得因已允许 HTML MIME 而放行该页面

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
任何未来拟提交给 Codex 或其他 LLM 作研究推断的材料批次 MUST 先生成不可变 `EvidenceSnapshotManifest`，固定 run ID、问题清单版本、允许 LLM 的来源定义版本、原始资源快照 ID、所用派生文本/页图版本与哈希、`as_of`、排除项和清单哈希。门禁 MUST 逐项验证归档存在、哈希匹配、point-in-time 合格、许可允许且未隔离；任一验证失败时不得部分静默放行。本 change 的研究消费入口实现 manifest 构建与校验，不调用 Codex 作定性推断；用户授权的 MinerU 文档解析作为独立派生步骤遵守本能力的云解析约束。

#### Scenario: 合格材料取得未来 LLM 消费资格
- **WHEN** 所有选中材料均已归档、哈希匹配、`available_at` 不晚于 `as_of` 且 `llm_processing=allowed`
- **THEN** 系统 SHALL 冻结证据清单并标记其通过门禁；调用方只能通过该 manifest ID 解析版本化材料；用户另行明确批准的十主题离线试点可交由 Codex 复核，采集 runtime 不自动调用模型，AI 复核不替代人工黄金签署

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
若现有或后续流程从原始快照生成文本、OCR、表格或页图，该派生物 MUST 记录父快照 ID、提取器名称/版本、参数、输出哈希和创建时间。提取器变化或输出哈希变化 MUST 创建新派生版本；搜索索引只可视为可重建缓存，不得覆盖或代表冻结证据，也不得改变旧证据清单引用的派生版本。本 change 的 PDF/HTML 原生文本及 MinerU 精准解析同样遵守以上版本约束；结构化财务表格抽取不在本轮范围。

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
