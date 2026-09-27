# ROIC、ROE 与估值资料取得核查

核查时间：2026-09-19 16:12—16:20（Asia/Shanghai）。本记录只证明本次取得及注明位置的核查；研究意见不是 A 股会计强制规定，公开可读不代表可以再分发全文。

## 已取得并核对

1. Aswath Damodaran，*Return on Capital (ROC), Return on Invested Capital (ROIC) and Return on Equity (ROE): Measurement and Implications*，July 2007，69 页。2026-09-19T08:12:56Z 请求 `https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/returnmeasures.pdf`，原始 URL 与最终 URL 相同，HTTP 200，application/pdf，7,914,220 字节；SHA-256 `aa3bd7206936ef4f6219c97dcc5bfe358363a6f14c08572731f2a6d37c612af6`。
   - PDF 页码与印刷页码一致。p7 定义本年税后经营利润与期初投入资本；p8 解释税后 EBIT 与实际缴税不能混用，避免重复计算债务税盾。
   - p10—11 的 Timing Differences 明确期初资本口径，也注明期初期末平均资本做法及期中现金流惯例。这能支持保留多口径及其条件，不能支持无条件断言某一口径唯一正确。
   - p11—12 定义 ROE、非现金 ROE，说明权益为负时 ROE 缺乏有效经济解释；p39—40 提示回购/大额分红改变权益账面值，ROE 上升不直接等于经营效率改善；p46—47 说明母公司/合并持股范围的口径问题。
   - p61—63 讨论超额回报与稳定增长终值的关系；它不提供适用于所有公司的固定永久超额回报预测。
   - 正文已用 pypdf 提取，p8/p10/p11/p12 已渲染核对公式、页码及脚注。本文未定位到明确的 DuPont 三因素分解，因此不能以它冒充杜邦专门来源。
2. Aswath Damodaran，*Valuation Approaches and Metrics: A Survey of the Theory and Evidence*，November 2006，77 页。2026-09-19T08:14:45Z 取得；原始 URL `https://www.stern.nyu.edu/~adamodar/pdfiles/papers/valuesurvey.pdf`，最终 URL `https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/valuesurvey.pdf`，HTTP 200，application/pdf，1,602,127 字节；SHA-256 `9bb7f22c1a7a6b10e37421e126ff0c07b062425fe0b1f23d409516c3386897eb`。
   - p58—59 的 Relative Valuation 说明可比资产选择、价格标准化、控制差异及相对估值的市场定价前提。
   - p67—69 的 Controlling for Differences across Firms 说明主观调整、修改倍数和回归等处理差异的方法及限制；p73—74 说明共线性、时点变化与市场整体错价风险。
   - 这些段落支持估值方法选择与可比性限制，不支持仅因某倍数低于同行便判断公司低估。此次核对为正文文本层；公式密集页尚未逐页视觉审阅。

## 路径与失败记录

官方 papers 目录 `https://pages.stern.nyu.edu/~adamodar/New_Home_Page/papers.html` 于 2026-09-19T08:12:56Z 返回 HTTP 200，48,390 字节，SHA-256 `1b723f447345b7b68548f65548107a7d8e9af3f3917a7ee41c16634f95c73ddc`，直接链接以上研究论文。目录只作出处导航，不单独作为规则支持。

首次 urllib 直连 returnmeasures.pdf 出现 SSL EOF；随后 requests 直连成功，未绕过访问控制。尝试的 `https://pages.stern.nyu.edu/~adamodar/pdfiles/eqnotes/valn.pdf` 直连与 Clash HTTP 代理均为 HTTP 404，未作为有效来源。估值来源随后从官方目录实际链接取得。

原始 PDF、响应记录、提取页及核查图位于本 worktree 的 `var/research/knowledge-sources/`，该目录已由现有 `.gitignore` 排除。Git 仅保存上述简要核查及原创整理；全文再分发许可尚未确认。
