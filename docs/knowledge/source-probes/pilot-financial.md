# 首批财务方法：来源正文复核

2026-09-19 首批财务资料由并行任务取得，本任务以只读方式复用原文并亲自核对下列相关页。来源正文和整份 PDF 不提交 Git；三个方法的结构测试、内容审阅与用户人工验收分别记录。

| source_id | 原作者、标题与版本 | 原始 URL（本次最终 URL相同） | 取得时间（UTC） | 访问状态及 SHA-256 |
|---|---|---|---|---|
| SRC-NYU-RETURN-2007 | Aswath Damodaran, *Return on Capital (ROC), Return on Invested Capital (ROIC) and Return on Equity (ROE): Measurement and Implications*, July 2007 | https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/returnmeasures.pdf | 2026-09-19T08:12:56Z | 200，69页PDF；`aa3bd7206936ef4f6219c97dcc5bfe358363a6f14c08572731f2a6d37c612af6` |
| SRC-NYU-WC-CH10 | Aswath Damodaran, *Investment Valuation*, Chapter 10 “From Earnings to Cash Flows”；独立发布日期未核明，unknown | https://pages.stern.nyu.edu/~adamodar/pdfiles/valn2ed/ch10.pdf | 2026-09-19T08:13:44.773168+00:00 | 200，32页PDF；`104701b8507f5c02e46a70c1934eca6a5ea636b7cd42a6224445191b58927f9d` |
| SRC-NYU-FINANCIAL-FIRMS | Aswath Damodaran, *Valuing Financial Service Firms*；独立发布日期未核明，unknown | https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/finfirm.pdf | 2026-09-19T08:13:44.772164+00:00 | 200，44页PDF；`f880d1a0887e5632ff39d776e151bcff416b3ae10b3ad6f7df192b42d1595160` |

这三份材料是原作者公开提供的研究/教材材料，不是会计准则或银行监管规则。公开可读不自动等于获准再分发；项目只发布自写规则解释和引用定位。章节日期未知不影响用实际内容哈希固定版本，取得日期不冒充发表日期。

本任务实际阅读及支持关系如下，页码均为 PDF 页与印刷页一致的页面。

| 来源及定位 | 支持 | 不支持 |
|---|---|---|
| Return Measures pp7–8 | ROIC分子为税后经营收益；EBIT按税率调整，或对净利润做相容的税后利息及非经营调整 | 净利润可不经调整代替NOPAT；EBIT减含利息抵税的实际税额可无条件使用 |
| 同文 p8脚注2 | EBIT100、利息60、税率40%时NOPAT60；实际税16不应导出NOPAT84 | 将税盾重复计算后据此宣称超额回报 |
| 同文 pp10–11及脚注6 | 期初资本是文中主约定；平均资本为另一约定，与现金流时间设定有关 | 二者无条件相同，或缺期初值可静默用期末值代替 |
| 同文 pp10–12 | 资产/资本路线可能因投资及长期非债务负债不同；账面回报未必等于真实项目回报；负权益ROE无正常经济意义 | 单个高会计ROIC或负权益下高ROE即可证明优秀经营 |
| Chapter10 p13 | 再投资含净资本开支和非现金营运资本；资本开支会成块发生，研发和并购亦影响经济再投资 | 低资本开支的一年自动代表可持续高自由现金流 |
| Chapter10 pp21–22 | 估值营运资本通常排除现金及有息债务；必要经营现金可另行判断；增加占款、减少释放 | 普通流动资产减流动负债与估值营运资本完全等同 |
| Chapter10 pp23–24 | 异常基年使营运资本预测失真，应结合收入/成本及历史或行业；是否细拆取决于项目行为 | 应付增加或负营运资本自动判好/坏；所有公司固定用同一比例 |
| Financial Service Firms pp6–7 | 银行营运资本变化可能与增长再投资无关；普通FCFE/资本成本定义存在困难，应考虑权益估值或重定再投资 | 本次已经建成银行手册、监管资本公式或银行公司估值 |

正文“缺失不填零”“代理量不能换名FCFF”“逾期应付释放不等于经营改善”是依上述定义和组成关系写出的项目综合解释，不伪称原文逐字规定。逾期应付的合成案例只对已给出的逾期事实作限定解释，不把所有应付增加归类为负面。

缓存副本：`var/research/knowledge-sources/damodaran-return-measures.pdf`、`working_capital.pdf`、`financial_firms.pdf`。原始取得元数据分别可在同目录的 `*.probe.json` 与 `financial-availability.json` 核对。Return Measures p8/10/11/12 和 Financial Firms p6 的渲染由取得任务核过；本任务另读完整相关页文本，不把他人的核页记为自己完成的图像审阅。
