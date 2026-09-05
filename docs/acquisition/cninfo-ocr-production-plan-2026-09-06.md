# 巨潮本地文本补全与生产运行方案

本方案执行用户 2026-09-06 的 OCR、历史复核、统一版本、基线及两次增量指令。原内部访问批准继续有效；技术配置变化不冒充新增人工签署。实际结果写入阶段日志。

1. 本地 OCR 使用独立 Python 3.12、RapidOCR 1.4.4 和随包中文 ONNX 模型，冻结依赖/模型 SHA-256。PyMuPDF 按 200 DPI 渲染完整页面，两个半页使用 20% 重叠保留边界文字；每个框按中心归属半页，保留原始各遍结果。文本不上传 OCR 服务。先抽样核对正文、数字/负号、表格、盖章和字形轮廓页，记录实际误差。处理 31 份全无文本 PDF 与 12 份局部无文本 PDF 的全部缺页；已有原生页保持原提取文本。产出页级布局、整篇布局与阅读文本，均绑定原 snapshot/hash/PDF 页码，不修改原 PDF 或旧派生。
2. 统一默认 registry 1.9.0 / CNINFO 1.8.0。保持巨潮 primary、SSE on_demand；原始/压缩/解压上限 128 MiB、attempt 600 秒、socket timeout 30 秒、直连且 TLS 验证、并发 1、间隔至少 5 秒、一次 attempt。失败保留，明确挑战停止当前任务后续来源请求，不用连续新 run 绕过。
3. 新字段 `content_validator_compatible_from_versions` 声明 CNINFO 1.4.0–1.7.0 的正文条件复用。这些版本的附件 canonical、上游身份与发布时间解释相同，差异为发现分类、采集角色和本地读取预算；代码仍逐条验证合同、确切 URL、原始/当前许可及哈希/隔离/namespace。未声明旧版本不可用。只复用成功观测的 ETag/Last-Modified；每次仍发合法条件请求。304 新建当前版本 observation 并引用旧 snapshot，200 记录完整字节 SHA-256/长度；相同哈希复用旧 snapshot，新哈希创建当前定义 snapshot，并用新 observation 的 validator snapshot/version 指向跨定义前驱。替换内容保留原发布日期/精度，但 available_at 按 retrieved_at，不能回填为旧日期。兼容旧正文不迁移 checkpoint、不修改旧 run_kind，不把来源镜像合并。
4. 生产库选择已绑定的 `var/pilots/cninfo-content-archive-20260905/runtime/analysis.db` 与同级 `data/`。先用 SQLite backup 保存一致性备份、旧行摘要和原始 SHA 清单；旧历史审计库与项目默认主库不写入。公司锚点取招股公开日 2001-07-26、上市日 2001-08-27，依据上市公告 snapshot-354a16aae8838234bd323db8 的 PDF 第 1–2 页；招股签署日 2001-07-24 单独保留，不作为公开日期。完整 profile 与锚点依据另存本地审计清单。新 production baseline 由正式 planner 从来源时区历史日界重新执行全量 HTTP discovery；禁止 retained_inventory 进入 production。先核对实际条件响应与新观测谱系，再继续同一可恢复 baseline，不截断计划或静默重试终态失败。
5. 仅在适用主采安全 checkpoint 成立后连续执行两次独立 production incremental。记录 run ID、冻结版本、14 天 overlap、HTTP proof、unchanged/新增/变化/失败、watermark 与 final event；默认查询不得悄悄收窄。外部故障保留缺口并报告，不能凑无效增量次数。
6. 历史复核单列两份 HTML 类型冲突、报告期覆盖与早期季报制度依据。未找到权威依据时保持待核，不能由空查询推断未披露。OCR 数字、表格行列与签章只供定位回看，不直接写财务事实。自动化、真实联网、人工黄金三门分别报告；10.2/10.3 仍须独立人工核对与签署。

原始公告、OCR 文本、页图、数据库、备份及运行报告仅写入 Git 忽略的本地目录。提交代码与合同，不合并 main。
