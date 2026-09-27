# 公司研究工具运行说明

新研究统一使用 `get_research_prompt` 返回的主提示和审阅提示（buy-side-v4），以及 `get_research_brief` 的写作材料投影；所有宿主共用 `config/prompts/`。写作数据契约仍为buy-side-v2，历史草稿可读。公司任务只填[任务模板](../../config/prompts/company_research_task.md)的对象及运行参数；详细参数和例子移至[按需工具参考](research-tool-reference.md)。本文是部署与工程运行说明，不要求研究模型默认阅读全文或其中的历史验收记录。

当前入口复用既有轻量包、证据读取器、估值引擎、ReportVersion 和导出器。公司身份、数据位置和状态目录由部署配置 `config/research_workspace.json` 管理，模型调用不传供应商或目录。当前行业画像支持已登记的七家白酒公司；其他公司返回画像能力缺口，尚不代表全部 A 股可用。

## 启动与读取

```powershell
Set-Location 'D:/估值模型-worktrees/fact-materialization-ultra'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH=(Resolve-Path src).Path
python -m analysis.research.cli prepare_research --arguments '{"company":"贵州茅台","as_of":"2026-09-13"}'
```

也可仅传 `company`；`latest` 固定为调用当天的日期。现阶段此路径从已登记投影构建，缺失投影返回 `acquisition_required`；冷启动自动采集、当天目录更新尚未接通，不能把缓存首包称作当天资料完整。

宿主没有直接注入研究MCP时使用此CLI，不必自写operations适配器。`--describe`从同一操作的真实类型生成参数Schema（包括分页上限和估值嵌套类型），不执行研究操作。任务提供独立配置时，每次调用都带`--config`；配置内相对路径仍相对`--root`，不相对配置文件目录。

```powershell
python -m analysis.research.cli calculate --describe
python -m analysis.research.cli query_research --describe
python -m analysis.research.cli get_research_brief --config 'output/research/贵州茅台-Codex-Astra-max-真实研究-20260919/workspace-config.json' --arguments '{"research_id":"r_9fb83be3097c6af02aa3ea4d"}'
```

返回的 `research_id` 用于后续工具。`content` 是可读核心包；`coverage.required_core`、研究和排版状态分开。历史包哈希不修改，现行模型自主估值规则通过 `research_policy` 提供。Token 数明确只计冻结核心包，策略与返回结构另计。

```powershell
python -m analysis.research.cli query_research --arguments '{"research_id":"r_9fb83be3097c6af02aa3ea4d","topic":"cash_flow_quality","period_type":"cumulative","page_size":20}'
python -m analysis.research.cli read_evidence --arguments '{"research_id":"r_9fb83be3097c6af02aa3ea4d","evidence_id":"document-evidence-ffd7af8fec07029184bcd7c4","max_tokens":2000}'
python -m analysis.research.cli search_knowledge --arguments '{"question":"现金流"}'
```

目录/查询默认每页20项、上限40，沿 `next_page` 继续；错误返回具体字段、有效范围和重试方式。未知指标先看同主题已注册ID、相近ID与目录入口，不直接补采。类型误筛产生空集时返回可用期间类型，模型显式修正后再查，框架不自动扩宽。画像缺口仍返回能力缺口，原件哈希验证失败拒绝消费。知识检索不返回整库正文；按返回的 `card_id` 读取，skeleton 不参与检索。

概览仅展开当前论点需要的类别，避免一次输出全部明细；大结果保存原始文件，显示标题、期间、状态、读取入口和next_page即可。query_research的正式值在`rows[i].fact.value`，分组period不一定等于事实日期；行情日期读`fact.period_end`。沙盒事实表已经扁平化，读取`data["tables"][表名]["rows"][i]["value"]`，不要混用两种结构。

## 计算、章节和组装

`calculate` 支持比值、CAGR、历史财务汇总、PE情景、合同调价、分红和已注册附注指标；当前方法与参数由`calculate --describe`及统一提示提供。输入绑定本研究的短事实引用。PE的`scenarios.value`是三个对象组成的数组；每项包含 `name`（bear/base/bull）、`growth`、`multiple`、`reason`、`valid_until` 和 `invalidation`。后者是字符串数组，例如`["利润率连续低于基准假设"]`，不是字符串。假设由模型提出，算术由代码执行；PE 采用一年盈利情景及不变当前股本。基期取绑定利润，`valid_until`不替代估值对应时点。

