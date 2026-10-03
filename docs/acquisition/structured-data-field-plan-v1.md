# 结构化数据字段、首批同行与精读规则 v1

> 接口样本取证日期：2026-09-08；目标范围更新：2026-09-13。样本字段和原参数作为历史能力证据保留，新任务以 [数据与文件采集清单](eight-step-data-and-document-scope.md) 的 55 数据集取舍、54 题映射和 D01–D21 文件规则为准。
>
> 上位文件：[项目总计划](../../项目总计划.md)、[来源规则](../methodology/evidence_policy.md)。本文件细化获取与阅读调度，不替代研究方法。
>
> 状态：新范围已接入请求与物化，当前结果见 [数据层验证](research-data-layer-verification.md)。第 2 节和第 5 节样本数字仍指 2026-09-08，不代表今天重测；后续生产缓存盘点另见新清单第 9 节。接口返回结构不等于默认需要全部采集，也不等于字段已标准化。

## 1. 本轮确定的分工

1. 东方财富主供公司基础、三表、主营、股东、治理和资本事项；优先整理已有事实并作同口径确定性计算，BaoStock 六类指标及东方财富扩展指标按缺口补充。普通股票日线不作默认输入。
2. 每个标准字段、期间类型和业务维度只有一个主要来源。主源成功且通过程序整理即可使用；实际缺失或失败才走同义备选。
3. 目标公司为贵州茅台及六家首批同行。按八步用途取必要字段/期间，默认五个完整年度、十二季度及计算前置期；其他历史有周期、估值或存续事项用途才新增获取，已有缓存复用。
4. 请求端支持选字段时收紧；固定响应完整保存并登记附带字段，只有范围内且通过语义检查的事实进入消费。下表是历史返回能力，不是新请求白名单；[返回字段清单](./structured-data-interface-fields-v1.json) 保留原取证内容。
5. 标签、观点、预测及供应商模型估计单列。原始响应含有它们，不意味着它们成为一类事实。
6. 完整中文年报、中报精读；季报和重大/更正公告按第 6 节触发。独立审计 PDF、英文年报继续停采正文。

## 2. 已核实的接口与具体字段

### 2.1 证据口径与调用方式

本轮通过直接请求确认 **55 个数据集的返回结构**，登记 **2,487 个“数据集—字段”位置**。该数字包含元数据、重复语义、同比/环比字段、文本、标签及空字段，不是 2,487 个独立有效指标。BaoStock 为 0.9.3，辅助阅读的 AKShare 源码为 1.18.43；东方财富接口参数同时参考了本轮官网实际加载的页面脚本。

原始响应和请求证据保留在本地忽略目录 `var/research/structured-data-contracts-20260908/`，不进入 Git。版本化 JSON 只保存字段名、样本类型/非空情况、查询参数、获取时间和哈希等结构证据；`plan_group_id` 对应下表 ID。东方财富哈希对应 HTTP 响应字节，BaoStock 哈希对应 SDK 查询结果与元数据组成的本地 JSON 快照，不冒充原生协议报文字节。

表中调用基址：

| 简称 | 实际入口 |
|---|---|
| EM-F | `https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/` |
| EM-S | `https://datacenter.eastmoney.com/securities/api/data/v1/get` |
| EM-W | `https://datacenter-web.eastmoney.com/api/data/v1/get` |
| EM-M | `https://datacenter.eastmoney.com/securities/api/data/get` |
| EM-Q | `https://push2.eastmoney.com/api/qt/stock/get` |
| BS | BaoStock Python API，匿名登录，原生连接 |

历史样本 EM-S/EM-W 使用 `reportName`、`columns=ALL`、公司过滤条件、`pageNumber/pageSize`；保留 `result.count/pages` 和终页证明。样本单公司过滤：`(SECUCODE="600519.SH")`；需要 `SECURITY_CODE` 或 `DIM_SCODE` 的例外见表。批量同行过滤已对七家公司验证。`columns=ALL` 是当时取证参数，不是新范围默认；新请求需限制字段、公司和期间并验证稳定排序。

样本参数是本轮取证记录，不是完整生产请求模板。例如仅按报告期排序仍可能有同日多条记录；正式分页须使用接口支持的稳定次级键，或按期间分区后完整取得分区，并核对记录键、总数和跨页重复，不能只因第二页有返回就宣布无遗漏。

“有数据”表示本轮至少一个选定公司样本返回了字段；不是七家公司该项均完整。主要空项：贵州茅台客户/供应商表及限售解禁查询返回 `success=false, code=9201, result=null, message=返回数据为空`，同行同接口有数据。应记录为供应商本次无记录，允许触发备选；不能据此称公司没有该业务，也不把 9201 当作完整历史的证明。

### 2.2 三表、财务指标与日行情

