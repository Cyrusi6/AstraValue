# 定制 Python 绘图与探索计算

模型可以用 Python 表达图表，也可以编写现有 calculate 尚不支持的算法。两者使用同一隔离运行环境，数据由研究工具从当前快照选择，模型不传数据文件路径。已有正式财务公式继续调用 calculate，避免在不同脚本里维护多个版本。

部署机器先启动 Docker Desktop 的 Linux 容器引擎，再在项目根目录构建一次运行环境：

```powershell
docker build -f docker/research-python/Dockerfile -t astravalue-research-python:1 .
```

依赖版本、基础镜像摘要与中文字体写在Dockerfile及requirements中。构建需要联网下载依赖；之后每次研究只使用本地镜像，不自动拉取或安装软件。代码运行不继承宿主的凭据环境变量。

## 输入与运行

run_python_analysis 接收 research_id、purpose、code、inputs 和 mode。inputs 是命名数据表选择器，支持选择事实引用、指标及期间、正式计算结果，以及经验证的探索结果。工具保留期间、单位、主体、币种和来源；不会把当前公司之外的数据或整套数据库交给脚本。assumptions 单独记录模型假设及理由，不能覆盖历史事实。

代码通过 data 变量访问输入，可使用 pandas、NumPy、Matplotlib、Decimal 与 Python 标准库。mode="chart" 时提供 fig 和 chart_data；前者是 Matplotlib 图，后者保存实际绘图数据。mode="calculation" 时提供 JSON 对象 result，并说明定义、适用条件、输出单位和期间。运行返回analysis_id、exploration_id及绘图时的chart_id；get_python_analysis 可按需读取代码和结果，较大嵌套内容用result_path及分页继续读取。

例如inputs={"history":{"metric_ids":["net_profit","operating_cash_flow"],"periods":["2024-12-31","2025-12-31"],"period_type":"cumulative"}}。代码读取data["tables"]["history"]["rows"]，每行包含metric_id、period、period_type、value、unit、currency、scope、fact_ref等。原始value保持源数值表达，可转Decimal或数值数组；只为绘图转换成亿元不改变来源金额。

运行环境是 Docker Linux 容器。研究运行不联网、不挂载项目目录或数据库；选定数据和代码只读挂载。默认每次30秒、512MB内存、1个CPU、64个进程、16MB输出和16KB日志。宿主等待实际执行退出，暂停容器后，通过独立只读导出器取回白名单文件；输出使用有容量上限的内存卷，不挂宿主可写目录。完成后清理本次容器及内存卷。

异常或超限保留执行记录，不登记为成功结果；源码、数据及语义完全相同的成功调用复用产物，修改输出单位或定义会产生新的未验证结果。retry=true显式重试，保留原失败记录；相同实现与输入累计失败3次后需修正代码或输入，改描述、注释不能重置失败次数。没有可用沙盒时返回原因，不回退到宿主机直接执行模型代码。单次验证最多240秒，保存各次执行结果。

## 图表进入报告

定制图保存图片、代码、绘图数据、输入来源和快照哈希。模型先调用 view_chart 查看图片，再通过 review_custom_chart 记录图义和数据检查；确认后使用原有 {{chart:图表ID}} 引用。画图成功不能替代检查。图表可以排序、缩放单位和改变呈现方式；新增财务推导先走计算和验证流程，再将其结果选作绘图输入。

## 自定义计算进入报告

探索运行完成后初始为 unverified，不能作为正式事实或估值输入。validate_python_analysis 使用单独的复算代码比较完整结果，重跑原实现检查可重复性，并执行有预期结果及预期失败的边界样例。代码还需明确公式、适用条件、输入口径与输出单位。output_unit可用统一单位字符串，或用字段路径到单位的映射表示多种单位；映射需覆盖每个数值结果。相同实现换注释不算独立复算；给出“已检查”布尔值也不能通过验证。

通过后可在报告中引用“经验证的探索计算”，保留方法、适用条件和限制；它仍不进入标准指标库。验证说明由模型对研究方法负责，机械复算不等于方法在所有公司和经济情景中都适用。反复使用的方法应另行加入版本化公式、输入规则和回归测试，不自动晋升。

边界样例只修改本次测试副本，例如{"name":"zero_denominator","reason":"零利润不能作分母","overrides":[{"path":["tables","history","rows",0,"value"],"value":"0"}],"expected_error":"ZeroDivisionError"}。正常边界提供expected_result替代expected_error。需至少一个正常样例和一个预期失败样例；修改路径限已有数据值和假设值，不允许改公司、期间、单位和来源。两个实现都须通过这些样例，测试副本不登记为历史事实。

旧报告和正式 calculate 工具保持兼容。未引用的失败实验不阻止组装其他报告；已引用的图表或探索结果若未检查、被修改或属于其他快照，则拒绝组装。

真实缓存验收脚本为scripts/verify_custom_python_research.py，先运行--stage run，取得茅台定制图和五年累计现金回收比例的探索计算。模型实际view_chart并完成review_custom_chart后，再运行--stage report验证MD/HTML组装；--stage mcp验证真实stdio调用、图片传输、审阅及独立计算验证。脚本使用独立状态目录，不覆盖现有茅台研报，测试文字不作为投资研究成果。

2026-09-19验收：研究相关196项测试通过（含11项真实Docker测试及真实缓存导出测试），另14项处理层测试通过。茅台2021—2025年10个正式事实完成定制图及五年累计经营现金流/合并净利润计算，结果86.08%，NumPy与Decimal独立实现一致；零分母、相等输入、重复运行均验证通过。模型实际查看图像，逐点核对金额换算，MD/HTML及报告JSON保存了引用与完整记录。真实MCP返回的PNG与已检查图像哈希一致。验收产物见output/research/Python沙盒验收-20260919；双宿主完整研究仍按9.11独立验收。