较长参数保存为 UTF-8 JSON，再使用 `--arguments '@文件路径'`，无效字段或类型返回可定位错误。`save_section` 参数为`research_id,snapshot_id,number,markdown,judgment,evidence_refs`，不含逐章反证/未知项/失效字段。`save_conclusion` 接收`rating,summary,theses,risk_summary,calculation_id`及研究/快照ID，唯一风险段最多300个非空白字符，框架在第6章插入一次。重启后用`get_draft(number=0)`读目录、1—8读单章。正文可用：

- `{{value:F199}}`：格式化冻结事实。
- `{{cite:F199}}`：附录引用，也支持证据和计算 ID。
- `{{chart:图表ID}}`：插入已绑定图片。
- `{{valuation:计算ID}}`：插入情景结果。

`chart_catalog` 给出候选，`create_chart` 渲染，`view_chart` 查看。现已接入收入利润趋势、利润现金流比较、现金流代理趋势、杜邦、历史PE和PE敏感性。定制图及探索计算通过run_python_analysis运行，详见[Python沙盒说明](research-python-sandbox.md)；现金流桥仍须补充专门模板与勾稽验证，不能以趋势图冒充。

`build_report` 在八章和结论齐备后导出；未收到正文返回 `analysis_pending`，未知引用阻止构建。先审核心预测的经营量级依据、估值时点及同持有期含息回报，再审文字、数字和每页图文；H2达标目标反推不能代替预测依据。模型审阅与用户人读验收分别记录，测试文字、文件存在或历史样本看过不能代替本版阅读检查。

## MCP 与补采状态

新增 `query_valuation(research_id,start_date,end_date,peers,reference_multiple)` 从已登记真实缓存生成独立估值事实投影，返回区间PE统计、1/3年窗口敏感性和共同有效日同行比较。只读原缓存、不发网络请求、不改旧研究包；历史窗口单独进入合同哈希，保留正式准入、源哈希和复算记录。数据缺失返回共同日期缺口，跨运行冲突拒绝静默覆盖。具体接口、已有能力和仍待接入的API见[研究补采自动化清单](research-automation-map.md)。

MCP stdio 服务：`python -m analysis.research.mcp_server`。设置与 CLI 相同的 Python 环境和依赖（`pip install -e ".[research]"`）；宿主配置中的 command 使用已安装依赖的 Python 绝对路径，args 为 `-m analysis.research.mcp_server`，PYTHONPATH 指向本项目 src。MCP 与 CLI 调用同一服务；图片查看返回 MCP Image。已经验证真实进程握手、工具发现和错误返回，尚未完成 Codex／Claude Code 各自的完整宿主验收。

`request_materials`、`query_material_requirements`、`resume_task` 使用持久任务接口；失败重试与正常分轮分别计数，同一资料需求不能改写问题文本绕过重试预算。旧requirement_ids路径的补采候选快照仍必须显式 `adopt_snapshot`；其全量基础采集验收范围不因本轮补充API接通而扩大。新增material_types路径自动登记当前快照的补充阅读资料，详见下节。

## 复现检查

```powershell
$env:ASTRAVALUE_REAL_CACHE_TEST='1'
python -m pytest tests/test_research_workspace.py tests/test_research_calculations.py tests/test_research_knowledge.py tests/test_research_mcp.py tests/test_research_report_integration.py -ra
```

真实缓存集成测试在临时状态目录使用明确标注的测试文字，验证组装和审计，不写入当前茅台研究，不代替黄金报告。未设置环境变量时跳过该本机数据测试。

附注正式计算新增inventory_composition与inventory_allowance_ratio，均经共享calculate仅传bindings={"period":"2026-06-30"}，无assumptions；公式及可用条件见统一主提示。目录自动以现有证据调和供应商空状态，按列明期间返回替代入口；缓存核验仍仅由用户运行脚本读取。

补充API的旧探针导入仍可用于历史原件：`python scripts/verify_catalog_evidence_completion.py --research-id <id> --api-run <已保存探针目录> --output <用户核查输出目录>`。新请求使用下述自动流程，不要求先运行探针。注册校验公司、披露截止日和响应哈希，原始包不改写；原文核查按年度/半年度提供具体范围，原文可读不代表全部字段数值化，未知不填无事项。