| ID | 字段组与主要来源 | 实际调用 | 具体原字段/归属 | 更新与阅读 |
|---|---|---|---|---|
| F01 | 资产负债表：东方财富 | EM-F `zcfzbDateAjaxNew` + `zcfzbAjaxNew` | 全部 319 个返回字段；关键项 `MONETARYFUNDS, INVENTORY, CONTRACT_ASSET, CONTRACT_LIAB, GOODWILL, TOTAL_ASSETS, TOTAL_LIABILITIES, TOTAL_EQUITY, TOTAL_PARENT_EQUITY` | 随报告；年报/中报精读，季报触发读 |
| F02 | 累计/年度利润表：东方财富 | EM-F `lrbDateAjaxNew` + `lrbAjaxNew`，`reportType=1` | 全部 203 个字段；`TOTAL_OPERATE_INCOME, OPERATE_INCOME, OPERATE_COST, OPERATE_PROFIT, NETPROFIT, PARENT_NETPROFIT, DEDUCT_PARENT_NETPROFIT` 及费用、减值、税项 | 同上 |
| F03 | 累计/年度现金流：东方财富 | EM-F `xjllbDateAjaxNew` + `xjllbAjaxNew`，`reportType=1` | 全部 254 个字段；`NETCASH_OPERATE, NETCASH_INVEST, NETCASH_FINANCE, END_CASH_EQUIVALENTS, BEGIN_CASH_EQUIVALENTS` 及流入/流出、补充资料 | 同上 |
| F04 | 单季利润、现金流：东方财富 | 对应 `lrbAjaxNew/xjllbAjaxNew`，`reportType=2` | 分别观测 204/253 个字段；按单季保存，不能把 QOQ 字段当金额 | 随季度；按变化触发 |
| F05 | 扩展财务指标：东方财富 | EM-M `type=RPT_F10_FINANCE_MAINFINADATA, sty=APP_F10_MAINFINADATA` | 141 个历史返回字段；所需且无法从三表同口径取得/复算的指标按需映射，其余保留响应 | 具体缺口及报告更新；不重复建同义主值 |
| B01 | 日行情/日估值：BaoStock | `query_history_k_data_plus`，`frequency=d, adjustflag=3` | `date, code, open, high, low, close, preclose, volume, amount, adjustflag, turn, tradestatus, pctChg, peTTM, pbMRQ, psTTM, pcfNcfTTM, isST` | 整套日线默认不取；估值备选仅所需字段/日期，新请求另验 |
| B02 | 盈利指标：BaoStock | `query_profit_data(code, year, quarter)` | `roeAvg, npMargin, gpMargin, epsTTM`；同响应 `netProfit, MBRevenue, totalShare, liqaShare` 保留，三表/股本仍采用指定主字段 | B02–B07 均按需补充；激活后随报告，保留专属定义 |
| B03 | 营运指标：BaoStock | `query_operation_data` | `NRTurnRatio, NRTurnDays, INVTurnRatio, INVTurnDays, CATurnRatio, AssetTurnRatio` | 同上 |
| B04 | 成长指标：BaoStock | `query_growth_data` | `YOYEquity, YOYAsset, YOYNI, YOYEPSBasic, YOYPNI` | 同上 |
| B05 | 偿债指标：BaoStock | `query_balance_data` | `currentRatio, quickRatio, cashRatio, YOYLiability, liabilityToAsset, assetToEquity` | 同上 |
| B06 | 现金流/资产结构指标：BaoStock | `query_cash_flow_data` | `CAToAsset, NCAToAsset, tangibleAssetToAsset, ebitToInterest, CFOToOR, CFOToNP, CFOToGr` | 同上；空的利息保障倍数不补零 |
| B07 | 杜邦指标：BaoStock | `query_dupont_data` | `dupontROE, dupontAssetStoEquity, dupontAssetTurn, dupontPnitoni, dupontNitogr, dupontTaxBurden, dupontIntburden, dupontEbittogr` | 同上；不与其他口径 ROE 无条件合并 |
| B08 | 证券状态、日历、复权：BaoStock | `query_stock_basic, query_trade_dates, query_adjust_factor` | `code_name, ipoDate, outDate, type, status`；`calendar_date, is_trading_day`；`dividOperateDate, foreAdjustFactor, backAdjustFactor, adjustFactor` | 身份与共享日历按需；复权默认不取 |
| M01 | 总/流通市值与收盘估值：东方财富 | EM-M，`RPT_VALUEANALYSIS_DET`，`SECUCODE` 分页，`st=TRADE_DATE` | `TOTAL_MARKET_CAP=总市值, NOTLIMITED_MARKETCAP_A=流通A股市值, CLOSE_PRICE=收盘价, TOTAL_SHARES=总股本, FREE_SHARES_A=流通A股股本, PE_TTM/PB_MRQ/PS_TTM=估值指标, TRADE_DATE=交易日`；其余响应字段原样保留 | 按估值时点/明确历史区间取必要字段；历史样本为 T+1 收盘、可见起点 2018-01-02，非实时承诺 |

F01–F04 的 `companyType` 从来源页面取得，本轮普通企业样本为 4，不能对其他行业硬编码；`reportDateType=0`，按目录日期分组请求。资产/利润目录均返回 103 期，现金流目录 99 期；只抽取了指定期报表，未全量下载这些期间。

BaoStock B02–B07 的 `quarter` 指查询报告季度，不能据此把所有指标标成“本单季”。每个字段保留 `pubDate/statDate`，按其累计、TTM 或时点定义使用；源定义未明确的字段先保留供应商命名空间，不作期间转换。单季营收与利润变化使用 F04 的同季度金额计算，不将其 `_QOQ` 字段误当同比。

已验证的首批财务范围采用上市公司默认报表及其归母/少数股东科目。母公司单体三表尚未取得独立接口证明，登记为独立范围缺口，不能用同一默认报表同时标成合并与母公司。

### 2.3 业务、公司、股东与治理

下列各行的结构化来源均为东方财富，返回字段登记与新任务选择分开；`staff_structure`、`holder_count` 等按新清单的具体用途激活，不因列在本表而默认取得整类历史。

