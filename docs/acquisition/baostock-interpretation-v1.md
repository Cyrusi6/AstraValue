# BaoStock 后补字段解释 v1.0.0：证据与逐字段合同

本文件记录当前公开文档核实和旧缓存重新解释，不能作为旧运行当时已确认这些定义的证明。原注册表、原运行和 LEGACY 合同均未改变。

- 显式入口：`--interpretation-contract baostock-interpretation-v1.0.0`。
- 生效时间：`2026-09-12T06:20:00+00:00`；晚于全部引用文档获取时间。
- 合同 SHA256：`9521639a413e60c670eb00a892343e7825eaf8f4fb91f93244d0a0f9289e598d`。标识与哈希在 loader 中固定；同版本内容变化拒绝消费。
- 物化 manifest 冻结完整解释合同；原字段版本、原 quality/unit/standard ID、原期键和新解释定义 ID/版本分别保存。
- 原 `definition_unknown` 只在输入描述与已适配旧合同完全一致、来源/快照/成功 attempt/原值定位校验通过时补解释。failed/partial、预测、字段值冲突、来源版本冲突均不能借后补映射放行。

## 来源和单位判定

使用 BaoStock 官网首页公开 JS 中的文档菜单与只读文档接口；GET 首页/JS、POST 文档读取，不调用行情采集。通过 `http://127.0.0.1:7897` 请求。响应原始 UTF-8 文本保存在 `baostock-interpretation-evidence.v1.json` 的 `content` 中，保留原换行；SHA256 对 `content.encode("utf-8")` 计算。JSON 外层的 canonical SHA 避免 Git 换行改变文件字节所造成的假不兼容。每个规则还固定官方字段表原文、行号、URL 和文档 SHA。