申万分类的旧手工导入仍保留：`python scripts/register_sw_industry.py --research-id <id> --result <申万分类核查结果.json>`。新任务由prepare_research自动准备，无需先运行脚本。登记使用二级目录与完整成分原件复核，不依赖公司写死映射。行业类别只增加一个“申万二级行业替代分类”入口；两个原供应商字段显示已有替代资料，原null不变。industry_index_code明确为行业指数代码；观察日期不等于历史有效日期，晚于截止日的资料仅作补充参考。来源传输核验状态保留，原件变动拒绝读取。

## 自动补充资料（2026-09-18）

CLI/MCP公开prepare_research在核心包就绪后返回supplement_preparation：已有分类则复用，否则返回申万查询后台task_id。get_task(task_id)只读状态，checkpointed调用resume_task；完成后重新读取首包/目录。底层ResearchWorkspace.prepare_research供数据层使用，自动流程位于共享operations入口。offline=true仅跳过本次prepare的自动补充；部署config的offline=true同时禁止补充API请求联网。

request_materials新增material_types，可选sw_industry、audit_opinion、regulatory_records、customers_peer、guarantee、litigation、seo、allotment、bond_issuance、pledge、unlock_peer；模型只传公司研究ID、问题、影响和材料类型。默认自动执行，execute=false仅规划；不与requirement_ids混用。每轮最多70秒（不超过配置max_tool_seconds），保存HTTP缓存及逐页/逐行业进度；最多连续失败次数由max_attempts配置，默认3。get_task不推进网络，resume_task执行下一轮。

同公司/研究/快照/资料类型的重复请求复用结果，换问题措辞不增加请求。refresh=true提供同一快照的一条显式刷新任务链，完成后同样复用，不表示每次调用都重新下载。新研究或新快照有自己的任务。空API响应的任务可completed，但材料为unavailable并保留查询回执；不会覆盖已有原文给出的范围内无事项结论。成功资料自动追加artifact，list_materials/read_material即时加载，无需手工注册或刷新目录。

```powershell
python -m analysis.research.cli request_materials --arguments '{"research_id":"r_9fb83be3097c6af02aa3ea4d","question":"读取审计意见及监管事项","impact":"财务可信度与治理判断","material_types":["audit_opinion","regulatory_records"]}'
```

对外六态和期间定义见[资料目录说明](research-material-catalog.md)。申万公开来源直连失败后尝试本地Clash；仅官方成分主机保留已记录的TLS不验证回退，结果如实标记，不提升来源传输状态。API业务失败不作为成功缓存锁住重试。基础包冷启动、其他公司画像支持范围仍遵循上文边界。

真实读取复核（仅查询已完成任务、复用已有资料）：

```powershell
python scripts/verify_research_auto_supplements.py --research-id r_9fb83be3097c6af02aa3ea4d --research-id r_a8d478cf90f17fe33896aaf4 --task-id j_caea488da387e4a2f3d719c8 --output output/research/自动补充与六态目录复核
```

## Codex实际研究候选（2026-09-19）

本轮从贵州茅台指定冻结包、完整补充目录和独立状态库完成研究、沙盒探索及同版导出，见[验收说明与复现命令](../../output/research/贵州茅台-Codex-Astra-max-真实研究-20260919/验收说明.md)。最终报告为`06795864-90c0-4366-a8a3-bcf177687e7e`：8页PDF、19表Excel、4图及1项经验证探索。所有项目命令在`D:\估值模型-worktrees\fact-materialization-ultra`运行；使用该输出目录的`workspace-config.json`，不得改回原研究库。

该历史会话没有直接注入共享MCP，留下`research_session.py`及`mcp_call.py`供过程复查；新任务使用上文CLI配置与Schema入口。build_report追加新版本，view_report使用对应report_id。Excel包含全文和同版图片；三情景是正式代码的冻结结果，只有“估值模型”基准复核单元格使用Excel公式。人工验收、54题状态回写及双宿主完整冷启动仍未完成。独立全文评审和本轮接口修复证据见该输出目录的`独立精读复核/`；v3提示尚需新完整研究检验，不回写原报告。
