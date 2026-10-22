# 领域术语地图

按本次任务选择术语表，再沿跨域引用读取需要的概念。六域按业务职责组织，代码位置用于定位实现。

| 域与术语表 | 接收 → 交付 | 主要代码位置 | 涉及输入或接口变更时补读 |
|---|---|---|---|
| [采集 acquisition](docs/domains/acquisition/GLOSSARY.md) | 来源与请求范围 → 原始快照、派生产物和获取状态 | `src/analysis/acquisition/`；`src/analysis/structured/runtime.py` | 事实 |
| [事实 facts](docs/domains/facts/GLOSSARY.md) | 原始证据与字段契约 → 带口径、核验状态和血缘的事实 | `src/analysis/structured/`；`src/analysis/models.py`；`src/analysis/governance/` | 采集；派生公式变更再读计算 |
| [研究 research](docs/domains/research/GLOSSARY.md) | 公司、截止日和材料 → 采用的研究资料、假设和判断 | `src/analysis/research/` 的任务、资料与取证工具；`src/analysis/structured/scope.py` | 事实；按需补读采集、计算或知识 |
| [计算 calculation](docs/domains/calculation/GLOSSARY.md) | 事实、显式假设和方法 → 可复算的指标、估值与情景结果 | `src/analysis/research/calculations.py`、`custom_python.py`；`src/analysis/valuation.py`、`formulas.py` | 事实、研究 |
| [报告 reporting](docs/domains/reporting/GLOSSARY.md) | 研究正文、引用与计算结果 → 报告版本和各格式成品 | `src/analysis/research/drafts.py`、`reports.py`；`src/analysis/structured/reporting_bridge.py`；`src/analysis/exports.py` | 研究、事实；涉及数值再读计算 |
| [知识 knowledge](docs/domains/knowledge/GLOSSARY.md) | 文献、方法与适用条件 → 可定位、带版本的知识内容 | `src/analysis/knowledge/`；`config/methods/knowledge/`；`src/analysis/research/knowledge.py` | 涉及指标绑定读事实、计算；涉及问题路由读研究 |

## 常见任务怎么读

- 来源请求、分页、空响应和恢复：采集；影响事实准入时加读事实。
- 财务口径、单位、期间、可得时间和公司治理事件：事实；改公式时加读计算。
- 研究范围、资料补充、快照采用和问题覆盖：研究；按实际问题加读对应输入域。
- 指标、估值、情景和探索验证：计算、研究、事实。
- 章节、引用替换、报告状态和导出：报告、研究；数值或证据链变化时加读计算、事实。
- 文献方法、知识适用性、候选与发布：知识；按绑定内容加读相关域。

## 容易混淆的概念到哪里查

| 讨论对象 | 定义位置 |
|---|---|
| 取得的原始材料 / 某次研究采用的资料 | 采集的[来源快照](docs/domains/acquisition/GLOSSARY.md#来源快照) / 研究的[研究快照](docs/domains/research/GLOSSARY.md#研究快照) |
| 获取范围完成 / 问题材料就绪 / 判断形成 / 知识方法可用 | 采集的[采集覆盖](docs/domains/acquisition/GLOSSARY.md#采集覆盖) / 研究的[逐题资料覆盖](docs/domains/research/GLOSSARY.md#逐题资料覆盖)、[研究判断与完成](docs/domains/research/GLOSSARY.md#研究判断与完成) / 知识的[知识覆盖](docs/domains/knowledge/GLOSSARY.md#知识覆盖) |
| 本地获取时间 / 历史上可使用的时间 | 事实的[时间与研究截止](docs/domains/facts/GLOSSARY.md#时间与研究截止) |
| 查询返回为空 / 公司明确未披露 | 采集的[完整空查询](docs/domains/acquisition/GLOSSARY.md#完整空查询) / 事实的[缺失与不适用](docs/domains/facts/GLOSSARY.md#缺失与不适用) |
| 事实 / 研究陈述 / 假设 / 计算结果 | 事实的[数值事实](docs/domains/facts/GLOSSARY.md#数值事实) / 研究的[研究陈述](docs/domains/research/GLOSSARY.md#研究陈述)、[研究假设](docs/domains/research/GLOSSARY.md#研究假设) / 计算的[正式计算](docs/domains/calculation/GLOSSARY.md#正式计算) |
| 公司治理中的主体、任期、持股和事件重建 | 事实的[公司治理事实](docs/domains/facts/GLOSSARY.md#公司治理事实)；公司治理评价归研究 |

每个概念在所属域维护一次，其他域使用链接。字段、公式、枚举和版本合同引用现有代码及配置；发现不一致时记录具体差异。维护方式见 [Agent 领域规则](docs/agents/domain.md)，布局决策见 [ADR-0001](docs/adr/0001-domain-glossary-layout.md)。

首次切分的检查记录见[茅台研究链术语核查](docs/domains/moutai-trace.md)。
