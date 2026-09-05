# 巨潮直连与 nullable 空结果修订

- 日期：2026-09-05；技术复核记录时间 `2026-09-05T06:58:26Z`。
- 依据：项目负责人本轮明确要求“优先把巨潮的直连和空结果兼容修好，通过巨潮获取其公开提供的公告正文”，失败及时报告讨论，不能攻击来源。
- 本记录描述项目内部修复授权，继续沿用[既有个人、本地、低频、非商业研究用途](source-access-policy-approval-2026-09-04.md)；不声称取得来源方新增授权。

## 本轮处理方式

正式采集客户端自建连接统一直连，显式禁用环境与 Windows 系统代理继承，保持 TLS 验证。新 run 保存 `http_route_policy=direct-v1`；恢复旧未完成且路由未知的 run 不得静默切换，需创建新 run。旧已完成结果及离线快照仍可读取。本轮不修改系统代理或其他应用的网络配置。

优先通过注册表中巨潮的既有公开接口和返回的附件链接取数。保留最大并发 1、同来源间隔 5 秒、单请求 30 秒、attempt 120 秒和默认无自动重试。失败/受限必须保留原始规范状态并及时向操作者报告，继续其他未受影响且已批准的工作；无法取得正文时讨论下一步。受限路径的自动请求暂停不代表结束整个修复任务，也不授权攻击、漏洞利用、流量冲击或自动挑战规避。

## 新版本与解释规则

registry `1.4.0` 仅将 `cninfo.disclosures` 升为 `1.3.0`；公告 `cninfo.announcements` schema 升为 `2`，查询 execution key 升版。bootstrap schema 1、全部 endpoint/参数/请求头/allowlist、保存与 LLM 权限保持原范围。其他来源和 legacy definitions 原样引用，全部旧 registry 文件保持不可变；新 definition 不声明旧 checkpoint 兼容。

依据[已完成诊断](source-availability-diagnosis-2026-09-05.md)中已冻结的 165 字节响应：snapshot `snapshot-ba8d2dd46c3bc479f5632c48`，SHA-256 `c2a890bbf3a6a53ab02ddc6c1794bf1c72ba45799fe9f59dcc5ba2cc18467114`。本轮只读复核长度和磁盘哈希匹配，原始响应仍在 Git 忽略目录。

schema 2 的 nullable 分支要求：

- 唯一 page 1、无 cursor，权威计数路径固定为 `totalAnnouncement`。
- `announcements` 存在且为 null；`totalAnnouncement` 是严格整数 0，`hasMore` 是布尔 false。
- `totalRecordNum`、`totalSecurities`、`totalpages` 若出现，均为严格整数 0，布尔值/字符串/浮点数不接受。
- `classifiedAnnouncements`、`categoryList` 若出现须为 null；以上八个字段以外的字段、错误标记或其他形态继续拒绝。
- 先通过 HTTP 状态/挑战/MIME/JSON 检查，再冻结原始字节、生成零行且闭合的 terminal proof，最后才能形成 no_data。

旧 schema 1 对 null 仍报解析失败；合法数组形态继续既有语义。不回写旧 run、snapshot、manifest 或 checkpoint。

## 验收边界

自动化正负例和旧版本重放先通过，再在全新 namespace 运行正式 CNINFO smoke 与有限窗口 ad_hoc 正文采集。真实 PDF 要核对 MIME、文件结构、长度、SHA-256、snapshot/provenance 与可解析性。失败须及时报告，不能把空 metadata 成功称为正文取得。少量正文或单家公司验证不替代全历史、两次生产 incremental 或人工黄金验收；实际证据追加到阶段日志。
