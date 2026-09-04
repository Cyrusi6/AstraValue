# 业务与商业模式 v1.2 项目内部访问策略审批

- 审批时间：`2026-09-04T07:57:25Z`（`2026-09-04 15:57:25+08:00`）。
- 复核与批准人：`AstraValue project creator/owner (user attestation)`。
- 适用项目：AstraValue `business-model-acquisition-v1`。
- 用途：个人、本地、低频、非商业的 A 股研究与可审计证据归档。
- 性质：这是项目负责人对 AstraValue 内部运行范围的访问策略审批，不是来源方授权、法律意见或对网站条款的重新解释。

## 共同批准范围与强制限制

项目负责人批准巨潮资讯、上海证券交易所、深圳证券交易所下述公开官方端点的自动访问，并批准在本地保存合法取得的原始响应与附件、生成派生文本，以及通过冻结的 `EvidenceSnapshotManifest` 交给 Codex/LLM 处理。所有处理同时受以下限制：

- 每个来源最大并发为 1；同一来源相邻请求至少间隔 5 秒。
- 单请求超时 30 秒、attempt deadline 120 秒；默认不自动重试。响应、压缩数据和解压数据上限分别为 64 MiB、64 MiB 和 128 MiB。
- 仅使用本文件列出的 HTTPS 域名、路径、请求编码、固定安全请求头和逐跳重定向范围；新网站或越界路径只能形成待人工审核的 source candidate。
- 禁止出售、对外再分发或构建商业数据服务；原始响应、附件、派生文本、数据库、令牌和生成报告不得写入 Git。
- 遇到登录、验证码、付费墙、JavaScript challenge、401/403、明确 robots/条款限制或其他许可冲突时立即停止，记录规范状态，不提交凭据、不规避限制、不切换未批准镜像。
- 来源协议、域名、响应 schema、条款或项目用途发生实质变化时，当前批准不自动延续；必须创建更高版本的 registry/definition 并重新审核。

公开依据继续采用 [v1.1 技术审核记录](source-access-policy-review-2026-09-04.md) 中逐项列出的官方网站、法律声明和页面脚本。本次审批解决的是项目内部处理权限边界；正式 runtime 仍必须用真实响应验证协议，任何不一致均记为失败或受限，不能转成 `no_data`。

## CNINFO disclosures

批准 `cninfo.disclosures@1.2.0`，范围如下：

- 初始请求：`www.cninfo.com.cn/new/data/szse_stock.json`、`www.cninfo.com.cn/new/hisAnnouncement/query`、`static.cninfo.com.cn/finalpage/`。
- 逐跳重定向：只允许上述三个域名与路径族，最多 1 跳，禁止 HTTPS 降级。
- 协议：bootstrap 使用 GET；公告查询使用 POST form。公告参数固定为 `stock/tabName/pageSize/pageNum/column/category/plate/seDate/searchkey/secid/trade/sortName/sortType/isHLtitle`。
- 公司参数：必须先归档并解析 bootstrap 的 `stockList`，按 ticker 唯一取得 `orgId`，再形成 `stock={ticker},{orgId}`；不得硬编码 `600519` 的 orgId。
- 响应：bootstrap 根字段为 `stockList`；公告根字段为 `announcements` 与 `totalAnnouncement`。公告 canonical ID 使用 `announcementId`，附件仅限 `static.cninfo.com.cn/finalpage/`。
- 时间：`announcementTime` 按 epoch milliseconds 解析为 instant；无法验证的时间不得提升精度。
- 查询：招股书类别 `category_scgkfx_szsh;`；定期报告类别为年报、半年报、一季报和三季报四类已登记代码；业务公告使用公开全公告范围。协议不匹配时停止并形成 barrier。

依据链接：<https://www.cninfo.com.cn/new/index>、<https://www.cninfo.com.cn/new/data/szse_stock.json>、<https://www.cninfo.com.cn/new/hisAnnouncement/query>。

## SSE disclosures

批准 `sse.disclosures@1.2.0` 的已确认定期报告查询，范围如下：

- 初始请求：`query.sse.com.cn/security/stock/queryCompanyStatementNew.do`；附件只允许 `www.sse.com.cn/disclosure/listedinfo/announcement/` 和 `static.sse.com.cn/disclosure/listedinfo/announcement/`。
- 逐跳重定向：只允许上述两个附件路径族，最多 1 跳，禁止 HTTPS 降级。
- 协议：GET query；固定 `productId/beginDate/endDate/reportType2/reportType/pageHelp.*` 等已登记参数，`reportType2=DQBG`。
- 响应：根字段 `pageHelp`，至少验证 `data/pageCount/total`；记录使用 `URL/TITLE/SSEDATE`，canonical ID 使用规范化附件文件名。
- 时间：`SSEDATE` 仅有日期，按 Asia/Shanghai 下一日边界计算保守 PIT。
- 范围限制：本版本不猜测招股书和一般公告分类，不为这些未确认查询发起 I/O；后续新增必须发布新 definition 版本。
- 访问限制：附件若返回 challenge、HTML 验证页、401/403 或非预期 MIME，立即记录 `restricted` 或对应失败状态，不重试绕过。

依据链接：<https://www.sse.com.cn/assortment/stock/list/info/announcement/>、<https://www.sse.com.cn/home/legal/>、<https://query.sse.com.cn/security/stock/queryCompanyStatementNew.do>。

## SZSE disclosures

批准 `szse.disclosures@1.2.0` 的已确认招股书与定期报告分类，范围如下：

- 初始请求：`www.szse.cn/api/disc/announcement/annList`、`disc.static.szse.cn/download/`。
- 逐跳重定向：只允许上述 API 与附件路径族，最多 1 跳，禁止 HTTPS 降级。
- 协议：POST JSON；`stock/channelCode/seDate/bigCategoryId` 必须为数组，`pageSize/pageNum` 为数字。
- 已批准分类：`0102`（首次公开发行及上市）、`010301`（年报）、`010303`（半年报）、`010305`（一季报）、`010307`（三季报）。一般业务公告分类尚未闭合，本版本不得猜测。
- 响应：根字段 `data/announceCount`；`announceCount` 允许数字或数字字符串；记录使用 `annId/title/attachPath/publishTime`。
- 时间：带本地时分秒的 `publishTime` 按 Asia/Shanghai instant 解析；仅有日期时降级到下一本地日界。
- smoke：`300750` 只执行已登记的年报 metadata probe，不下载附件、不推进 production checkpoint。

依据链接：<https://www.szse.cn/disclosure/listed/notice/index.html>、<https://www.szse.cn/application/laws/index.html>、<https://www.szse.cn/api/disc/announcement/annList>。

## Moutai IR

`moutai.ir@1.2.0` 不在本次联网批准范围内，继续保持 `live_access_review=rejected`、`pending_policy/disabled`、空 endpoint 与空 allowlist。原因是页面为 HTML 分页，稳定机器协议、归档/派生/LLM 条款及年龄确认边界仍未闭合。AstraValue 不得访问、提交年龄确认或用其他站点替代；后续启用必须创建新的 registry/definition 版本并重新签署。

## 签署结论

项目负责人已确认上述用途、三个官方来源的精确批准范围、保存与派生权限、Codex/LLM 处理权限、速率和大小限制，以及登录/验证码/付费/挑战/许可冲突时立即停止的边界。批准只允许通过 AstraValue 正式 acquisition runtime 执行；临时脚本或任意 URL 抓取不属于批准范围。
