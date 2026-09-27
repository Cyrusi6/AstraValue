# 研究工具按需参考

本文件只在需要具体调用方法时查阅，不作为研究主提示全文加载。输入类型和范围以当前工具Schema为准；金融研究和审阅要求统一维护在config/prompts/，不在这里另设研究标准。

## 入口与恢复

优先使用任务提供的研究工具入口。未注入MCP时，在任务指定工作目录设置PYTHONPATH为src，使用共享CLI：

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
$env:PYTHONUTF8='1'
python -m analysis.research.cli get_research_brief --config '<任务配置路径>' --arguments '{"research_id":"<研究ID>"}'
python -m analysis.research.cli calculate --config '<任务配置路径>' --describe
```

长参数用UTF-8 JSON文件，通过`--arguments '@文件路径'`传入。已有研究直接读取，不重新初始化。对当前调用的参数/类型错误，按返回提示修正；真实工具故障保留操作、错误及所影响的判断，交由工程任务处理，不把公司研究转为源码排查或框架开发。业务上可用的替代资料、方法或经验证探索仍可继续研究。

## 目录、期间和补充资料

二级目录将同主题跨期资料合并为一个入口。read_material优先返回已整理的跨期表，期间与数值逐列对应；年度、半年累计和单季等口径分表，sources保留逐期原文入口。目录data_view显示已形成表格的期间和原文覆盖期间；原文可读不等于所有表格已结构化。需要核对完整三大报表时，通过主题的逐期入口，或statement_catalog/read_statement读取，沿next_page读完。disclosed_blank表示已核对原表该金额栏留空，不是采集失败，也不作为数值零。

附注主题另有table_index子表目录，按table_id交给read_material即可读取所选子表，也可同时指定period。不要把首次展示的常用表当作全部附注。完整子表以报告期为列，期初、期末、本期和上期数仍由各自的原列名区分；不要把“本报告期所列期初数”当成该期末数。

目录统一区分已有正式数值、已有原文、已核实无事项、已有替代资料、尚未取得、待处理；以所选期间和scope为准。已有替代资料可按入口使用，不为供应商空字段重复补采。已有原文可用于研究，进入正式计算仍须已注册公式。

prepare_research在核心包就绪后自动准备申万替代分类，supplement_preparation返回后台任务或已登记结果。需要审计或治理补充资料时，调用request_materials(research_id,question,impact,material_types=[...])；可选sw_industry、audit_opinion、regulatory_records、customers_peer、guarantee、litigation、seo、allotment、bond_issuance、pledge、unlock_peer。框架自动查询、校验和登记，模型不选择供应商或文件。get_task读取返回的task_id，checkpointed用resume_task继续；completed后重新展开目录。接口空记录只表示该来源无记录，已有原文核查结论仍按披露范围使用。旧requirement_ids路径仍按任务指引调用，不与material_types混用。技术状态不写进正文。

## 计算、保存和图表引用

所有工具使用同一research_id；保存正文和结论必须绑定当前snapshot_id。引用使用工具给出的真实ID。

工具参数以当前Schema为准。目录和查询先用默认分页并沿next_page续读；概览只保留标题、期间、状态和入口，选中后再展开内容。未知metric_id先按返回的可用ID或目录修正，不直接补采。query_research正式数值位于row["fact"]["value"]；查询分组日期不一定是行情日，行情使用事实中的period_end。若宿主未注入MCP，使用同一CLI的`<operation> --describe`查参数，按任务给定的`--config`运行，无需自写调用器或读源码猜规则。

- save_section(research_id, snapshot_id, number, markdown, judgment, evidence_refs)：保存正文、核心判断和证据引用。没有章节级counter_evidence、unknowns、invalidation参数。
- save_conclusion(research_id, snapshot_id, rating, summary, theses, risk_summary, calculation_id)：评级可选“积极关注”“中性观察”“谨慎”“暂不评级”。summary是一段简洁摘要；risk_summary是唯一风险段，不超过300个非空白字符，必须包含评级失效触发条件；calculation_id绑定你的估值计算。“暂不评级”仅用于公司身份或不可替代的估值基础事实确实无法成立，不用于逃避预测假设。
- get_draft(research_id, number)：按章恢复；number为0读取目录与结论，不一次塞入全部正文。
- chart_catalog / create_chart / view_chart：按需要生成并查看标准图，图表只选择与论证有关的。历史财务计算复用financial_summary；估值敏感性引用你自己的计算结果。
- run_python_analysis：自行编写定制图或探索计算。inputs以命名表选择当前快照的fact_refs，或metric_ids、periods、period_type；也可选择calculation_ids或已验证exploration_ids。代码从data["tables"][表名]["rows"]读取数据；沙盒已将正式事实行整理为value、period、period_type、unit、fact_ref等字段，数值用row["value"]，计算/探索输入则读row["result"]。假设另放assumptions并写依据，不传本地文件路径。可使用np、pd、plt、Decimal、math。
- 定制图使用mode="chart"，代码产出fig与chart_data；工具返回chart_id。先view_chart实际看图，再review_custom_chart填写visual_review和data_review，确认图义、轴单位、期间及绘图数据后引用{{chart:图表ID}}。新财务算法先单独计算验证，绘图不替它注册为正式指标。
- 探索计算使用mode="calculation"，代码产出result对象，并提供definition、applicability、output_unit、output_period。初始结果为unverified；通过validate_python_analysis提交不同实现的复算代码、validation_reason和boundary_cases，验证重复运行、完整结果比较、正常及失败边界。验证通过后用{{explore:exploration_id.字段}}引用，自动注明探索性质，方法与适用限制放附录。get_python_analysis按需读取结果、代码和检查记录。成功运行不自动晋升标准指标；常用方法后续沉淀成正式公式。
- build_report(research_id, formats=["md","html","xlsx","pdf"])：实际生成报告；view_report按页查看。报告存在不等于已经查看。

正文引用：{{value:事实ID}}、{{cite:事实或证据或计算ID}}、{{chart:图表ID}}、{{valuation:估值计算ID}}、{{explore:探索计算ID.字段}}。图表和估值块独立成段，不把相同图表和情景表在多章重复插入。

calculate(method="financial_summary", bindings={})返回历史增速和杜邦分解。

附注新指标按需接入已注册公式：inventory_composition与inventory_allowance_ratio均只传bindings={period:"YYYY-MM-DD"}，不传金额或assumptions。前者返回存货各分类净额占比；若分别披露在产品和自制半成品，还返回两者合计占比value。后者返回期末存货减值准备/期末账面余额。代码核对原件、分类合计及账面余额减准备等于净额。返回value_reference时可直接引用；缺少分类、明确数值或不支持表式时提交具体计算能力需求，不自行填零或算出正式指标。

正文比例也交给calculate，不心算后手工抄写：
- price_change：bindings={start_evidence:首份调价公告ID,end_evidence:末份调价公告ID}，代码提取同产品、同销售合同价口径且首尾衔接的两次调整，计算累计幅度，不相加两次涨幅。
- payout_ratio：bindings={dividend_evidence:年度累计分红披露ID,earnings:同年度归母净利事实ID}，计算分红率，保留“含年度分红预案”等来源口径。
- dividend_yield：bindings={dividend_evidence:年度累计分红披露ID,shares:当前股本事实ID,price:行情事实ID}，计算历史年度分红按参考市值折算的股息率，不作为未来派息。
- forecast_dividend_yield：使用同上bindings；assumptions={dividend_growth:{value:小数,reason:派息假设依据},horizon:{value:"next_12_months",reason:预测期间依据}}。例如dividend_growth=0明确表示假设未来十二个月分红总额维持历史水平，当前股本不变；不是默认事实。
上述方法返回value_reference，正文直接使用{{value:计算ID.value}}，框架替换为百分数并保留计算来源。提取规则不支持的披露格式提交处理需求，不手填金额绕过核验。预测假设仍由模型解释一次。
calculate(method="pe_scenarios")要求bindings包含earnings、shares、price，对应本包事实引用；assumptions包括scenarios、sensitivity_growth、sensitivity_multiples，每项结构为{value:值, reason:理由}。scenarios.value是三个对象组成的数组，各有name（bear/base/bull）、growth（小数）、multiple、reason、valid_until、invalidation；最后一项是非空字符串数组，例如`invalidation:["利润率持续低于基准假设"]`。这里的invalidation是计算假设的内部记录，不要求逐条写进正文。方法是一年盈利情景、当前股本不变；增长所对应的基期由绑定事实决定，valid_until只表示假设有效截止日，不替代目标价时点或持有期。