| 官方文档 | 获取时间 UTC | 原始字节 SHA256 |
|---|---|---|
| [stockKData](https://www.baostock.com/helpdocs/api/markdown/stockKData.md) | 2026-09-12T06:14:14.832168+00:00 | `2a6db3b24ab21a199aa17bd949dfbe545d3279007bea1664854f9b7c20542aac` |
| [factorInfo](https://www.baostock.com/helpdocs/api/markdown/factorInfo.md) | 2026-09-12T06:14:14.858008+00:00 | `6820224b59a62b5e96b3347e0342263f0f9fb448a00a5c4ce1e61d2857b8984b` |
| [seasonProfit](https://www.baostock.com/helpdocs/api/markdown/seasonProfit.md) | 2026-09-12T06:14:14.818187+00:00 | `1d51b4abbd8db3f05934fc58b339a639978438f4cfaad5632d12240f20275afd` |
| [seasonOperation](https://www.baostock.com/helpdocs/api/markdown/seasonOperation.md) | 2026-09-12T06:14:15.760998+00:00 | `53da9ba813c249fcb5a44f52d37d308d45188fa29da9f3e2836fb4b024c38b04` |
| [seasonGrowth](https://www.baostock.com/helpdocs/api/markdown/seasonGrowth.md) | 2026-09-12T06:14:15.772632+00:00 | `3be700e9ac875e40454add8c9d2cdb825c6dba785e3aaebb38d9e8aba0ec1ab1` |
| [seasonBalance](https://www.baostock.com/helpdocs/api/markdown/seasonBalance.md) | 2026-09-12T06:14:15.806522+00:00 | `8b157300060ae0c33c6c3bcb747ecc6488f2c54a7e5a59f29eaeb5d165e63d63` |
| [seasonCashFlow](https://www.baostock.com/helpdocs/api/markdown/seasonCashFlow.md) | 2026-09-12T06:14:15.813616+00:00 | `fde08f2d70215d492d1f2a52cc92a49692c2853395d144e32951910f83f96cd0` |
| [seasonDupont](https://www.baostock.com/helpdocs/api/markdown/seasonDupont.md) | 2026-09-12T06:14:16.651259+00:00 | `1c4bcb18f258e17e375ce4074124843a055341f041c1dd0d6a334d01c526dc14` |
| [stockBasic](https://www.baostock.com/helpdocs/api/markdown/stockBasic.md) | 2026-09-12T06:14:16.669745+00:00 | `f8d5ba4ff8f3f664dc7f3fa8205a03e7827aa025044f16e4aa41aa5d3f0f70c8` |

日线 `turn/pctChg` 的精度/单位表和算法明确为百分数数字，标准小数倍率 0.01。财务表虽然部分标题含 `%`，官方返回示例为小数：净利润 28,522,000,000 / 收入 83,354,000,000 = 0.3421791395…，对应示例 `npMargin=0.342179`，因此财务比率倍率为 1。不能把两个接口的 `%` 表头机械地用同一倍率处理。

`totalShare/liqaShare` 是股本股数。官方示例为 28,103,763,899 股，`epsTTM` 公式使用 TTM 归母利润/最新总股本；结合日线换手率公式明确的流通股股数，按 shares 处理，无万元/万股倍率。EPS 单位为元/股，TTM 分母为最新股本，不伪装基本 EPS。

`netProfit/MBRevenue` 官方明确元，支持原数值投影，分别使用供应商专属 metric；官方季频文档只明确 `statDate` 报告期末，未对这些金额明确说明累计或单季窗口。因此使用 `reported_period`、`period_start=null`，逐事实记 `semantic_gaps=["period_window_unconfirmed"]`，不派生单季或 TTM，不替代三表主源。利润率、流量同比、现金流比率及杜邦指标的未明确窗口也同样处理。

营运能力的 90/180/270/360 天公式明确累计报告口径；周转率属于 ratio，不能做流量加减。存量比率为 instant；TTM EPS 为 ttm；行情指标保留 market_quote，不因名称含 TTM 就把报价日期改成财务期间。

复权因子只按原值和除权日投影，不按官方页面的简化公式重算价格；原缓存错误用 `__retrieved_at` 当期键的字段仍原样保留，新解释业务日期为 `dividOperateDate`。基本资料 status/type 为观察日的枚举，不能追溯到上市日。daily 的 adjustflag/tradestatus/isST 同样是 code/label，不是金额、资产或比率。

停牌时源给出的前收价格和成交量/额 0 可以保留；空换手率始终缺失，不采用官网示例把空值填 0 的便捷处理。负比率/负金额不截断；价格/成交量负值、停牌非零成交、涨跌幅零分母、复权状态与冻结请求冲突留明确缺口。

## 原合同适配

| 字段版本/哈希 | 来源版本/哈希 |
|---|---|
| 1.0.0 / `dc7e9581868f0331fa6856c3fa4658d66d0fbc3f41e0af2955360ce80cfbb618` | 1.0.0 / `1500f0f04e3ea61ca216e7c9a92bbd1ad82574663644d6c720e671ff8e66c424` |
| 1.1.0 / `4c8492349c6e490f173f538e7d2f59f380d3f08abaf58de7ae4f29423692e4f4` | 1.2.0 / `a97dacd2c9952037ab29b15d9bb327f73b0d88fe1ffcb2a3c9afed8a38f5963e` |
| 1.1.0（provenance migration） / `47c1d77755ff1c5fe34b5591554f015a9eb0e02fa7b0f53c2bf1ab68aa35b64b` | 1.2.0 / `a97dacd2c9952037ab29b15d9bb327f73b0d88fe1ffcb2a3c9afed8a38f5963e` |

这是同一字段语义合同在 provenance 文件迁移后的新哈希；旧哈希仍可回放，禁止把迁移哈希解释为新增字段语义。

## 61 个字段：输出单位、倍率、期间和实际数量

各行均引用上述同名数据集官方字段表。完整逐字段引文与定义定位在配置 JSON 中；本表数字来自真实缓存回放。数量 0 表示该字段本缓存没有可用数值，不表示其业务值为 0。

| dataset / 原字段 | 标准 metric | 原单位 → 标准单位 ×倍率 | value_kind / period_type | 事实数 |
|---|---|---|---|---:|
| baostock_adjust.adjustFactor | baostock_adjust_factor | ratio → ratio ×1 | ratio / instant | 31 |
| baostock_adjust.backAdjustFactor | baostock_backward_adjust_factor | ratio → ratio ×1 | ratio / instant | 31 |
| baostock_adjust.foreAdjustFactor | baostock_forward_adjust_factor | ratio → ratio ×1 | ratio / instant | 31 |
| baostock_balance.YOYLiability | liability_growth_yoy | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_balance.assetToEquity | asset_to_equity | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_balance.cashRatio | cash_ratio | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_balance.currentRatio | current_ratio | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_balance.liabilityToAsset | liability_to_asset | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_balance.quickRatio | quick_ratio | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_basic.status | baostock_listing_status | code → code ×1 | label / instant | 1 |
| baostock_basic.type | baostock_security_type | code → code ×1 | label / instant | 1 |
| baostock_cash_flow.CAToAsset | current_assets_to_assets | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_cash_flow.CFOToGr | operating_cashflow_to_gross_revenue | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_cash_flow.CFOToNP | operating_cashflow_to_net_profit | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_cash_flow.CFOToOR | operating_cashflow_to_revenue | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_cash_flow.NCAToAsset | noncurrent_assets_to_assets | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_cash_flow.ebitToInterest | ebit_interest_coverage | ratio → ratio ×1 | ratio / reported_period | 0 |
| baostock_cash_flow.tangibleAssetToAsset | tangible_assets_to_assets | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_daily.adjustflag | baostock_adjustment_type | code → code ×1 | label / market_quote | 6074 |
| baostock_daily.amount | market_amount | CNY → CNY ×1 | flow / market_quote | 6074 |
| baostock_daily.close | market_close | CNY → CNY ×1 | stock / market_quote | 6074 |
| baostock_daily.high | market_high | CNY → CNY ×1 | stock / market_quote | 6074 |
| baostock_daily.isST | baostock_is_st | code → code ×1 | label / market_quote | 6074 |
| baostock_daily.low | market_low | CNY → CNY ×1 | stock / market_quote | 6074 |
| baostock_daily.open | market_open | CNY → CNY ×1 | stock / market_quote | 6074 |
| baostock_daily.pbMRQ | pb_mrq | multiple → multiple ×1 | ratio / market_quote | 6074 |
| baostock_daily.pcfNcfTTM | pcf_net_cashflow_ttm | multiple → multiple ×1 | ratio / market_quote | 6074 |
| baostock_daily.pctChg | price_change_rate | percent → ratio ×0.01 | ratio / market_quote | 6074 |
| baostock_daily.peTTM | pe_ttm | multiple → multiple ×1 | ratio / market_quote | 6074 |
| baostock_daily.preclose | market_previous_close | CNY → CNY ×1 | stock / market_quote | 6074 |
| baostock_daily.psTTM | ps_ttm | multiple → multiple ×1 | ratio / market_quote | 6074 |
| baostock_daily.tradestatus | baostock_trading_status | code → code ×1 | label / market_quote | 6074 |
| baostock_daily.turn | turnover_rate | percent → ratio ×0.01 | ratio / market_quote | 6000 |
| baostock_daily.volume | market_volume | shares → shares ×1 | flow / market_quote | 6074 |
| baostock_dupont.dupontAssetStoEquity | dupont_asset_to_equity | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_dupont.dupontAssetTurn | dupont_asset_turnover | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_dupont.dupontEbittogr | dupont_ebit_to_revenue | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_dupont.dupontIntburden | dupont_interest_burden | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_dupont.dupontNitogr | dupont_net_profit_to_revenue | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_dupont.dupontPnitoni | dupont_parent_profit_to_net_profit | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_dupont.dupontROE | dupont_roe | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_dupont.dupontTaxBurden | dupont_tax_burden | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_growth.YOYAsset | asset_growth_yoy | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_growth.YOYEPSBasic | basic_eps_growth_yoy | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_growth.YOYEquity | equity_growth_yoy | ratio → ratio ×1 | ratio / instant | 78 |
| baostock_growth.YOYNI | net_profit_growth_yoy | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_growth.YOYPNI | parent_net_profit_growth_yoy | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_operation.AssetTurnRatio | asset_turnover | multiple → multiple ×1 | ratio / cumulative | 78 |
| baostock_operation.CATurnRatio | current_asset_turnover | multiple → multiple ×1 | ratio / cumulative | 78 |
| baostock_operation.INVTurnDays | inventory_turnover_days | days → days ×1 | ratio / cumulative | 78 |
| baostock_operation.INVTurnRatio | inventory_turnover | multiple → multiple ×1 | ratio / cumulative | 78 |
| baostock_operation.NRTurnDays | receivables_turnover_days | days → days ×1 | ratio / cumulative | 68 |
| baostock_operation.NRTurnRatio | receivables_turnover | multiple → multiple ×1 | ratio / cumulative | 68 |
| baostock_profit.MBRevenue | baostock_main_business_revenue_reported | CNY → CNY ×1 | flow / reported_period | 40 |
| baostock_profit.epsTTM | eps_ttm | CNY_per_share → CNY_per_share ×1 | ratio / ttm | 78 |
| baostock_profit.gpMargin | gross_profit_margin | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_profit.liqaShare | baostock_liquid_shares | shares → shares ×1 | stock / instant | 78 |
| baostock_profit.netProfit | baostock_net_profit_reported | CNY → CNY ×1 | flow / reported_period | 78 |
| baostock_profit.npMargin | net_profit_margin | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_profit.roeAvg | roe_average | ratio → ratio ×1 | ratio / reported_period | 78 |
| baostock_profit.totalShare | baostock_total_shares | shares → shares ×1 | stock / instant | 78 |

## 剩余缺口与验收边界

- 全部有可核实数值的实际字段已登记；61 个规则中 60 个在缓存产生事实。`ebitToInterest` 78 条全空、`turn` 74 空、`NRTurnRatio/NRTurnDays` 各 10 空、`MBRevenue` 38 空，共 210 个空值，全部留原字段缺口。
- 1,444 条报告期末事实未确认累计/单季窗口，不能被期间公式或三表主源代替消费；此限制逐事实、summary、合同均可查询。
- `code/date/pubDate/statDate/ipoDate/outDate/code_name/dividOperateDate` 为身份、日期或文本，保留原证据与上下文，不映射成财务数值；`__retrieved_at` 31 条是旧运行补入的技术字段，仍为 unknown_field。基本状态另按观察时点投影。
- `baostock_calendar` 原缓存 0 条记录，本次没有日历事实；0 不能证明休市。日历字段尚未新增解释规则。其他供应商、经营分部和新事件定义不属于本轮新增映射。
- 100,189 条中有 18,224 个状态编码、81,965 个非状态数值；维度和事件在此缓存没有目标记录，现有投影支持由相关原有测试验证，不用空集合冒充本轮新增支持。
- 原库读入的是 2026-09-08 已观察的真实历史缓存；2026-09-12 只联网核实公开文档。本轮没有当前行情/财务联网采集，也没有人工黄金验收。

完整原值复算、locator、逐指标期间计数、文件哈希和未来定义过滤结果见 `baostock-interpretation-validation.v1.json`；本地完整事实见专项交付记录中的只读输出路径。
