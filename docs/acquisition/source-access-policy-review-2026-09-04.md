# 业务与商业模式 v1 来源访问策略审核

- 审核完成时间：`2026-09-04T03:18:22Z`（`2026-09-04 11:18:22+08:00`）。
- 技术复核执行者：OpenAI Codex 辅助技术审核；本记录不是法律意见，也不构成人工法律签署。
- 审核范围：只读检查公开官方网站、页面脚本、`robots.txt`、响应元数据，以及不改变上游状态的 GET/HEAD 观测。未执行登录、验证码、付费墙、表单提交、生产采集运行或访问控制绕过。
- 判定原则：页面公开、`robots.txt` 为空或不存在、端点技术上可访问，均不当然授予自动采集、归档、派生文本、再分发或 LLM 处理权利。
- 版本结果：registry `1.0.0` 及其中全部 `1.0.0` definition 保持不变；registry `1.1.0` 记录本次完整评估。checklist 标为 completed 只表示该项已经评估，不表示该项获准。

## 审核结论摘要

| 来源 | 审核结论 | 联网策略 | 原始/发现响应保留 | 派生文本 | LLM 处理 | 主要原因 |
| --- | --- | --- | --- | --- | --- | --- |
| `cninfo.disclosures@1.1.0` | `rejected` | `pending_policy/disabled` | `pending` / `forbidden` | `pending` | `pending` | 公告请求与分页合同已偏离现行官方脚本，且未确认自动使用和保留许可。 |
| `sse.disclosures@1.1.0` | `rejected` | `pending_policy/disabled` | `pending` / `forbidden` | `pending` | `pending` | 旧参数可产生 HTTP 200 假空集；附件重定向到未批准静态域名后出现挑战响应；批量自动化与 LLM 权利未确认。 |
| `szse.disclosures@1.1.0` | `rejected` | `pending_policy/disabled` | 附件仅有内部非商业保留依据；发现响应禁止保留 | `pending` | `pending` | 法律声明允许非商业浏览、下载，但真实 POST schema、批量自动访问、派生文本和 LLM 权利仍未确认。 |
| `moutai.ir@1.1.0` | `rejected` | `pending_policy/disabled` | `pending` / `forbidden` | `pending` | `pending` | 实际 IR 页面为服务端 HTML，而非 adapter 假定的 JSON schema；未找到明确的自动化、归档、派生或 LLM 许可。 |

因此，当前 registry 中四个来源均为零 I/O；真实联网样本门继续为 `pending`。上述决定不得把失败、受限或无法确认的查询转换为 `no_data`。

## CNINFO (`cninfo.disclosures`)

### 官方依据

- 平台身份、版权与免责声明：<https://www.cninfo.com.cn/new/index>
- `robots.txt`：<https://www.cninfo.com.cn/robots.txt>（观测为 `404`；不存在不等于许可）
- 股票 bootstrap JSON：<https://www.cninfo.com.cn/new/data/szse_stock.json>
- 现行公告页面脚本：<https://static.cninfo.com.cn/new/assets/js/app/data/person-stock-news.js?v=20260710082532>
- 现行公告详情脚本：<https://static.cninfo.com.cn/new/assets/js/disclosure/notice-detail.js?v=20260710082532>
- 独立的官方数据服务/权限渠道：<https://webapi.cninfo.com.cn/>

### 已评估的协议

- 股票 bootstrap 返回 `application/json`，无重定向，根字段为 `stockList`；行字段包括 `code`、`pinyin`、`category`、`orgId` 和 `zwjc`，响应提供 `ETag` 与 `Last-Modified`。
- 官方公告脚本向 `/new/hisAnnouncement/query` 提交 `stock`、`tabName`、`pageSize`、`pageNum`、`column`、`category`、`plate`、`seDate`、`searchkey`、`secid`、`sortName`、`sortType`、`isHLtitle`，并读取 `announcements` 与 `totalAnnouncement`。
- Registry `1.0.0` 建模的是 `ticker/start_date/end_date/category` 和 `totalRecordNum`。该合同可能发出错误查询或错误结束分页，不能安全执行。
- 公告详情脚本依据 `adjunctUrl` 在 `static.cninfo.com.cn` 构造附件地址；附件抓取属于新的 initial request。旧 initial allowlist 未包含该域名，而 redirect allowlist 的 `/` 又过宽。
- 本次没有通过真实公告 POST 验证 `announcementTime`；其单位、时区和稳定发布时间含义均未闭合，因此审核后的 definition 不声明瞬时精度。
- 网站能确认运营方、版权方并提供免责声明，但审核到的官方材料没有授权批量自动访问、长期保存响应/原文、生成派生文本或交给 LLM。独立数据服务平台涉及账号、API 权限和付费访问，不能作为公共页面端点的授权依据。

