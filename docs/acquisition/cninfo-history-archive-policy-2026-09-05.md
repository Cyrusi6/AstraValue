# 巨潮历史正文兼容与归档策略修订

依据：项目负责人 2026-09-05 明确要求先修完整读取、历史 HTML、招股分类，再固化巨潮主采与上交所按需补缺，随后分批归档现有公告目录。该指令延续既有个人、本地、低频、非商业研究内部策略，不是来源方授权。

## HTML 兼容版本

registry 1.5.0 / CNINFO definition 1.4.0 / announcements schema 3 仅新增已登记 `/finalpage/` 下目录返回的 `.html|.htm` 正文格式识别。PDF 仍要求 PDF MIME；HTML 要求 HTML MIME、完整页面结构与有效编码。HTTP 状态和窄化挑战信号先于格式检查。旧 schema 1/2 的解释、旧配置哈希及旧运行保持不变。

原始正文归档和完整 SHA-256/长度复核后，才从 snapshot ID 提取文本，保存独立派生版本和输出哈希。历史中文兼容 UTF-8、GB2312/GBK/GB18030 与 Big5，声明编码不支持或字节非法须显式失败，不用替换字符掩盖乱码。解析不运行脚本、不获取子资源。

网络仍直连、TLS 验证、同来源并发 1、最小间隔 5 秒、无自动重试。遇到明确拦截保留状态并及时报告，同运行该来源后续零 I/O；独立修复工作继续。新版本不自动迁移旧 checkpoint 或清除旧失败。

文本提取器 1.1.0 排除 HTML head 元数据并严格验证整份页面编码；材料分类器 1.6.0 保留实际封面与网站重复标题的差异，不把目录中的“财务数据摘要”误判成整份报告摘要，并把权证、债券上市材料与首次上市公告分开。明确的会议资料、决议或议案标题优先于其后出现的报告、招股书等议题引用。报告之后的董事会真实性声明不构成公告标题；英文年报以明确的 ANNUAL REPORT 加年份及摘要修饰语判别。年报工作制度/规程归其他公告，网上集体或业绩说明会归报告相关公告，不计入报告正文。既有派生版本保留。

本文件定义授权范围与版本语义。真实样本结果和三类验收门另记阶段日志；元数据遍历、正文归档、生产增量和人工黄金验收分别说明。

## 首发分类与正式主采版本

registry 1.6.0 / CNINFO 1.5.0 使用公开检索页直接引用的 history-notice.js 中“首发”类别 `category_sf_szsh`；客户端用分号连接多项，单项没有尾部分号。旧 `category_scgkfx_szsh;` 不作为新协议。检索结果仍是公告目录；招股说明书、附录、上市公告书和发行公告分别以标题和正文前部确定材料类型，分歧保留人工审查状态。

registry 1.7.0 / CNINFO 1.6.0 明确 `collection_role=primary`；SSE 1.4.0 明确 `collection_role=on_demand`、`supplements_source_id=cninfo.disclosures`。角色变化升级来源定义，旧定义与旧 checkpoint 不动。默认 SSE 零 I/O 并保留静态覆盖，显式 smoke 仍可检查其可达性。按需补缺必须引用已终结主采运行的实际 required 缺口，目标市场及 query family 必须受当前补充协议支持。SSE 目前只有定期报告 DQBG，不能承诺补齐任意公告或招股材料。补充来源成功不会清除巨潮原屏障；默认 incremental 仍需要主采的兼容安全 checkpoint。

## 本地目录分批归档

`export_cninfo_inventory` 仅读原始审计库，核验公司、namespace、finalized run、原始响应哈希与长度、proof/observation/snapshot 关系和公告行。输出自包含的冻结目录及原始支持字节。新运行使用独立的 `retained_inventory` proof，无 HTTP 状态、不伪造新来源查询时间；原 namespace/run/proof/snapshot/row locator 作为 origin 引用保留。每批最多 100 条且仅为 ad_hoc，不推进生产 checkpoint。

先定期报告及更正、招股材料，再其余公告。每条正文单独记录 fetch、原始 SHA-256/长度、文本提取及类型确认结果；扫描件没有文本层时明确留待 OCR/审查。正常失败保留并继续其他资源，恢复不默认重试终态失败；明确 challenge 或 HTTP 403 停止整项归档的后续批次，及时讨论解决。

运行示例（目录输入先经只读导出验证）：

```powershell
python scripts/archive_cninfo_inventory.py --inventory <verified-inventory.json> `
  --db <archive-analysis.db> --data-root <archive-data> --output-dir <archive-results> `
  --batch-size 75 --prepare-only
# 去掉 --prepare-only 执行或恢复；--report-only 读取当前明确状态。
# --derive-only 仅刷新文本/分类派生版本，不重新下载；旧派生和旧缓存投影保留。
```

所有运行文件保持 ignored。本轮目录覆盖的总数不等于公司历史绝对完整，归档完成也不代替 production baseline、两次 incremental 或人工黄金验收。