| ID | 数据组 | 实际数据集/入口 | 关键原字段 | 更新与精读 |
|---|---|---|---|---|
| C01 | 公司基本信息 | EM-S `RPT_F10_ORG_BASICINFO` | `ORG_NAME, FORMERNAME, FOUND_DATE, LISTING_DATE, REG_CAPITAL, REG_ADDRESS, ORG_WEB, BUSINESS_SCOPE, ACCOUNT_FIRM` 等 107 字段 | 基线/变更；文字简介单列 |
| C02 | 主营构成 | EM-S `RPT_F10_FN_SEGMENTSV` | `REPORT_DATE, MAINOP_TYPE, ITEM_NAME, ACTUAL_ITEM_NAME, ITEM_CODE, ITEM_PARENT_CODE, ITEM_LEVEL, MAIN_BUSINESS_INCOME, MAIN_BUSINESS_COST, MAIN_BUSINESS_RPOFIT, MBI_RATIO, GROSS_RPOFIT_RATIO` | 年报/中报；经营、产品与地区章节 |
| C03 | 客户/供应商 | EM-S `RPT_F10_BUSINESS_CUSTSUPP` | `TYPE, TYPE_CODE, ITEM_NAME, AMOUNT, SUM_AMOUNT, TOI_RATIO, REPORT_DATE, RANK` | 随报告；匿名或缺失保持原状态 |
| C04 | 研发 | EM-S `RPT_F10_BUSINESS_RDEXPENSE` | `RESEARCH_EXPENSE, RESEARCH_EXPENSING, RESEARCH_EXPENSE_CAPITALIZATION, RESEARCH_EXPENSE_RATIO, RESEARCH_NUM, RESEARCH_NUM_RATIO` 及变化字段 | 随报告；投入/资本化明显变化时读 |
| C05 | 员工与人均指标 | EM-S `RPT_HSF9_BASIC_STAFFCOMPOSITION` | `TOTAL_NUM, PRODUCT_NUM, SALE_NUM, FINANCE_NUM, TECHNOLOGY_NUM, RESEARCH_NUM, DOCTOR_NUM, MASTER_NUM, SALARY, AVG_SALARY, AVG_INCOME` 等 | 随报告；人均指标保留供应商定义 |
| C06 | 人员结构 | EM-S `RPT_F10_STAFFCOMPETE_STRUCTURE` | `YEAR, DISTRIBUTION_TYPE, DISTRIBUTION_NAME, LAYEREMPLOYEE_PCT, REPORT_DATE` | 随报告；不默认精读 |
| C07 | 控参股企业 | EM-S `RPT_F10_PUBLIC_OP_HOLDINGORG` | `HOLD_ORG_NAME, HOLD_TYPE, REG_CAPITAL, ORG_HOLD_RATIO, ACTUAL_INVEST_AMT, NETPROFIT, MAIN_PRODUCTS, REPORT_DATE` | 随报告/股权事件；重要变化读 |
| G01 | 实控关系 | EM-S `RPT_F10_EH_RELATION` | `HOLDER_NAME, RELATED_RELATION, HOLD_RATIO, SECUCODE` | 变更；实控变化必读，不凭同名合并实体 |
| G02 | 股本历史 | EM-S `RPT_F10_EH_EQUITY` | `END_DATE, TOTAL_SHARES, LIMITED_SHARES, UNLIMITED_SHARES, LISTED_A_SHARES, CHANGE_REASON` 等 73 字段 | 按事件；变动原因按需 |
| G03 | 十大股东历史 | EM-S `RPT_F10_EH_HOLDERS` | `END_DATE, HOLDER_CODE, HOLDER_NAME, HOLDER_RANK, HOLD_NUM, HOLD_NUM_RATIO, SHARES_TYPE, HOLD_NUM_CHANGE` | 随报告/事件；不是全部股东名册 |
| G04 | 十大流通股东历史 | EM-S `RPT_F10_EH_FREEHOLDERS` | `END_DATE, HOLDER_CODE, HOLD_NUM, FREE_HOLDNUM_RATIO, HOLD_RATIO, SHARES_TYPE, NOTICE_DATE` 等 | 同上；比例基数不混用 |
| G05 | 股东户数 | EM-W `RPT_HOLDERNUM_DET`，按 `SECURITY_CODE` | `END_DATE, HOLDER_NUM, PRE_HOLDER_NUM, HOLDER_NUM_CHANGE, HOLDER_NUM_RATIO, HOLD_NOTICE_DATE` | 按事件；不默认精读 |
| G06 | 管理层任职 | EM-S `RPT_F10_ORGINFO_MANAINTRO` | `PERSON_CODE, PERSON_NAME, INCUMBENT_DATE, INCUMBENT_TIME, POSITION, POSITION_TYPE_CODE, BIRTH_YEAR, NATIONALITY, HOLD_NUM, SALARY, REPORT_DATE` | 变更/报告；关键人员变更读 |
| G07 | 高管历史薪酬 | EM-S `RPT_F10_ORGINFO_SALARY` | `END_DATE, PERSON_CODE, PERSON_NAME, POSITION, SALARY, AVG_SALARY` | 随报告；薪酬金额按元登记 |
| G08 | 高管及相关人员持股变动 | EM-W `RPT_EXECUTIVE_HOLD_DETAILS`，按 `SECURITY_CODE` | `CHANGE_DATE, PERSON_NAME, CHANGE_SHARES, AVERAGE_PRICE, CHANGE_AMOUNT, CHANGE_REASON, CHANGE_AFTER_HOLDNUM, PERSON_DSE_RELATION` | 事件；赠与/继承等不等于市场买卖 |
| G09 | 担保 | EM-W `RPT_F10_ORGRES_GUARANTEE` | `GUAR_NAME, GUARANTEED_NAME, GUARANTEE_AMT, CURRENCY, GUARANTEE_START_DATE, GUARANTEE_END_DATE, IS_PERFORM, IS_RELATED_TRADE, NOTICE_DATE` | 事件；重大金额/异常状态读 |
| G10 | 诉讼仲裁 | EM-W `RPT_LITIGATION_ARBITRATION_BSINFO` | `CASE_NAME, PLAINTIFF, DEFENCE, CASE_AMOUNT, CURRENCY, FI_PROSECUTE_DATE, FI_JUDGMENT_DATE, EXECUTE_SITUATION, NOTICE_DATE` | 事件；达到调度阈值或关键事项读 |
| G11 | 违规处罚 | EM-W `RPT_HSF9_OP_VIOLATION` | `SOLVE_ORG, VIOLATE_TYPE, PUNISH_TYPE, PUNISH_AMT, PUNISH_OBJECT, NOTICE_DATE` | 事件；公司/关键人员重大事项读 |
| G12 | 商誉 | EM-W `RPT_GOODWILL_STOCKDETAILS` | `GOODWILL, GOODWILL_PRE, GOODWILL_CHANGE, SUMSHEQUITY, SUMSHEQUITY_RATIO, SE_CHANGE_RATIO, REPORT_DATE` | 余额标准字段主源仍是 F01；本表主供减值及专属比例 |

