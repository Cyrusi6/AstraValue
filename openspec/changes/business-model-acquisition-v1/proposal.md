## Why

AstraValue 已具备正式公告下载、哈希归档和部分 point-in-time 快照能力，但来源、查询、失败与覆盖结果仍分散在硬编码适配器和自由文本汇总中，无法证明一次“公司业务与商业模式”采集是否对所有合法、适用且相关的来源与时间范围做过完整尝试。现在需要先建立可审计、可增量、不可回写历史的采集底座，再把材料交给解析器或 Codex；长期目标与证据边界继续以 [计划.md](../../../计划.md) 为准，当前仍为骨架的方法论继续以 [第一步方法文件](../../../docs/methodology/steps/01_business_model.md) 为准。

## What Changes

### Scope

- 按 2026-09-06 用户指令补全本地无文本 PDF 页面：先视觉抽样核对 OCR，再处理全扫描及局部缺文本页；保留原始 PDF、原生文本、PDF 一基页码、文字框坐标、模型/参数与派生哈希，不将 OCR 数字直接提升为财务事实。
- 统一生产 registry 1.9.0 / CNINFO 1.8.0，显式声明正文 validator 兼容版本，严格验证旧正文后经当次真实条件响应复用；在既有正文归档 namespace 追加新的 production baseline 和两次安全 incremental，旧 ad_hoc 目录与所有历史证据保持不变。
- 新增版本化 `SourceDefinition` 注册表，明确上游身份、权威级别、访问方式、许可与使用限制、适用主题、刷新频率、增量策略及 LLM 处理许可；`business_model` v1 范围固定为巨潮资讯、上海证券交易所、深圳证券交易所和一个贵州茅台官方投资者关系站点定义，其中 IR 定义在 exact allowlist 与许可人工核对前保持 `pending_policy/disabled`，既有财务 adapter 仅以 legacy 定义保持兼容。
- 新增 `AcquisitionRun`、区分 `discovery|fetch` 的物理执行 `AcquisitionAttempt`、物理查询计划与业务问题覆盖的多对多关联、可过期执行租约、来源级 checkpoint、水位线、覆盖清单和不可变 `RawResourceSnapshot`；attempt 只引用实际执行的物理计划项，使一次传输查询可在不重复联网的前提下证明来源 × 业务问题 × 时间范围的每个单元状态，并能在进程崩溃后安全接管。
- 支持 `baseline`、`incremental`、`reconcile` 三种运行模式。首次分析从招股书/上市日与来源最早可得边界确定的有效起点回溯；后续使用来源水位线与重叠回看，仅归档新增或变化版本；对历史缺口、乱序和来源修订使用 reconcile。
- 将成功、未变化、无数据、受限、付费、登录要求、限流、超时、网络失败、解析失败、策略跳过和部分成功建模为独立采集状态；`no_data` 只表示查询成功且结果为空，不得被失败或受限状态替代，也不得自动推导为“未披露”。
- 将成功的列表/API 发现响应也纳入可审计证明：许可允许时冻结原始响应，许可不允许保留正文时至少保存响应哈希、长度、schema/分页/总数摘要和规范化资源引用；没有可验证的发现证明不得产生 `no_data` 或推进覆盖。
- 建立强制材料门禁：任何交给下游解析器或 Codex 的内容都必须已由代码归档、重算并核对哈希、通过 `available_at <= run.as_of` 检查，并冻结为可引用证据快照。
- 由注册表驱动 `AdapterManager`、同步 API/CLI、在线 smoke 和结构化同步结果；注册表外网站只能形成隔离的待人工审核 source candidate，批准前不得进入正式证据链。
- **BREAKING**：`POST /api/documents` 不再允许无法映射到已批准来源定义的自由 `source_name/source_url` 直接生成权威文档；已知 alias 继续兼容，未知来源改为返回待审核 candidate/迁移提示。
- 为 SQLite 增加只追加/不可变采集表、运行控制租约和显式迁移链；支持 fresh v0、现有 v4/v5 到 v6 及 v6 no-op，并对未来/不支持版本失败关闭。复用现有 `SourceRecord`、`DocumentRecord`、`SyncResult`、报告与 Parquet 快照，不覆盖旧记录。同 URL 内容变化时创建新文档/资源版本并保留旧版本。
- 将自动化测试、真实联网样本和人工黄金样本验收定义为三个独立门槛；以 `600519 贵州茅台` 做首次 baseline 与重复 incremental 试点，但不据此宣称全部 A 股已验证。

