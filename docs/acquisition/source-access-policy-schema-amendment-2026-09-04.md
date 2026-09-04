# 业务与商业模式 v1.3 SSE 响应合同修订

- 确认时间：`2026-09-04T09:50:30Z`（`2026-09-04 17:50:30+08:00`）。
- 确认人：`AstraValue project creator/owner (user attestation)`。
- 适用项目：AstraValue `business-model-acquisition-v1`。
- 变更性质：只修正已批准 SSE 查询的响应 schema 与空结果分页解释，不扩大访问端点、请求类别、保存权限、LLM 权限、速率或项目用途。
- 权限边界：继续受 [v1.2 项目内部访问策略审批](source-access-policy-approval-2026-09-04.md) 约束；该内部决定不是来源方授权或法律意见。

## 冻结证据锚点

以下仅记录 Git 忽略的隔离试点中两份 `resource_role=discovery_response` 快照的脱敏元数据；原始响应、数据库和本地绝对路径均不进入 Git。

| 用途 | snapshot ID | SHA-256 | 字节数 | 复核结果 |
| --- | --- | --- | ---: | --- |
| 合法空结果 | `snapshot-b5d0e77f2e3d3e0c3eb297b5` | `9fb3b061b324ecf4f06e612ab3eb3a99d7550801f7c922dbc1fe8b7ccf9e8452` | 584 | 重新读取文件计算的长度与完整 SHA-256 均匹配；`pageHelp.data=[]`、`total=0`、`pageCount=0`、`result=[]`。 |
| 非空结果 | `snapshot-c47510940b72a443718fe714` | `0e36382fa6439b3a26fef808fb92bb48fc5c220970a8bd22ae9d6090439370a5` | 10074 | 重新读取文件计算的长度与完整 SHA-256 均匹配；`pageHelp.data` 有 4 行、`total=4`、`pageCount=1`。 |

非空样本的记录同时存在 `TITLE` 与 `title` 键，但 `TITLE` 为 null，实际标题保存在小写 `title`；`URL` 与 `SSEDATE` 有值，`SSEDATE` 形状为 `YYYY-MM-DD`。因此 v1.2 将标题声明为 `pageHelp.data[].TITLE` 会把合法记录误判为缺少 canonical 字段。

## SSE disclosures v1.3.0

新 registry `1.3.0` 只为 `sse.disclosures` 创建 definition `1.3.0`，并作以下修订：

- `sse.periodic_report` 的 discovery schema 升为 `2`，标题路径改为 `pageHelp.data[].title`。
- parser 同时兼容已冻结旧响应的 `TITLE` 和现行响应的 `title`，但 v1.3 注册表以已观察到的有效字段为准。
- 上游声明 `pageCount=0,total=0,data=[]` 时，客户端收到并冻结的一次 page 1 响应可作为唯一 terminal proof；这里的 `pageCount=0` 表示零个结果页，不表示 HTTP 响应或审计 proof 不存在。
- 只有 schema 有效、`total=0`、规范化行数为 0、terminal 唯一且 proof 连续时才能形成 `no_data`。失败、挑战、受限、缺字段或不闭合响应仍不得转换为 `no_data`。
- query execution key 更新为 `sse.periodic_report.dqbg.v1.approved.v1.3`，definition 不声明兼容 v1.2 checkpoint；v1.3 必须创建新 baseline，旧 v1.2 partial run、barrier、snapshot 和 manifest 保持不变。

除上述字段外，SSE 的 GET endpoint、参数、initial/redirect allowlist、最大并发 1、最小间隔 5 秒、响应大小、deadline、保存/派生/LLM 策略及 DQBG-only 范围均与 v1.2 完全相同。巨潮、SZSE、贵州茅台 IR 和 legacy definitions 在 v1.3 registry 中原样引用既有版本；贵州茅台 IR 继续 `rejected/pending_policy/disabled`、空 endpoint、空 allowlist和零 I/O。

## 实施与验收边界

- v1.0、v1.1、v1.2 registry 文件不得修改，其 canonical hash 必须继续通过兼容测试。
- 测试夹具只保留上述形态的脱敏最小 JSON，不提交真实响应。
- 本修订不把 v1.2 试点误报的空结果直接改写为旧 run 的 `no_data`；只有新 v1.3 run 才按新合同生成新 proof、attempt、coverage 和 checkpoint。
- material gap 计数同时按物理查询时间片/重试工作身份区分，不能把同一 partition 的多个失败时间片压缩为一个缺口。
- 自动化、真实联网和人工黄金仍是三个独立门；单个 `600519` 试点不得称为全部 A 股验证或商业模式分析完成。