### 再次审批前置条件

取得明确许可依据；用最小、获准的公告 POST 验证类别代码；冻结准确的参数、schema 和时间语义；initial allowlist 只纳入经批准的 `www.cninfo.com.cn/new/data/szse_stock.json`、`www.cninfo.com.cn/new/hisAnnouncement/query` 与经核验的 `static.cninfo.com.cn/finalpage/` 附件路径，并逐跳审批重定向。在此之前，不启用任何 endpoint 或 allowlist。

## Shanghai Stock Exchange (`sse.disclosures`)

### 官方依据

- 上市公司公告页：<https://www.sse.com.cn/assortment/stock/list/info/announcement/>
- 法律声明：<https://www.sse.com.cn/home/legal/>
- 现行查询脚本：<https://www.sse.com.cn/xhtml/home/public/querySearch/searchJ.js>
- `robots.txt`：<https://www.sse.com.cn/robots.txt>（观测为 `404`；不存在不等于许可）
- 前端引用的现行查询端点：<https://query.sse.com.cn/security/stock/queryCompanyStatementNew.do>
- 旧流程中仍存在的兼容端点：<https://query.sse.com.cn/security/stock/queryCompanyBulletin.do>
- 观测到的附件重定向路径族：<https://static.sse.com.cn/disclosure/listedinfo/announcement/>

### 已评估的协议

- 向 `queryCompanyBulletin.do` 发送 registry `1.0.0` 的参数名时，服务虽返回 HTTP 200，却将其解释为 `productId=None`、空日期和空报告类型，并返回 `pageCount/total=0`。若按成功空结果处理，会制造假 `no_data`。
- 现行前端使用 `productId`、`beginDate`、`endDate`、`reportType2`、`reportType` 与 `pageHelp.*` 分页字段。已确认 `DQBG` 对应定期报告；招股书和一般业务公告的精确分类值未确认。
- 成功 JSON 响应为 `application/json;charset=UTF-8`，根为 `pageHelp`，包含 `data`、`pageCount`、`pageNo`、`pageSize`、`total`、`beginPage`、`endPage`、`cacheSize`；记录包含 `URL`、`TITLE`、`SSEDATE`、`ADDDATE`、`SECURITY_NAME` 和 `BULLETIN_TYPE`。
- `SSEDATE` 只有日期。PIT 继续采用 Asia/Shanghai 下一日边界；未验证字段不得提升为 instant。
- 一个 `www.sse.com.cn/disclosure/...` PDF 地址重定向到 `static.sse.com.cn`。对后者的一次受限请求得到 HTML/JavaScript challenge 而非 PDF，审核随即停止。该结果属于 `restricted`，不是 `no_data`，且未归档正文。
- 法律声明允许非商业浏览、下载，同时限制未经许可的营利性复制、下载、存储、电子抓取或传播；它没有明确授权定时批量 API、完整历史采集、长期批量归档、派生文本或第三方 LLM 处理。

### 再次审批前置条件

确认每个注册查询对应的官方分类值；更新现行 endpoint 与参数构造；以最小获准样本验证分页终止；明确审批 `www.sse.com.cn` 到 `static.sse.com.cn` 的重定向与路径族；取得预期访问量、归档、派生和模型处理的许可依据。遇到 challenge、登录、401/403 或许可限制必须停止且不得重试绕过。

## Shenzhen Stock Exchange (`szse.disclosures`)

### 官方依据

- 官方首页：<https://www.szse.cn/index/index.html>
- 上市公司公告页：<https://www.szse.cn/disclosure/listed/notice/index.html>
- 法律声明：<https://www.szse.cn/application/laws/index.html>
- `robots.txt`：<https://www.szse.cn/robots.txt>（观测为 `200 text/plain` 空响应；空响应不等于许可）
- 官方页面脚本引用的端点：<https://www.szse.cn/api/disc/announcement/annList>
- 官方附件路径族：<https://disc.static.szse.cn/download/>

### 已评估的协议

