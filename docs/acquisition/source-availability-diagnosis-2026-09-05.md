# CNINFO / SSE 来源可用性诊断与最小修复建议

- 诊断日期：2026-09-05；下文时间为 UTC，北京时间为 UTC+08:00。
- 代码基线：`d060f39828cc3afa70e8bcaaa0f2cd4f7fb4248b`，已 fast-forward 合入并推送 `main`；[main CI 33947894268](https://github.com/Cyrusi6/AstraValue/actions/runs/33947894268) 于 `2026-09-05T05:44:33Z` 成功。
- 当前访问合同：registry `astravalue.data_sources@1.3.0`；CNINFO definition `1.2.0`、SSE definition `1.3.0`。
- 本文是已完成的诊断与待实施建议。没有修改生产代码、registry、历史规划记录或已存运行；实际运行记录归属[阶段日志](../../阶段日志.md)。

## 结论

1. **CNINFO 的 504 与本次使用的网络路由有关。** 默认 HTTPX 客户端继承了 Windows 系统代理 `127.0.0.1:7897`；该路由完成 CONNECT/TLS 后收到 HTTP 504。相同 bootstrap URL、GET 方法、固定请求头及空参数，经本次 CNINFO 专用直连进程取得 HTTP 200，成功解析并唯一匹配 600519。这个对照支持显式管理路由，但不能证明本地代理程序本身故障，也不能证明直连长期稳定。
2. **连通后出现独立的 CNINFO 响应合同缺口。** 公告 POST 返回 HTTP 200 JSON，其中 `announcements=null`、`totalAnnouncement=0`、`hasMore=false`，其他已给出的计数也为 0。当前 parser 只接受数组，严格按既有合同记为 `parse_failed`。应在新版本合同中定义这类空集合；本轮不把既有失败改写成 `no_data`。
3. **SSE 元数据接口可用，附件访问仍有明确限制证据。** 当前 metadata probe 返回有效空结果；既有附件观测含 `x-tengine-error: denied by bot`，来源停止行为正确。本轮没有重试附件、切换 SSE 路由或访问其他端点来尝试取得受阻文件。

因此，下一轮的最小工程范围是“显式运行路由 + CNINFO null 空集合合同”。SSE 附件可用性仍是独立的外部条件；这两项工程改动不能单独保证当前业务采集 change 的在线门通过。

## 证据与诊断边界

诊断调用未修改的 `analysis.cli.main` 与正式 acquisition runtime，使用 registry 中已有的 `smoke_enabled` 查询。一个仅观察日志的本地包装器记录连接阶段、时间和 HTTP 状态；不记录完整 httpcore 消息、响应头、Cookie、请求正文或响应正文。包装器和全部原始输出在忽略目录，未加入 Git。

两个新运行均为 `run_kind=smoke`、metadata-only；模型内的 `mode=incremental` 不代表 production incremental，也不计入 task 9.3。隔离 namespace 为 `96131c1f-a498-434b-a262-95356dd6889e`。

| 对照 | 运行 / 查询 | 实际返回 | 从等待响应头开始到取得响应头 |
| --- | --- | --- | ---: |
| 默认系统代理 | `b2708b4f-fc06-41b3-bfe5-a5bede2eab71` / CNINFO bootstrap | HTTP 504；`nginx`、CDN 响应头；依赖公告查询零 I/O 跳过 | 20.084 秒 |
| 同一默认系统代理 | 同一 run / SSE 定期报告 metadata | HTTP 200，`validated_empty_result` | 0.055 秒 |
| CNINFO 专用直连进程 | `ea3611ff-f2ed-430e-9c53-69cd4862208d` / CNINFO bootstrap | HTTP 200；600519 匹配 1 条，存在 orgId | 0.195 秒 |
| 同一直连进程 | 同一 run / CNINFO 公告 POST | HTTP 200；null 空集合被既有 parser 拒绝 | 0.178 秒 |

这些时间不是端到端耗时或性能基准，不含 runtime 初始化、DNS/门禁等待、完整下载和归档。代理与直连探针是相邻时点的单次对照，不是重复控制实验。两个 bootstrap 物理计划的来源版本、查询、URL、方法、编码、固定请求头、实际参数完全一致；bootstrap 参数为空。两个公告计划的日期参数均为 `2026-09-04~2026-09-05`，公司参数继续从已冻结 bootstrap proof 动态绑定。

### 网络路由定位

- 原 baseline/reconcile 的 CNINFO attempt 总时长约 25.35 秒，持久化 observation 已记录真实 HTTP 504、`text/html`、2051 字节以及 nginx/CDN 响应头。它们不是客户端将未收到响应的 DNS/TLS 错误伪装成 504；但旧运行没有记录有效代理配置，不能回填其路由事实。
- 本轮环境没有 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量，但 `urllib.request.getproxies()` 读到了 Windows 系统代理。HTTPX `0.28.1` 的默认配置继承该设置；实际请求 trace 显示 TCP 连接目标为 loopback 代理，CONNECT 返回 200、TLS 成功，随后 CNINFO 应用请求收到 504。
- 直连对照只在 CNINFO 专用子进程设置 `NO_PROXY=www.cninfo.com.cn`，实际 TCP 目标与 TLS hostname 均为 `www.cninfo.com.cn`。没有改系统代理设置、URL、请求头、速率、重试或许可配置。此次没有因 challenge 而切换路径。
- 本机还存在一个配置陷阱：无网络请求的客户端路由检查表明，仅设置上述 `NO_PROXY` 时，CNINFO **和 SSE** 都会从 `HTTPProxy` 变为 `ConnectionPool`，因为该环境变量影响了 Windows 系统代理的继承。直连探针因此只选择 CNINFO。后续多来源运行必须显式解析每个来源的有效路由，不能把这个诊断进程的环境设置直接当作全来源生产配置。
- 已证实的是客户端路由和本次返回的差异。504 究竟由哪个 CDN/回源环节形成、代理出口如何影响节点选择、是否有临时源站因素，仍缺少提供方证据；不据此更换官方端点或归因于某个代理软件。

相关实现位置：[RegistryBoundHttpTransport](../../src/analysis/acquisition/transport.py)、[runtime 组合根](../../src/analysis/acquisition/runtime.py)。

### CNINFO 空集合合同定位

用于离线复核的两份内容均为 discovery response，读取前重新验证磁盘长度与 SHA-256：

| 用途 | snapshot ID | SHA-256 | 字节数 |
| --- | --- | --- | ---: |
| bootstrap 唯一公司匹配 | `snapshot-9baaa814740f85083b8d7fa9` | `1d618cbaca6e89a5d0ccc0079937dadb327a09ff7a28d930c977f9ff41061ff6` | 591343 |
| 公告空集合形态 | `snapshot-ba8d2dd46c3bc479f5632c48` | `c2a890bbf3a6a53ab02ddc6c1794bf1c72ba45799fe9f59dcc5ba2cc18467114` | 165 |

第二份响应的 `announcements` 为 null，`totalAnnouncement`、`totalRecordNum`、`totalSecurities`、`totalpages` 都是整数 0，`hasMore` 是布尔 false。没有以正文中的提示、HTTP 200 本身或 MIME 正确来推断 `no_data`。

[CninfoAcquisitionAdapter._rows_and_page](../../src/analysis/acquisition/adapters/official.py) 在处理计数之前要求 `announcements` 是 list；离线重放原始已冻结字节，稳定产生 `ValueError: cninfo announcements schema mismatch`，随后被通用异常分类器记为 `response_parse_failed`。已有[空集合测试](../../tests/test_acquisition_cninfo_adapter.py)只覆盖 `announcements=[]`。

作为定位实验，仅在内存副本中将 null 换成空数组，未修改其他字段，既有 parser 即得到 `declared_total=normalized_total=0`、`terminal=true`。该实验不修改磁盘快照或数据库，不产生新 proof，不构成新的来源合同或线上验收。

### SSE 已知状态

新 SSE metadata 快照为 `snapshot-26a6915ea3c9cb97c668f399`，HTTP 200，有 terminal proof；它只验证本次 metadata 查询及空结果形态。

附件限制依据保留在既有 baseline `e3616697-2ce9-4bb6-9b46-a622d2577cd0`：attempt `a636091a-325f-49be-be12-4b4183e4cadf`、observation `d0195257-72fd-48a3-a56b-895fe41a803e`，从已批准 `www.sse.com.cn` 附件路径重定向至 `static.sse.com.cn` 后得到 HTTP 200 HTML 与明确 bot denial；其后 24 个 discovery 和 3 个 required fetch 零 I/O 终结。本轮沿用该限制证据，没有再次请求附件。

当前 DQBG-only 查询、重定向路径和保存边界依据[既有访问审批](source-access-policy-approval-2026-09-04.md)及[SSE v1.3 合同修订](source-access-policy-schema-amendment-2026-09-04.md)。没有证据支持在本轮更换 SSE endpoint、扩大查询类别或改变访问方式。若后续官方提供新的机器访问协议或许可依据，应单独审核、版本化；CNINFO 取得相同公告也不能直接解除 SSE 自身的 barrier。

## 建议的最小修复范围

### A. 显式记录并选择运行路由

建议在现有 composition root / transport factory 内增加可复现的来源路由配置，复用现有执行器、lease capability、allowlist 和 source gate：

- 在创建 run 时解析系统代理或显式配置，记录经脱敏的有效路由模式、配置版本/指纹及来源映射；凭据保留在配置或 secret 引用中，不写入 run metadata、日志或 Git。
- CNINFO 可以由操作者明确选择已验证的 direct 配置；其他来源的路由逐项确定。恢复同一个 run 时使用其冻结配置或对漂移明确拒绝，避免进程环境变化导致静默切换。
- 对 HTTP 响应与连接异常分别记录安全的阶段信息：是否取得 HTTP status、连接/TLS 是否完成、等待响应头的时间、异常类别。保留 30 秒请求上限及 120 秒 attempt deadline；本次证据不支持通过加长客户端 timeout 修复远端已经返回的 504。
- 任何来源的 restricted/login/paywall/challenge 都继续触发原有停止机制。路由选择不提供自动 failover，不以来源拒绝为触发条件切换出口或直连。
- 路由本身不改变 endpoint/schema，因此无需仅为环境诊断改写 v1.3 registry。若实施扩大来源访问合同，另按来源版本审批。

需要覆盖的测试包括：继承系统代理但环境变量为空、仅设置 NO_PROXY 的 Windows 行为、每来源路由隔离、run 配置冻结/恢复漂移、日志不泄露凭据、已有 challenge 路径零额外 I/O。测试应验证可观察行为，不依赖真实代理服务。

### B. 用新版本支持 CNINFO 的严格 null 空集合

建议新建 registry `1.4.0`，为 CNINFO 发布新的 definition（当前下一可用 minor 为 `1.3.0`），明确修订响应 schema、parser/schema version 和相应 execution key；SSE、SZSE、Moutai IR 原样引用现有定义。先完成协议修订审核，再启用实现；本报告不代替审批。

新 nullable 分支只适用于已通过 HTTP 状态、挑战检查、MIME/JSON 检查且匹配已冻结请求合同的响应，并要求：

- `announcements` 字段存在且为 null；`totalAnnouncement` 明确为整数 0（不是布尔值）。
- `hasMore` 明确为布尔 false；如果出现 `totalRecordNum`、`totalSecurities`、`totalpages` 等辅助计数，其类型和值也必须满足已登记的一致性约束。
- 将该形态规范化为零行；唯一 page 1 响应仍需冻结并生成完整 terminal proof，随后才能分类为 `no_data`。
- null 配正计数、缺少权威计数、矛盾分页、错误标记或不支持的类型继续失败，不把异常吞成空集；原有合法数组形态及非空分页继续按既有校验处理。

至少增加正例 null+明确零计数+hasMore=false，以及 null+正计数、缺计数、计数类型错误、hasMore=true/缺失、辅助计数冲突、受限 HTTP/挑战正文等负例。旧 CNINFO `1.2.0` 不得因共享 parser 变更而在 replay 时静默获得新语义；新增规则应由冻结 schema/version 显式启用。旧 run、快照和水位线保持不变，不默认宣称跨版本 checkpoint 兼容。

预期涉及的现有代码主要是 `acquisition/models.py`、`runtime.py`、`transport.py`、`orchestrator.py` 的配置传递与诊断元数据，以及 `adapters/official.py` 的版本化空集合解释；再加新 registry 和相关聚焦测试。当前没有证据要求另建采集内核、迁移旧数据库、调整 endpoint/请求类别、扩大许可或增加自动重试。

## 实施顺序与验收

1. 将上述两个事项形成下一轮明确的代码合同和 CNINFO schema 修订记录，确定新版本及路由配置的持久化方式。本轮不修改历史任务清单。
2. 在隔离分支实施，先运行 CNINFO adapter、transport、registry、orchestrator 的聚焦合同测试，再完成全量 pytest、来源 registry 校验、前端构建及 exact pushed commit CI；原始响应只用于忽略目录中的重放，Git 只保留合成的边界夹具。
3. 自动化通过后，用显式路由和新 registry 运行最小正式 metadata smoke，观察 bootstrap 绑定与空集合 terminal proof。空 metadata 成功仍不能证明附件可用。
4. 再按已批准范围决定是否启动全新 namespace 的 production baseline。每个适用来源都有兼容非空安全 checkpoint 后才执行两次合法 incremental；取得要求的内容样本和版本链后再进入人工黄金复核。

当前数据与验收边界保持明确：新诊断只有 2 个 smoke runs、5 个 discovery attempts、32 个 coverage/resolutions、3 个 discovery snapshots，content snapshots、production checkpoints、持久化 checkpoint barriers 和资源 fetch 都为 0。两个运行均自动 partial finalize、默认不可消费。一致性脚本通过；默认数据和两个既有试点的文件哈希未变。

业务采集 change 仍为 **78/81**，在线门和人工黄金门均为 **pending**。本轮定位了可实施的两项修复，并未完成 production incremental、取得 20 个内容黄金样本，或证明 SSE 附件访问已经恢复。
