# 完整三大报表与空值核对

默认`get_research_brief`提供精选指标和已核实空白状态，不把所有年度报表塞入首包。`statement_catalog(research_id)`返回期间、报表类型、合并口径、原件页码和证据ID。`read_statement(research_id, statement, period, page, max_tokens)`按需返回完整报表原文；沿next_page读取即可读完，不局限于精选指标。

statement可选balance_sheet、income_statement、cash_flow_statement。期间使用YYYY-MM-DD；季报利润/现金流列的本期或年初累计以原表列头为准，不自动换算。附注或母公司报表仍通过read_document_page读取，不能与合并口径混用。现有接口交付全表原文，并非每个单元格均已标准化为可计算指标。

`prepare_statements`接收已缓存的原件元数据，核对证券代码、报告期、公布日期及哈希，识别法定报表起止，排除后续会计政策调整表。作为新快照候选，经adopt_snapshot采用；旧报告仍绑定旧快照。其他公司的原件使用同一入口，遇不支持的格式明确报错。

本轮将三类空值中的29个去重期间核对为disclosed_blank：报表行存在、当前金额单元格确实为空，保留单元格、页码和原件证据。不把供应商null自动当零，不把“原表留空”写成“不适用”，也不将该状态用于算术运算。缺原件或表格无法识别的仍为pending。

当前茅台已采用19份报告的57份完整合并报表：2021年年报，2022—2025年各四期，2026年一季报及中报。真实逐表分页读取核验在output/research/贵州茅台-财务空值与三大报表核验/核验结果.json。三类数值采集/处理疑点已核对，不表示行业、渠道或治理补证缺口全部关闭。
