# 有用资料目录与按需读取

首包get_research_brief仅返回分类摘要、覆盖期间、六态数量和展开入口，不返回490项数值、完整证据清单或排除解释。list_materials按类别展开，数据集支持parent_id继续展开到具体字段；read_material按条目、期间读取，长内容沿next_page续读。旧query_research等专项工具仍可使用。

目录覆盖当前快照的正式指标、完整三大报表与已选证据、注册投影文件中符合research_scope.v1.json的consume_fields、已核实知识卡，以及已登记公告缓存中当前公司和研究窗口内有具体研究用途的原件。报表附注按主编号与主题定位，不索引重复子标题。未筛中资料不加载到模型；数据原件和投影均不删除。

公开status统一为formal_numeric（已有正式数值）、original_readable（已有原文）、verified_none（已核实无事项）、alternative（已有替代资料）、unavailable（尚未取得）、processing（待处理）。首包、展开目录和读取使用同一规则；period_states列明期间，scope限定原文核查范围。混合原文/无事项/正式数值的集合统一显示已有原文，展开具体期间再区分。供应商原字段、原始空白及内部缓存状态保留在source_status/source_state，不用于公开完成度计数。API空响应不能产生verified_none；原表空白仍是原文证据，不补零。分红预案与实施、合并与母公司、历史与预测仍按各自定义读取。

原件采用研究默认窗口，结构化长期历史按既有研究范围保留按需入口。程序性会议通知、重复摘要、研究范围排除的数据集及不相关原件不进入默认目录。目录条目固定于构建时的快照与内容哈希，更新缓存后由数据准备流程刷新目录，不静默替换原值。

缓存核对及不纳入原因仅面向用户：没有把catalog_audit或refresh_catalog注册为模型工具；首包不返回审计入口。用户核验脚本逐一对照登记数据库中的content快照ID和投影record_id，检查每项都有入选或排除记录，并抽测各类读取器；该核对不等于每一份材料的语义都已人工验收。

运行：先设置PYTHONPATH=src，再执行`python scripts/verify_research_catalog.py --research-id <研究ID> --output <用户核验目录>`。交付用户核验明细.json与模型实际首包.json。仅核对已登记缓存；未登记磁盘目录不声称已覆盖。新增公司复用同一规则，无法识别的来源/格式仍需补充规则。

## 主题与跨期表

二级附注按同名主题合并，存货统一为“存货构成与减值”；三大报表按表种合并为三个主题。list_materials返回覆盖日期、已整理数据的期间及原文期间，parent_id可继续展开原文。查询默认只搜索当前层，不把逐年子条目重新摊开。原件读取限定在附注标题和下一标题之间，不从该页一直读完整份年报；正文“详见附注”的引用不作为起点。

read_material返回tables：periods是按时间排序的列，rows中values、states、refs逐列对应。同指标不同单位、币种、合并范围、年度/半年累计/单季分开，不自动推定一致。同一期冲突保留观测及来源；原表空白、未取得、不适用符号、明确零分别保存。标准指标引用已有fact_ref；供应商字段和PDF表格整理不自动晋升正式指标，不改变冻结包。

topics-v2同时保留常用摘要表和完整子表视图。note_tables.py自动整理同主题多表、多层合并表头、跨页重复表头/无表头续表、资产增减与分类行层级。严格表格线识别避免背景矩形制造虚假列。原件单元格保存物理页、行列和框坐标，金额、比例、叙述文字与空白分别处理。规则依据原表结构，不硬编码公司金额。

read_material首次读取主题时返回常用表（无既有摘要时返回第一张子表）及table_index；按table_id进一步读取所选子表，可同时筛period。完整子表按报告期对齐，filing_stock_column/filing_column列头仍保留期初/期末/本期/上期及细分含义；比较数据不冒充当期事实，不静默覆盖正式指标。存货摘要仍有独立分项合计和余额减减值等于净额检查。

目录同时返回有数据的期间和全部检测子表已处理的期间。processing逐期记录detected_tables/processed_tables；detected_tables_processed仅指检测清单内表格均处理，partial表示仍有具体表格待处理，no_applicable_table/narrative_only区分无适用表或叙述材料。首表成功不再代表整期完成。未知表格仍保留原文及待处理记录；没有宣称所有公司格式、所有无框表或叙述信息均已转为数值指标。

运行`python scripts/verify_research_topics.py --research-id <研究ID> --output <用户核验目录>`，从当前目录导出主题覆盖清单和存货跨期表，并以PDF文本独立核对存货单元格和完整工具续读。先用前述目录核验命令刷新目录。该脚本是用户核验工具，不注册为模型工具。

完整子表核验使用`python scripts/verify_note_tables.py --research-id <研究ID> --output <用户核验目录> --refresh`：刷新后逐个金额从PDF框坐标独立读取，检查同口径同期间的冲突，导出附注自动整理结果.md及子表核验结果.json。原文排除明细和处理诊断仍只交付用户，不进入模型首包。

已构建目录可省略--refresh直接核验。补规则后，可重复指定--material-id只重处理对应附注原件，新增目录版本；原缓存、其他已整理数据和旧快照保留。缓存清单复核脚本可用--no-refresh检查当前目录，避免重复解析。
