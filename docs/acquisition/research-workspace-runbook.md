# 公司研究工具运行说明

当前入口复用既有轻量包、证据读取器、估值引擎、ReportVersion 和导出器。公司身份、数据位置和状态目录由部署配置 `config/research_workspace.json` 管理，模型调用不传供应商或目录。当前行业画像支持已登记的七家白酒公司；其他公司返回画像能力缺口，尚不代表全部 A 股可用。

## 启动与读取

```powershell
Set-Location 'D:/估值模型-worktrees/fact-materialization-ultra'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH=(Resolve-Path src).Path
python -m analysis.research.cli prepare_research --arguments '{"company":"贵州茅台","as_of":"2026-09-13"}'
```

也可仅传 `company`；`latest` 固定为调用当天的日期。现阶段此路径从已登记投影构建，缺失投影返回 `acquisition_required`；冷启动自动采集、当天目录更新尚未接通，不能把缓存首包称作当天资料完整。

返回的 `research_id` 用于后续工具。`content` 是可读核心包；`coverage.required_core`、研究和排版状态分开。历史包哈希不修改，现行模型自主估值规则通过 `research_policy` 提供。Token 数明确只计冻结核心包，策略与返回结构另计。

```powershell
python -m analysis.research.cli query_research --arguments '{"research_id":"r_9fb83be3097c6af02aa3ea4d","topic":"cash_flow_quality","period_type":"cumulative","page_size":20}'
python -m analysis.research.cli read_evidence --arguments '{"research_id":"r_9fb83be3097c6af02aa3ea4d","evidence_id":"document-evidence-ffd7af8fec07029184bcd7c4","max_tokens":2000}'
python -m analysis.research.cli search_knowledge --arguments '{"question":"现金流"}'
```

分页通过 `next_page` 继续；未知指标和画像返回能力缺口，原件哈希验证失败则拒绝消费。知识检索不返回整库正文；按返回的 `card_id` 读取。skeleton 不参与检索。

## 计算、章节和组装

`calculate` 支持 `ratio`、`cagr`、`pe_scenarios`，输入绑定本研究的短事实引用。PE 要求三种情景及敏感性网格；每个情景包含 `name`（bear/base/bull）、`growth`、`multiple`、`reason`、`valid_until` 和 `invalidation`。假设由模型提出，算术由代码执行；PE 当前采用一年盈利情景及不变当前股本，不能冒充已披露历史每股收益。

较长参数保存为 UTF-8 JSON，再使用 `--arguments '@文件路径'`。`save_section` 接收章节号、Markdown、判断、证据、反证、未知项和失效条件；`save_conclusion` 接收模型评级及计算引用。两者必须提供 `snapshot_id`，重启后通过 `get_draft` 恢复。正文可用：

- `{{value:F199}}`：格式化冻结事实。
- `{{cite:F199}}`：附录引用，也支持证据和计算 ID。
- `{{chart:图表ID}}`：插入已绑定图片。
- `{{valuation:计算ID}}`：插入情景结果。

`chart_catalog` 给出候选，`create_chart` 渲染，`view_chart` 查看。现已接入收入利润趋势、利润现金流比较、现金流代理趋势和 PE 敏感性。杜邦、现金流桥、定制沙盒尚未完成，目录明示限制。

`build_report` 在八章和结论齐备后，通过原有报告对象导出；未收到正文返回 `analysis_pending`。未知引用阻止构建。Markdown／HTML／Excel 的真实缓存接口测试通过，测试文字不是正式公司研究；PDF 全页视觉检查尚未执行。定稿前仍需模型完成真实研究、检查图文与数字，再由用户验收阅读体验。

## MCP 与补采状态

新增 `query_valuation(research_id,start_date,end_date,peers,reference_multiple)` 从已登记真实缓存生成独立估值事实投影，返回区间PE统计、1/3年窗口敏感性和共同有效日同行比较。只读原缓存、不发网络请求、不改旧研究包；历史窗口单独进入合同哈希，保留正式准入、源哈希和复算记录。数据缺失返回共同日期缺口，跨运行冲突拒绝静默覆盖。具体接口、已有能力和仍待接入的API见[研究补采自动化清单](research-automation-map.md)。

MCP stdio 服务：`python -m analysis.research.mcp_server`。设置与 CLI 相同的 Python 环境和依赖（`pip install -e ".[research]"`）；宿主配置中的 command 使用已安装依赖的 Python 绝对路径，args 为 `-m analysis.research.mcp_server`，PYTHONPATH 指向本项目 src。MCP 与 CLI 调用同一服务；图片查看返回 MCP Image。已经验证真实进程握手、工具发现和错误返回，尚未完成 Codex／Claude Code 各自的完整宿主验收。

`request_materials`、`query_material_requirements`、`resume_task` 已有持久任务接口；失败重试与正常分轮分别计数，同一资料需求不能改写问题文本绕过重试预算。补采候选快照必须显式 `adopt_snapshot`，不会自动混入原研究。该采集路径仍待真实联网、中断及来源备选验收，不能将接口存在视为已完成自动补采。

## 复现检查

```powershell
$env:ASTRAVALUE_REAL_CACHE_TEST='1'
python -m pytest tests/test_research_workspace.py tests/test_research_calculations.py tests/test_research_knowledge.py tests/test_research_mcp.py tests/test_research_report_integration.py -ra
```

真实缓存集成测试在临时状态目录使用明确标注的测试文字，验证组装和审计，不写入当前茅台研究，不代替黄金报告。未设置环境变量时跳过该本机数据测试。
