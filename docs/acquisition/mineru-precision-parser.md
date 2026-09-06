# 公告 MinerU 精准解析

扫描公告解析器使用 [MinerU 精准解析 API](https://mineru.net/apiManage/docs) 的 v4 上传接口，显式指定 `vlm`，开启扫描识别、表格和公式。版本化配置为 `config/data_sources/announcement_parser.mineru.v1.json`。原 RapidOCR worker 与隐式 Tesseract 调用已经移除。

在 `.env.local` 配置 `MINERU_API`（也兼容 `MINERU_API_TOKEN`），或设置同名进程环境变量。程序不会执行 dotenv 内容；真实密钥、短期签名链接、原始文件、结果和数据库不得提交 Git。

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
python scripts/parse_announcement_mineru.py `
  --db <archive-db> --data-root <archive-data-root> `
  --snapshot-list <selection.json> --env-file <path-to-.env.local> `
  --output <local-report-directory>
```

选择文件包含 `namespace_id` 和非空、无重复的 `snapshot_ids`。只接受数据库中已经提交、许可允许派生与 LLM 处理、未隔离且哈希正确的 PDF。它们会上传到用户指定的 MinerU 服务；不通过解析服务重新抓取公告来源。单文件不得超过 200 MB 或 200 页，超限会要求拆分，不截断成部分成功。

程序先串行上传，再收集结果；服务可同时处理先前上传文件。`--submit-only` 仅提交，之后用同一命令省略该参数即可继续。常规恢复使用相同选择、数据库和配置：已提交任务继续查询，已完成任务逐项核验派生哈希后零网络复用。没有远端 ID 的不确定提交需要核查服务任务；程序不盲目重发。

MinerU 请求优先直连；发送前连接失败时可按用户配置切换 Clash `http://127.0.0.1:7897`。TLS 验证保持启用，Bearer 仅发送至固定 API 主机，上传/下载请求无 Bearer。认证失败、限流及额度不足会保留任务并停止后续提交；不切换到 Agent 轻量 API 或本地 OCR。巨潮采集的直连配置独立保留。

每个成功 snapshot 新增不可变结果 ZIP、Markdown、逐页布局 JSON、逐页阅读文本，记录原始哈希、服务返回版本、任务和输出哈希。布局必须完整覆盖原 PDF 页序，保留零基服务页码与一基 PDF 文件页码、原生文字、表格 HTML 和内容块。所有机器页均保留人工未核状态；未提供的置信度为 `null`。这些表格尚未成为经过验收的结构化财务事实。

私有任务状态和签名链接位于绑定 data root 的 `mineru-jobs/`，公开运行摘要只有脱敏状态；旧原始 PDF、派生、生产运行和人工签署不回写。一次解析不推进采集 checkpoint，也不要求重新运行 baseline/incremental。

实际服务可把 `vlm` 请求交给 `hybrid` 后端，程序分别保存请求模型、返回后端及 `_version_name`，不会把它写成另一种 API。返回的 `origin.pdf` 可能只改变 PDF 序列化；程序保留原公告字节，对不同字节的返回 PDF 逐页比较 72 DPI 渲染，并记录单独哈希及渲染版本。

网络中断后可先查询原任务。仅当它仍明确处于 `waiting-file` 时，`--retry-upload-once` 允许对同一签名地址补传一次，持久计数防止反复补传。`--network-route clash` 可显式选择用户本地代理；已被服务接收的文件直接继续查询与下载，不补传。

完成后生成阅读入口：

```powershell
python scripts/build_mineru_reading_index.py `
  --report <verified-completion-report.json> --announcement-index <original-index.csv> `
  --data-root <archive-data-root> --output <new-reading-directory>
```

阅读目录包括完整公告索引、逐页待复核队列，以及每份公告的 Markdown/图片、逐页文本和布局。旧阅读目录继续保留；新索引中的 `effective_parser` 明确区分原生文字与 MinerU 结果。