### Non-goals

- 不实现业务结构化事实抽取、Codex 定性推断（本轮仅新增公告原文的确定性文本派生与材料类型识别），亦不判断护城河、定价权、战略可信度或投资价值。
- 不补全 `content_status: skeleton` 的金融方法论，不修改历史报告内容。
- 不接入付费数据，不构建通用互联网爬虫、通用搜索引擎或任何登录、验证码、付费墙及许可限制绕过机制。
- 不在 Git 中保存原始 PDF/HTML/响应、数据库、令牌、浏览器资料或生成报告；实际运行与验收证据仍记录到 [阶段日志.md](../../../阶段日志.md)，不写入本 change 充当已完成证明。

## Capabilities

### New Capabilities

- `acquisition-source-registry`: 版本化来源与查询定义、合法性/适用性/许可/LLM 策略、v1 来源范围及待审核 source candidate 隔离。
- `acquisition-run-coverage`: 三种运行模式、发现/抓取物理尝试、physical-query-to-coverage 多对多追踪、执行租约与崩溃恢复、完整历史覆盖清单及可复核审计结果。
- `acquisition-checkpoints`: 来源级水位线、重叠回看、canonical ID/HTTP validator/哈希变化检测以及失败屏障。
- `raw-resource-snapshots`: 发现响应证明与原始资源不可变归档、同 URL 多版本、时间精度/point-in-time 检查、证据冻结及进入 Codex 前的强制门禁。
- `registry-driven-acquisition-interfaces`: 注册表驱动的 AdapterManager、API、CLI、在线 smoke、结构化同步结果，以及旧 provider 调用的兼容映射。

### Modified Capabilities

- 无；当前 `openspec/specs/` 没有已归档 capability，本 change 全部以新增 delta specs 描述。

## Impact

- **数据模型与存储**：在 `src/analysis/acquisition/` 建立注册表、计划、执行控制、存储、快照和来源专用 adapter 边界，并以兼容投影连接 `src/analysis/models.py`、`storage.py`、`documents.py`；新增 SQLite 迁移、索引、不可变约束、执行租约和历史查询，所有数据库/blob/derived/manifest/backup/quarantine 路径由显式注入的 `data_root` 解析，原始内容继续写入被 Git 忽略的本地目录。
- **采集与接口**：重构 `src/analysis/adapters/manager.py`、`official_adapter.py`、`online_smoke.py`，扩展 FastAPI 同步/查询接口与 CLI；API、CLI、smoke 共用同一注入式采集服务。贵州茅台 IR 定义先以 `pending_policy/disabled` 纳入固定范围，只有 exact allowlist 与许可人工批准的新版本才可启用其站点专用适配器，不提供任意域名抓取入口。
- **兼容性与历史数据**：已知旧 provider 名只作为兼容 alias 解析到固定 `SourceDefinition` 版本，旧 `provider_results` 由结构化 attempts 派生并标记弃用；新程序继续读取旧 `SyncResult`、报告、文档、快照和 SQLite v4/v5 数据且不回填伪造的历史尝试。迁移前数据库必须按支持路径生成已验证备份；若回退旧程序，只能把匹配版本的已验证备份恢复到独立路径供其读取，旧程序不得直接打开 v6 工作库。
- **风险控制**：来源镜像不得伪装成独立证据；许可、登录、验证码和付费边界以停止并记录为准；每跳重定向、响应大小、并发/速率与 schema 按注册表失败关闭；日期精度、乱序修订、future leakage、附件抓取失败、解析失败与部分成功不得静默推进 checkpoint。
- **验收边界**：严格 OpenSpec 校验和自动化测试通过，只代表离线合同成立；真实联网门需分别记录四个 v1 来源在贵州茅台 baseline/重复 incremental 中的观察结果；人工黄金门需人工核对覆盖清单、失败语义、版本链和抽样原文。任一门未通过均必须独立标记，不能由另一个门替代。
