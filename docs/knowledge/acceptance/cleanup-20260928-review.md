# 当前知识候选阅读样本

候选：`cleanup-20260928-v2`  
内容哈希：`8837ddc717fb2664da0a75e0b4d611f4348ad0b583d58875d2164491f7557802`

以下是当前版本八步各一题的阅读与应用检查。Codex 已读取冻结内容并核算示例；用户人工阅读仍待完成，未发布默认包。例子全部为合成教学数据。

## ES01 · 利润由哪些产品、地区和渠道贡献？

A收入60/(60+40)=60%；B毛利20/(12+20)=62.5%。两者说明不同结构，缺共同费用、抵销或净利桥接时不能声称净利润贡献。删除分部成本后只保留收入结构，缺失不能补零。

方法正文：[产品地区渠道的收入与利润结构](../../methodology/knowledge/segment_economics.md)；方法版本 `1.0.0`。
主要边界：缺成本分摊或抵销，只列已披露指标并保留利润贡献缺口。；合成例仅说明判断边界，不能写成目标公司事实。
来源：[ifrs-8-operating-segments](https://www.ifrs.org/issued-standards/list-of-standards/ifrs-8-operating-segments/)，About全部概览段。
来源：[Management’s Discussion and Analysis, Selected Financial Data, and Supplementary Financial Information](https://www.sec.gov/files/rules/final/2020/33-10890.pdf)，Item303(b)(2)(i)–(iii)；PDF/印刷p165。

## ES02 · 营运资本占用和自由现金流如何？

经营占款变化=-5-10-(-5)=-10，即当期释放10；逾期应付增加10同样短期释放现金，但会增偿付和供货风险，不能称效率改善。完整FCFF=100+20-30-10=80；缺组成时不输出完整FCFF。银行贷款110/存款100不能套普通企业占款公式。

方法正文：[营运资本、再投资与自由现金流边界](../../methodology/knowledge/working_capital.md)；方法版本 `1.1.0`。
主要边界：只含应收+存货−应付时须证明其他经营科目不重要，否则只是代理。；现金流代理不自动等于FCFF/FCFE；行业未知仅给条件化指导。
来源：[Investment Valuation, Chapter 10: From Earnings to Cash Flows](https://pages.stern.nyu.edu/~adamodar/pdfiles/valn2ed/ch10.pdf)，PDF/印刷pp21–22。

## ES03 · 关联交易和承诺是否履行？

到期应收100且在期限内收到100，支持该笔义务已履行；存在审批但到期收到0时为该笔未履行，审批不是付款。只有审批、没有合同和履约凭证时状态未知，也不能据此判利益输送。

方法正文：[关联交易、条款与承诺履行](../../methodology/knowledge/governance_es03_q05.md)；方法版本 `1.0.0`。
主要边界：关联方定义、豁免与审批门槛依适用制度核对；跨境和金融关联交易需要行业补充。；仅凭披露不能裁定未披露合同或未来行为；不足部分明确列缺口。
来源：[G20/OECD Principles of Corporate Governance 2023](https://www.oecd.org/content/dam/oecd/en/publications/reports/2023/09/g20-oecd-principles-of-corporate-governance-2023_60836fcb/ed750b30-en.pdf)，PDF21/印刷19 §II.F.1；PDF32/印刷30 §IV.A.7；PDF40/印刷38 §V.D.7。

## ES04 · 并购、出售与商誉形成有哪些责任？

非同一控制全资收购、两边均公允价值且无其他调整时，120-100=20为商誉。商誉存量20未含减值测试结果，不能推减值20或协同已兑现。新版缺失例删除的是减值/履约证据，因此应限制相应判断，已不再误称缺交割日期。

方法正文：[并购出售、商誉及后续责任](../../methodology/knowledge/governance_es04_q05.md)；方法版本 `1.0.1`。
主要边界：同一控制、分步收购、跨境税务和特定行业资产估值需专用规则，IFRS概览不等于A股会计全文。；仅凭披露不能裁定未披露合同或未来行为；不足部分明确列缺口。
来源：[G20/OECD Principles of Corporate Governance 2023](https://www.oecd.org/content/dam/oecd/en/publications/reports/2023/09/g20-oecd-principles-of-corporate-governance-2023_60836fcb/ed750b30-en.pdf)，PDF17/印刷15 §II.B；PDF38/印刷36 §V.D.1。
来源：[IFRS 3 Business Combinations: About](https://www.ifrs.org/issued-standards/list-of-standards/ifrs-3-business-combinations/)，About, core principles and disclosures。

## ES05 · 同行估值是否可比？

两家5%增长、10%资本成本且现金转化近似，只支持候选可比，还需校准分子分母。2%/20%增长与8%/15%资本成本明显不同，同业低倍数可有基本面解释。只有12倍和20倍、缺增长风险时可报倍数差异，不能据此声称低估40%。

方法正文：[同行可比性与倍数差异](../../methodology/knowledge/valuation_es05_q03.md)；方法版本 `1.0.0`。
主要边界：回归相关性不能替代经济解释，样本少和共线性会削弱调整。；相对估值依赖市场平均定价，不证明内在价值。；定量输入映射待补，不代表已能从事实库直接取数；方法不生成目标公司判断。
来源：[Valuation Approaches and Metrics: A Survey of the Theory and Evidence](https://www.stern.nyu.edu/~adamodar/pdfiles/papers/valuesurvey.pdf)，PDF p58、62、65，Comparable Firms。

## ES06 · 哪些量价、份额或新业务变量驱动增长？

价格与范围稳定、实际交付销量增长20%，支持历史收入数量驱动解释；不能外推未来持续20%。行业总量增长20%但公司份额和交付未知时，公司增速仍未知，需查订单/交付、份额和产能约束。

方法正文：[增长驱动的可观察变量](../../methodology/knowledge/growth_drivers.md)；方法版本 `1.0.0`。
主要边界：缺销量/结构或新业务交付时只列候选驱动和待验证变量。；合成例仅说明判断边界，不能写成目标公司事实。
来源：[Management’s Discussion and Analysis, Selected Financial Data, and Supplementary Financial Information](https://www.sec.gov/files/rules/final/2020/33-10890.pdf)，Item303(b)(2)(i)–(iii)；PDF/印刷p165。
来源：[Producer Price Index Manual, Chapter 1](https://www.imf.org/external/np/sta/tegppi/ch1.pdf)，§§1.151–1.155；印刷p29/PDF33。
来源：[ifrs-8-operating-segments](https://www.ifrs.org/issued-standards/list-of-standards/ifrs-8-operating-segments/)，About全部概览段。

## ES07 · 行业边界与公司业务归属是什么？

特定工艺设备、运输成本高且替代记录明确，可以提出产品与地域边界，但仍应检验客户替代和供应扩张。硬件代工与订阅软件虽同属科技标签，盈利方式和客户用途不同，不能自动互为同行。缺范围定义时不计算精确市场份额。

方法正文：[行业边界与公司业务归属](../../methodology/knowledge/industry_boundary.md)；方法版本 `1.0.0`。
主要边界：缺替代和地理定义时保留宽/窄候选边界，不给唯一精确份额。；合成例仅说明判断边界，不能写成目标公司事实。
来源：[Merger Guidelines](https://www.justice.gov/atr/media/1329301/dl?inline)，§4.3；印刷pp40–43/PDF41–44。
来源：[ifrs-8-operating-segments](https://www.ifrs.org/issued-standards/list-of-standards/ifrs-8-operating-segments/)，About全部概览段。

## ES08 · 当前实际基线及关键缺口是什么？

EBIT120、capex30、折旧10和占款增加5可作为有定位的基线，但缺税率时FCFF只可写120*(1-t)+10-30-5，不能默设t。2026预测120不能登记为2025实际；占款变化缺失时不能补零完成模型。

方法正文：[实际基线、假设与缺口分栏](../../methodology/knowledge/valuation_es08_q01.md)；方法版本 `1.0.0`。
主要边界：当期尚未完整披露时不能把部分季度机械称为全年实际。；此处只规定核查步骤，不代表知识服务获取了公司数据。；定量输入映射待补，不代表已能从事实库直接取数；方法不生成目标公司判断。
来源：[Valuation Approaches and Metrics: A Survey of the Theory and Evidence](https://www.stern.nyu.edu/~adamodar/pdfiles/papers/valuesurvey.pdf)，PDF p7–8、26，cash-flow claims and FCFF inputs。

## 待人工阅读

- [ ] 八步样本的可支持判断和禁止推断符合预期。
- [ ] 方法正文、来源定位和边界能够理解并复核。
- [ ] 没有需要修改的重要问题；如有，记录具体问题后再生成新版。

未勾选项保持未验收。接受此知识候选不等于接受贵州茅台研报或证明公司数据完整。
