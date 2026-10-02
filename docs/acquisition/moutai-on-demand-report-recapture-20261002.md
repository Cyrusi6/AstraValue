# 贵州茅台按需报告正文回采（2026-10-02）

本次只选择贵州茅台 2025 年年度报告 `cninfo:1225114741`，没有扩展为报告归档或全量正文下载。目录来自已核验的 CNINFO discovery inventory；目录复用本身没有网络 I/O，正文通过当前注册来源 `cninfo.disclosures@1.10.0` 在新空 acquisition 根中真实请求。

## 结果

- URL：`https://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF`
- HTTP：`200`
- fetch：`success / content_new`
- snapshot：`snapshot-4d5d67dd7d28143dfb93e9dd`
- 原文 SHA-256：`474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288`
- 文件大小：`1,082,847` bytes
- 解析：`143` 页、`157,207` 字符，`empty_page_count=0`
- material：`periodic_report`，`title_body_agree`
- acquisition run：`c8635ea3-8663-4967-a8e6-e58688d86c16`
- 生产 checkpoint：未推进；运行范围为 `ad_hoc_retained_inventory`

同一目录和运行再次执行后，`acquisition_runs=1`、`acquisition_attempts=2`、`resource_observations=1`、`raw_resource_snapshots=2`、`derived_artifacts=2` 均未增加；正文仍引用同一 `snapshot_id` 和 SHA-256。目录载入 attempt 明确记录 `io_performed=false`，正文 fetch attempt 为 `content_new`。完整机器证据保存在 `tmp/moutai-on-demand/repeat-audit.json`，该目录不纳入 Git。

## 边界

这证明一份明确选中的报告能够通过当前按需 acquisition 链真实采集、解析并在重复请求时复用快照。它不证明全部报告、所有来源或贵州茅台八步报告已经完成；报告仍需研究工作区显式引用后，经 reporting bridge 生成 `ReportVersion` 并由用户完成人工阅读验收。