C02 必须使用分页数据集，不能以 `BusinessAnalysis/PageAjax` 或 `stock_zygc_em` 的 200 行返回作为全历史完成证明。本轮 C02 显示 446 条，并已验证第 2 页返回不同记录；尚未遍历全部 446 条。

C02 的产品、行业、地区及父子分类必须分开；五粮液“酒类”与其下产品、今世缘“白酒”与产品档次不能重复相加。采用记录的父子键、层级与原始名称；分类不明时保留原始分类，不能根据标题创造一个产品。

G12 的 `GOODWILL_CHANGE` 已在东方财富当前[商誉减值页面](https://data.eastmoney.com/sy/jzlist.html)核到列名“商誉减值(元)”。按这一来源字段保存，不能用商誉余额前后之差自行冒充减值。净资产比例保留供应商分母定义，不能自动换成归母净资产比例。

### 2.4 资本、交易、持仓及独立信息层

| ID | 数据组与主要来源 | 实际数据集/入口 | 关键原字段 | 更新与精读 |
|---|---|---|---|---|
| A01 | 分红送转：东方财富 | EM-W `RPT_SHAREBONUS_DET`，`SECURITY_CODE` | `PRETAX_BONUS_RMB, BONUS_RATIO, IT_RATIO, PLAN_NOTICE_DATE, EQUITY_RECORD_DATE, EX_DIVIDEND_DATE, ASSIGN_PROGRESS, IMPL_PLAN_PROFILE` | 事件；方案与实施分开，特殊变化读 |
| A02 | 回购：东方财富 | EM-W `RPTA_WEB_GETHGLIST_NEW`，`DIM_SCODE` | `REPURPROGRESS, REPUROBJECTIVE, REPURAMOUNTLIMIT, REPURNUMCAP, REPURAMOUNT, REPURNUM, REPURSTARTDATE, FINISHDATE` | 事件；用途/终止/重大规模变化读 |
| A03 | 增发、配股：东方财富 | EM-S `RPT_F10_DIVIDEND_SEO / RPT_F10_DIVIDEND_ALLOTMENT` | `ISSUE_NUM, ISSUE_PRICE, TOTAL_RAISE_FUNDS`；增发另有 `NET_RAISE_FUNDS, LISTING_STATE, SEO_PURPOSE, NOTICE_DATE` | 事件；金额与实际状态决定读取 |
| A04 | 债券发行：东方财富 | EM-S `RPT_F10_DIVIDEND_BOND` | `BOND_COMBINE_CODE, BOND_TYPE, ISSUE_SCALE, ISSUE_COUPON_IR, VALUE_DATE, EXPIRE_DATE, TRANSFER_START_DATE, INITIAL_TRANSFER_PRICE` | 事件；偿付/转股关键条款按需 |
| A05 | 质押/解押：东方财富 | EM-W `RPTA_APP_ACCUMDETAILS`，`SECURITY_CODE` | `HOLDER_NAME, PF_NUM, PF_HOLD_RATIO, PF_TSR, PF_START_DATE, ACTUAL_UNFREEZE_DATE, UNFREEZE_STATE, NOTICE_DATE` | 事件；状态与分母明确后使用 |
| A06 | 限售解禁：东方财富 | EM-W `RPT_LIFT_STAGE`，`SECURITY_CODE` | `FREE_DATE, CURRENT_FREE_SHARES, ABLE_FREE_SHARES, NON_FREE_SHARES, BATCH_HOLDER_NUM, FREE_SHARES_TYPE` | 事件；计划与实际区分 |
| A07 | 投资项目：东方财富 | EM-S `RPT_F10_CAPITAL_ITEM`，`SECURITY_CODE` | `ITEM_NAME, NOTICE_DATE, PLAN_INVEST_AMT, ACTUAL_INPUT_RF, BUILD_PERIOD` | 事件；计划金额不等于已投入，收益率/回收期单列预测 |
| A08 | 募资使用基础：东方财富 | EM-S `RPT_F10_CAPITAL_RAISE` | `FINANCE_TYPE, NET_RAISE_FUNDS, START_DATE, NOTICE_DATE` | 事件；不以净募资额推断项目完成 |
| T01 | 融资融券：东方财富 | EM-S `RPT_MARGIN_STATISTICS_STOCKS` | `TRADE_DATE, FIN_BUY_AMT, FIN_REPAY_AMT, FIN_BALANCE, LOAN_SELL_VOL, LOAN_REPAY_VOL, LOAN_BALANCE, LOAN_BALANCE_VOL` | 默认不取；不是公司的借款或有息债务 |
| T02 | 大宗交易：东方财富 | EM-S `RPT_DATA_BLOCKTRADE` | `TRADE_DATE, DEAL_PRICE, DEAL_VOLUME, DEAL_AMT, BUYER_NAME, SELLER_NAME, PREMIUM_RATIO, TRADE_UNIT` | 仅具体股东事件按需，不抓整套成交历史 |
| T03 | 龙虎榜成交：东方财富 | EM-S `RPT_BILLBOARD_DAILYDETAILS` | `TRADE_DATE, EXPLANATION, TOTAL_BUY, TOTAL_SELL, TOTAL_NET, TRADE_ID` | 默认不取 |
| T04 | 机构持仓汇总：东方财富 | EM-S `RPT_F10_MAIN_ORGHOLDDETAILS` | `REPORT_DATE, ORG_TYPE, TOTAL_ORG_NUM, TOTAL_FREE_SHARES, TOTAL_SHARES_RATIO, IS_COMPLETE` | 仅特定股东问题按需；不与明细重复计数 |
| T05 | 基金持仓明细：东方财富 | EM-S `RPT_MAIN_ORGHOLDDETAIL`，`ORG_TYPE=01` | `REPORT_DATE, HOLDER_CODE, HOLDER_NAME, FUND_CODE, TOTAL_SHARES, HOLD_VALUE, TOTALSHARES_RATIO, FREESHARES_RATIO, NETVALUE_RATIO` | 默认不取；历史样本共 1697 条仅取 2 条 |
| T06 | 机构调研：东方财富 | EM-W `RPT_ORG_SURVEY`，`SECURITY_CODE` | `NOTICE_DATE, RECEIVE_START_DATE, RECEIVE_OBJECT, INVESTIGATORS, RECEPTIONIST, URL, CONTENT` | 未回答问题/新信息按需；D18 相关问答作为来源文本 |
| I01 | 宏观 CPI：东方财富 | EM-W `RPT_ECONOMY_CPI` | `REPORT_DATE, NATIONAL_SAME, NATIONAL_BASE, NATIONAL_SEQUENTIAL, NATIONAL_ACCUMULATE` 及城乡字段 | 研究需要时加载，随月度发布更新 |
| I02 | 社零：东方财富 | EM-W `RPT_ECONOMY_TOTAL_RETAIL` | `RETAIL_TOTAL, RETAIL_TOTAL_SAME, RETAIL_TOTAL_SEQUENTIAL, RETAIL_TOTAL_ACCUMULATE, RETAIL_ACCUMULATE_SAME` | 同上；不当作白酒销量 |
| L01 | 平台概念标签：东方财富 | EM-S `RPT_F10_CORETHEME_BOARDTYPE` | `BOARD_CODE, BOARD_NAME, BOARD_TYPE, BOARD_LEVEL, SELECTED_BOARD_REASON` | 概念题材默认不取；明确分类线索需要时另列独立层 |
| P01 | 机构盈利预测：东方财富 | EM-S `RPT_HSF10_RES_PREDICTDETAIL` | `PUBLISH_DATE, ORG_NAME_ABBR, RESEARCHER, YEAR1..4, EPS1..4, PARENT_NETPROFIT1..4, RATING` | 按需独立观点层；年份依 YEAR 字段，不作为历史实际值 |

历史备选定位包括东方财富 `stock_zh_a_hist`（上游 `push2his.eastmoney.com/api/qt/stock/kline/get`）和新浪三表 `stock_financial_report_sina`，当时未重复取数。普通日线备选不再属于默认任务；必要估值备选另限字段/日期，三表缺口触发时验证新浪响应及同义映射。源码/接口定位不等于联网成功。

国债收益率按需指定中债 `bond_china_yield`，实际上游为 `yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery`；所需区间按单次小于一年的窗口分段。历史仅核到封装与参数，联网及当前可得边界待具体估值任务触发时检查。

### 2.5 尚无完整结构化接口证明的细项

以下仍保留为明确缺口，不能在实现时用猜测数据或通用长文本伪装成类型化金额：

- 母公司单体三表；个别公司的客户/供应商名单或金额；历史完整任期终止日期。
- 全面的关联交易对手/合同明细、并购对价与业绩承诺状态、股权激励行权条件、内控缺陷及整改细项。本轮担保的 `IS_RELATED_TRADE` 只能表达关联担保，不能代表全部关联交易。
- 债券实时余额、实际转股/赎回的全生命周期；本轮发行表里的初始转股价不能代表当前转股价。
- 白酒产销量、产能利用率、渠道库存、终端批价等没有在本轮找到并实测的稳定免费字段。社零、公司产品收入或平台标签不能替代这些变量。

处理规则：具体缺口先尝试已登记同义免费备选，再按 D01–D21 复用/取得必要文件；没有可用来源就留缺口。不得因一项为空遍历所有 PDF。新字段需有八步用途与定义才版本化纳入，不因免费或接口新增便默认扩采。

## 3. 字段整理与去重合同

### 3.1 一个标准字段的唯一主路由

- 财务原始金额以对应东方财富三表科目为主；现金流补充资料和主要指标接口返回的同名净利润不另建第二个默认真值。
- 通用盈利、成长、营运、偿债、现金流和杜邦指标优先用已有完整同口径事实确定性计算，保存输入与公式；B02–B07、F05 只按已确认缺口补充。需要供应商特定口径时登记独立字段，不把近似本地公式当同义替换。新路由另行版本化，旧冻结合同不改写。
- `epsTTM` 不用累计基本 EPS 替代；`roeAvg` 不用定义不同的加权 ROE 替代；`MBRevenue` 保留供应商原标签，不自动改成“营业收入”或“主营收入”；营收标准字段明确区分 F02 的 `TOTAL_OPERATE_INCOME` 与 `OPERATE_INCOME`。
- 估值按 M01 所需时点/区间的价格、市值、股本和 PE/PB/PS 主输入整理。历史已定义 `pe_ttm, pb_mrq, ps_ttm, pcf_net_cashflow_ttm` 等不意味着全部必取；BaoStock `pcfNcfTTM` 的现金流量净额口径不等于经营现金流市现率，不能无条件同义补源。
- 员工总数的期间序列用 C05，个人薪酬历史用 G07，实控关系用 G01，股本变动用 G02；基本资料中的当前摘要保留取得时间，不覆盖期间事实。
- 因同一接口同时返回多个字段而产生的原始重叠可以保留；不为主源已经成功的标准字段再单独发起一次查询。

### 3.2 单位、期间与时间

| 内容 | 整理规则 |
|---|---|
| 财务金额/股本/薪酬 | 保存原始单位和币种，规范金额为元、股份为股；已在页面渲染核到 G02 原值按股，G07 薪酬按元，不能把显示的万股/万元再误套原值 |
| 比率与百分数 | 规范为 ratio；BaoStock 利润率/ROE 与 C02 `MBI_RATIO/GROSS_RPOFIT_RATIO` 原值为小数；`turn/pctChg` 等按其百分数定义转换，不能同名一律除以 100 |
| 分红 | `PRETAX_BONUS_RMB/BONUS_RATIO/IT_RATIO` 保留“每 10 股”的源口径并显式换算每股；缺现金字段不补零，除权日不能代替实际支付日 |
| 单季与累计 | F02/F03 按报告期累计，Q4 对应全年；F04 为供应商单季。缺单季时才用同年同范围累计相减，保留输入和公式 |
| TTM | 复用已有有效同义 TTM，或由完整同口径期间确定性派生；仅必要缺口补源。缺季不拼凑，不平均四季利润率或对存量做 TTM，供应商专属 EPS 分母另核 |
| 资产负债与持仓 | 按时点保存，不相减当单季，也不相加当 TTM；十大股东不代表全部持仓 |
| 日期 | 分开报告期、公告/发布时间、生效日、行情时点和获取时间；已取得修订版本留存，不主动追查全部正式更正 |
| 历史可得性 | 当前历史序列可直接用于当前研究；无当时版本证明不能冒充严格 PIT 回放输入。交易后的 N 日涨跌幅不得提前进入交易当日回放 |

按供应商字段定义进行整理，无明确单位/含义的字段保留原始值并标记具体疑点，只影响该字段的精确计算，不阻断其他有效记录。不会为“解释所有未知字段”默认启动 PDF 核验。

### 3.3 非一类字段与文本

- A05 的 `WARNING_LINE, OPENLINE, WARNING_STATE, IS_DOUBT` 归供应商估计/判断，不作为实际平仓、违约或司法事件。
- A07 的 `YIELD, INVEST_RECOVERY_PERIOD` 归项目预测；计划投资额是“已公布计划”的记录，不能改标为已支出。
- C01 的简介/宣传，G06 的履历文本，T06 的调研回答保留为来源文本。明确类型化任期或日期可以映射，优势判断和前瞻回答不自动变成事实。
- L01、P01 整体进入独立层；股东接口中平台推断的关联标签同样单列。
- 源字段清单是实测结构清单。未知字段先登记、分类，不静默丢弃；其未分类状态不表示所有客观字段需要逐项人工批准。

## 4. 所需历史与更新安排

| 数据组 | 首次基线 | 新范围目标更新安排（北京时间，待接入运行） |
|---|---|---|
| 财务/财务指标 | 所需五年/十二季加计算前置期，先缓存整理；额外历史有明确用途才取，所选分区完整分页 | 随报告更新；供应商尚未更新则记延迟，不以旧值冒充最新 |
| 估值辅助输入 | 指定时点价格/市值/股本、明确区间频率的估值序列；原报价日期保持 | 按报告需要，不设普通日线每日默认任务 |
| 股东/治理/资本事件 | 当前状态、窗口内事件和更早仍存续事项；所选范围全分页，计划/实施串联 | 每日 20:30 与目录衔接新增/更新，未完成事项刷新；无关交易数据不纳入 |
| 公司基础/管理层摘要 | 首次取得当前快照，接口给出的历史另行保存 | 报告/相关事件触发刷新；每周一次检查供应商摘要变化 |
| 轻量公告目录 | 复用原目录与有效 checkpoint；新增同行分别建立范围 | 每日 20:30，包含非交易日；按标题类别生成重要正文任务 |
| 行业/宏观/预测 | 问题激活后只取所需变量/期间，预测独立；概念题材默认不取 | 随相应发布更新，同行共享缓存；没有历史接口不伪造历史 |

在线访问默认每来源单并发、请求结束后至少间隔 3 秒；现有巨潮策略保留其更严格的至少 5 秒直连间隔。正式调度须服从来源实际限额；不设置无限重试。

优先按供应商更新标记续读；没有更新标记的适用事件表使用最近 30 日重叠窗口加未完成事项刷新，并去重。首次所选范围后不默认全表重抓；该策略不承诺发现供应商没有更新信号的久远原地改写。

分页必须覆盖获取、汇总、恢复、去重和熔断读取路径。稳定记录键不足时组合公司、期间、维度/主体、业务类型与来源记录 ID；不得仅用报告期合并多条股东或业务记录。源快照新版本保留 hash，不覆写旧报告。

## 5. 首批公司与同行集合

选定目标 **贵州茅台 600519**；首批仍为 **7 家**，按相同八步用途与期间口径取各自适用数据。名单依据沿用 2026-09-08 研究选择，不作为供应商客观事实或自动估值权重；不再默认所有接口全历史。

| 公司 | 代码 | 首批角色 | 纳入依据与使用边界 |
|---|---|---|---|
| 贵州茅台 | 600519.SH | 目标 | 已有业务采集与重要报告基线，继续复用 |
| 五粮液 | 000858.SZ | 核心同行 | 主要酒类业务，可比较核心产品、渠道、利润与现金回报；保留非酒类占比 |
| 泸州老窖 | 000568.SZ | 核心同行 | 酒类及中高档酒业务，可比较产品结构、渠道与现金回报 |
| 山西汾酒 | 600809.SH | 经营比较同行 | 酒类业务，可比较业务扩张、周转、渠道和利润变化；产品结构单独处理 |
| 洋河股份 | 002304.SZ | 经营比较同行 | 以白酒为主，可比较渠道、经营变化、存货和资本回报 |
| 古井贡酒 | 000596.SZ | 经营比较同行 | 以白酒为主，保留酒店等其他业务及不同子公司结构 |
| 今世缘 | 603369.SH | 经营比较同行 | 以白酒为主，可比较分档产品、渠道、区域业务与成长 |

本轮同一期查询采用 `RPT_F10_FN_SEGMENTSV`、`REPORT_DATE=2025-12-31`：返回 72/72 行、终页、七家公司齐全。酒类/白酒相应项目收入占比约为：茅台 99.96%、五粮液 91.55%、泸州老窖 99.51%、山西汾酒酒类销售 99.68%、洋河白酒 97.59%、古井白酒 98.45%、今世缘白酒 98.30%。这些是供应商该分类口径的样本值，用于支持行业相关性；不将各公司不同分类、合并范围或渠道自动视为相同。

同行使用规则：

- 数据层采集全部六家同行；估值默认先展示两家核心同行的逐公司指标，另外四家用于经营比较。全体分布可作为辅助视图，不把六家的均值机械当成目标公允估值。
- 对比时固定财务期间、指标定义、分类版本与业务结构；亏损或指标不适用的公司仍保留在采集集合，相关倍数单列而非删除公司。
- 其他白酒公司、酒类流通商和啤酒/葡萄酒企业本轮不自动扩入；出现新的具体研究问题时再版本化扩展。
- 新集合保存选择日期、依据、角色及纳入/排除理由；不把这份今日名单回填为过去某时点已知的同行集合。也不把本轮代理选定伪写为历史人工逐项验收。

## 6. 精读触发条件

### 6.1 固定阅读与触发阅读

- **完整中文年报、中报**：所需期间进入精读队列，优先经营讨论、产品/地区/渠道、关键附注、治理和资本事项。使用完整正文并保留内部审计章节，按主题读、按哈希复用；范围见新清单，不自动解析所有历史中报。摘要不能冒充完整报告。
- **季报**：结构化更新始终执行；符合下表任一规则或已有研究问题需要解释时，定向读相关章节。
- **重大事件/更正**：公司控制、主要管理层、交易条款及研究结论所依赖事项触发阅读；更正仅因标题出现不默认逐份下载核对。
- **其他材料**：轻量目录与选择理由保留。主源空值/接口失败进入补源与缺口流程，本身不是“重读所有原文”的触发器。

### 6.2 v1 数值与事件触发器

以下阈值用于安排阅读，不是法定重大性标准、估值参数或风险评级。它们是本轮选定的可版本化初始规则，尚未以生产研究结果校准。

| ID | 触发条件（任一满足） | 定向阅读 |
|---|---|---|
| R01 | 同口径营收、归母净利或扣非归母净利单季同比变化绝对值 ≥20%；或该同比增速较上一季度变化的绝对值 ≥15 个百分点 | 季报经营解释、收入/利润变化；相关最新年报/中报经营讨论 |
| R02 | 营业毛利率同比变化绝对值 ≥3 个百分点；销售/管理/研发费用率任一同比变化绝对值 ≥2 个百分点 | 产品/渠道结构、成本、费用与研发说明 |
| R03 | 正归母净利下经营现金流转负；或同口径经营现金流/净利润低于 0.8 且同比下降 ≥0.2 | 现金流说明、应收、合同负债和营运资金附注 |
| R04 | 应收或存货同比增速超过营收同比增速 ≥20 个百分点；或其可比周转天数同比上升 ≥20% 且增加 ≥30 天 | 应收、存货、渠道及产销说明 |
| R05 | 合同负债同比下降 ≥20%；或有息负债/总资产同比增加 ≥5 个百分点 | 预收/渠道、债务与现金流附注 |
| R06 | 本期新增记录含正额商誉减值；或资产/信用减值损失金额达到上期末归母净资产的 1% | 资产组、减值对象与假设；不以余额差代替减值 |
| R07 | 最新股权记录显示实控人改变；董事长/总经理/财务负责人变化；审计机构或审计意见类型变化 | 对应公告及治理/审计说明；不采独立审计 PDF |
| R08 | 并购、处置、融资或投资项目金额 ≥上期末归母净资产 5%；或已标记重大事项且涉及当前研究问题 | 具体交易、资金用途、承诺和状态条款 |
| R09 | 新担保或诉讼/处罚金额 ≥上期末归母净资产 1%；或涉及公司/关键人员立案、控制权和经营资质 | 对应担保/案件/监管公告；金额不明的关键事件保留触发理由 |
| R10 | 实施口径年度每股现金分红同比下降 ≥20%；分红/回购计划取消或回购用途改变；回购计划股数 ≥总股本 1% | 资本回报、资金安排与计划状态 |
| R11 | 更正/补充关联到正在使用的报告/字段，且已出现上述异常、源数据实质修订或有明确待回答问题 | 只读相关更正及受影响章节，保留旧版本和影响说明 |
| R12 | 新事件或事实直接影响已有研究问题、关键假设、风险跟踪项或结论失效条件 | 该问题的证据章节；不受金额阈值限制 |

触发计算的约束：

1. 流量指标比较同季度同比；没有单季时使用同长度累计并标明，不能把半年与一季度直接比较。存量比较同一季末，报告期和币种必须一致。
2. 利润盈亏切换直接触发 R01；比较基数为零或负数时不硬算普通同比。其他无法有效比较的字段记 `trigger_input_missing/definition_mismatch`，不是“未触发因此无异常”。
3. 比率变化使用百分点，不是把两个百分比相除。R03 使用经营现金流与同范围净利润，不能把合并现金流任意除以不同口径利润。
4. 金额阈值的分母必须是有效的上期末正归母净资产；分母缺失/非正时金额规则不可计算，明确的关键事件仍按 R07/R12 读取。
5. 减值只使用来源明确给出的损失/减值金额及其符号定义，不对含转回的净额随意取绝对值；单季/累计不同的触发保留期间标签。
6. 分红按所属利润年度汇总该年度全部已实施分配，只有两年分配均已完成时才比较年度降幅；不能把今年暂已支付的中期分红与上一完整年度比较。尚有预案/未完成分配时记待完成，取消计划仍可直接触发。未知进度码保留原码，不从金额非空推断已实施。

触发输入与本轮接口的对应关系：

| 规则 | 输入字段与计算约束 |
|---|---|
| R01 | F04 `TOTAL_OPERATE_INCOME, PARENT_NETPROFIT, DEDUCT_PARENT_NETPROFIT`；当期与去年同季形成同比，两个相邻季度的同比作差。缺单季时按 3.2 的同口径累计路径处理，并标明覆盖限制 |
| R02 | 同口径毛利率优先用 F02/F04 `OPERATE_INCOME, OPERATE_COST` 计算，报告累计与单季分开；B02 `gpMargin` 仅同义缺口或专属定义按需使用。费用率用 F04 `SALE_EXPENSE, MANAGE_EXPENSE, RESEARCH_EXPENSE` 除以 `OPERATE_INCOME`，分母须正且同口径，历史研发包含关系不重复相加 |
| R03 | F04 `NETCASH_OPERATE` 与同期间利润判断转负；现金利润比优先用完整同范围 `NETCASH_OPERATE / NETPROFIT`，报告累计与单季分开，仅正利润分母应用阈值。B06 `CFOToNP` 按需补充并核实定义，不硬替代不同分母 |
| R04 | F01 `ACCOUNTS_RECE, INVENTORY` 与 F04 营收；周转天数由已确认平均余额、收入/成本分母及期间天数计算，缺项留缺口。B03 `NRTurnDays, INVTurnDays` 按需补充并保留专属定义，不混用期间或分母 |
| R05 | F01 `CONTRACT_LIAB`；有息负债比的分子必须是已确认完整的有息债务范围，分母为 `TOTAL_ASSETS`。不得把全部负债或未区分性质的一年内到期负债直接冒充有息债务；范围不明时该子规则记输入缺口 |
| R06 | G12 `GOODWILL_CHANGE`；F02/F04 的 `ASSET_IMPAIRMENT_LOSS, ASSET_IMPAIRMENT_INCOME, CREDIT_IMPAIRMENT_LOSS, CREDIT_IMPAIRMENT_INCOME` 按适用会计格式选取，不能把同义损失/收益列重复相加；分母取 F01 `TOTAL_PARENT_EQUITY` |
| R07–R09 | G01/G06/C01 的实控、任职与 `ACCOUNT_FIRM`；审计意见标准字段取 F01 `OPINION_TYPE`，其他表同字段仅保留响应；G09–G11、A03/A04/A07/A08 供给事件金额和状态。并购细项未结构化时由轻量目录与已有问题路由，不能把无字段当无事件 |
| R10 | A01 `PRETAX_BONUS_RMB, REPORT_DATE, ASSIGN_PROGRESS`；A02 回购用途、进度和计划股数，结合 G02 同时点总股本。计划上下限均有时按上限达到 1% 安排阅读，并明确记录这是计划上限 |
| R11–R12 | 轻量公告目录、已取得版本变化、已有研究问题和引用关系；无需先抓所有正文来判定标题或关联关系 |

首次基线只建立历史数值和事件序列，不将每条旧任职/旧事件都报成“本次新变化”。仍有效的关键事项、固定年报/中报及已激活的研究问题按优先级入队；随后只对新增事实或版本变化执行增量触发。

### 6.3 合并阅读任务，控制文本噪声

- 同公司、同报告期、同材料的多个触发合并为一份问题包，保留所有触发指标与原因；不是一个字段调用一次 LLM。
- 优先读取已有结构化事实、已解析章节与旧答案。正文以内容哈希去重，章节以主题和内容版本复用，变化处和未回答问题才追加读取。
- 年报/中报一次解析、多主题复用；不重复下载同一附件，不因不同来源 URL 重跑 MinerU。
- 输出只保存新增事实、更新事实、解释证据、尚未回答的问题及引用；没有新增证据也明确记录，不生成无关长摘要。
- 触发记录包含公司、期间、规则版本、输入事实 ID、阈值、目标章节/文档、结果和缺口。规则只能决定阅读优先级与范围，不能改变事实是否采集或直接写入风险结论。

## 7. 实施交接与验收边界

后续按 [新采集清单](eight-step-data-and-document-scope.md)、代码合同和[运行说明](research-data-layer-runbook.md)实施新范围、正文选择、格式适配与覆盖。本文及返回字段 JSON 供核对历史接口能力，不直接以 `columns=ALL` 样本参数生成新请求。

需要验证：新请求无无关日线及正文、必要估值输入仍可用、文件下载/解析/消费分开；并保留主源成功零备选、所选范围全分页/恢复、合法空结果、单位/期间、唯一主路由、非事实隔离、七家公司范围和 R01–R12 合并触发检查。旧快照、旧对账状态、旧报告和历史验收不变。

2026-09-08 当次证明限于字段结构与七家公司共同期间主营查询，后续原生产缓存的记录层证据另见新清单。2026-09-13 先完成范围文档，随后实施请求约束、原件解析、财务物化与逐题覆盖，并进行了本轮真实补采和增量验证；已支持字段、具体缺口及人工边界见 [数据层验证](research-data-layer-verification.md)。