- 现行前端合同使用 JSON 字段 `pageSize`、`pageNum`、`stock`（数组）、`channelCode`（数组）、`seDate`（起止日期数组）、`bigCategoryId` 与可选 `plateCode`；registry `1.0.0` 的 `ticker/start_date/end_date/category` form body 不是实际 wire contract。
- 已观测的官方分类 ID 包括 `0102`（首次公开发行及上市）、`010301`（年报）、`010303`（半年报）、`010305`（一季报）和 `010307`（三季报）；一般业务公告分类集合仍未闭合。
- 相邻官方 detail 响应包含根字段 `companyCount`、`announceCount`、`disclosureTip`、`recordCount`、`defaultValue` 和 `data`；`announceCount` 可能为数字字符串。记录字段包括 `annId/id/title/attachPath/attachFormat/attachSize/publishTime`。
- 观测到的 `publishTime` 含本地秒和小数部分。由于本次只读审核没有提交 `annList` POST，审核后的 definition 保留保守日期精度并明确记录精度损失，不声称已经验证 instant 合同。
- 一个页面直接链接的附件在 `disc.static.szse.cn/download/` 返回 `200 application/pdf`，无重定向，且提供 `Content-Length`、`ETag` 和 `Last-Modified`。
- 法律声明称机构或个人可在非商业目的下浏览、下载，并限制未经许可的营利用途。这为人工取得的官方附件提供了内部非商业保留依据，但不得出售或再分发；它没有确认批量自动 API、完整 discovery 响应保留、派生文本、商业再利用或 LLM 传输/处理。

### 再次审批前置条件

以获准的最小 POST 验证 `annList`；实现 JSON 而非 form 请求；只映射已确认的类别 ID；明确数字字符串规范化和时间戳解析；取得自动化、派生文本和 LLM 处理许可。initial request 集合需同时包含精确 API 路径和已核验的 `disc.static.szse.cn/download/` 附件路径；每个重定向仍需逐跳审批。

## Guizhou Moutai Investor Relations (`moutai.ir`)

### 官方依据

- 公司公告：<https://www.moutaichina.com/mtgf/tzzgx/gsgg/index.html>
- 观测到的第二页：<https://www.moutaichina.com/mtgf/tzzgx/gsgg/d3f80281-2.html>
- 财务报告：<https://www.moutaichina.com/mtgf/tzzgx/cwbg/index.html>
- 投资者关系交流：<https://www.moutaichina.com/mtgf/tzzgx/tzzgxtx/index.html>
- `robots.txt` 候选：<https://www.moutaichina.com/robots.txt>（浏览器客户端在收到源站响应前阻止了请求，因此不提供任何许可信号）

### 已评估的协议

- 公开公司公告页无需登录即可加载；页面每页渲染 15 条记录，显示总数、页数、PDF 链接和仅日期标签。分页使用 `d3f80281-2.html` 一类的独立 HTML 路径。
- 观测到的 PDF 路径族包括 `/mtgf/articleFileDir/YYYY-MM/DD/*.pdf`；公开搜索可见的官方结果还出现 `/mtgf/attachDir/...`。本次没有批准或冻结任何附件请求、正文或重定向链。
- 页脚声明贵州茅台酒股份有限公司拥有版权。审核到的公开声明没有明确授予自动遍历、归档、派生文本、再分发或 LLM 处理权利。
- 当前 `moutai_ir` adapter 假定 JSON 对象含 `items`、`total`、`has_more` 和 `next_cursor`；实际观测到的是 HTML，无法证明该 schema。启用现有 adapter 可能导致解析失败或假覆盖。
- 主站显示年龄确认提示。本次没有提交确认，也没有绕过年龄门、登录、挑战或其他限制。

### 再次审批前置条件

取得明确许可依据；确认稳定的官方机器可读接口或严格限定的 HTML 合同；验证准确分页、附件路径和逐跳重定向；再创建新的 definition 版本。在许可、访问合同和完整 checklist 全部闭合前，`moutai.ir` 保持 `pending_policy/disabled`，没有 endpoint 或 allowlist。

## 后续获批版本的本地安全上限

以下是项目侧保守上限，不是上游配额或许可授予：

- 每来源并发 1，并使用 workspace 级共享来源门禁；
- 同一来源请求之间至少间隔 5 秒；
- 默认只尝试 1 次；未来如批准重试，只能针对幂等网络错误、429 或 5xx，且必须遵守 `Retry-After`；
- 请求超时 30 秒、attempt deadline 120 秒、响应/压缩上限 64 MiB、解压上限 128 MiB，最多 1 次逐跳单独批准的重定向；
- 401、403、登录、验证码、JavaScript challenge、付费墙或许可限制不得重试，必须记录规范状态并停止；
- 本文记录的候选 endpoint/path 不属于生效 allowlist。

## 后续边界

下一步应先解决许可与协议缺口，不应启动 baseline。任何批准都必须使用 registry `1.2.0` 或更高版本以及新的 source-definition 版本；registry `1.1.0` 必须保持不可变。本审核不验证全部 A 股，也不完成真实联网门或人工黄金样本门。
