## Purpose

为已选公司集合建立可恢复的结构化接入任务，使首次可得全历史与后续财务、行情和事件更新有清晰的范围、完成依据和缺口，重复执行能复用成功证据，来源故障不会被汇总或自动调度掩盖。

## ADDED Requirements

### Requirement: Versioned company and history scope
系统 SHALL 将贵州茅台及五粮液、泸州老窖、山西汾酒、洋河股份、古井贡酒、今世缘保留为首批已选集合。明确输入新公司时使用该公司的解析身份和选定同行，不回退成茅台集合；未指定公司或集合的调用须明确选择范围。保存目标/同行角色、选择依据和版本，每家取得适用数据集可免费提供的全部历史，展示五年/十二季度不得限制获取。

#### Scenario: Plan first full history
- **WHEN** 对首批集合创建首次计划
- **THEN** 计划包含七家公司适用数据集的历史枚举或供应商目录任务，明确当前快照/按需/不适用/缺口；不扩展到全市场或假称历史完成

#### Scenario: Default versus explicitly narrowed dataset scope
- **WHEN** 新策略同步未显式指定数据集，或用户明确指定一个子集
- **THEN** 前者覆盖所选公司全部适用数据集，后者仅声明该子集；旧模型默认 financials/market 和展示窗口不能暗中限制新策略

### Requirement: Frozen, idempotent scheduling and recovery
系统 MUST 冻结任务公司、数据集、来源/字段/调度策略版本、查询范围与参数；运行和页面保存追加证据。重复计划不重复创建同一任务，过期执行者不得提交结果；失败恢复从未完成范围继续，成功缓存不重复抓取。

#### Scenario: Crash after a committed page
- **WHEN** 第二页提交后进程退出并恢复相同任务
- **THEN** 第一、二页及记录复用，从未完成位置继续；所有提交与新尝试均可追踪，最终计数无遗漏或重复

#### Scenario: Configuration changes while a task is pending
- **WHEN** 任务创建后当前字段配置发生变化
- **THEN** 原任务只使用冻结版本；不匹配或无法恢复冻结配置时明确失败，不把当前配置偷偷替换进去

### Requirement: Frequency and publication aware incremental work
系统 SHALL 按北京时间交易日 19:00 更新日行情/估值，每日 20:30 更新目录和事件，财务随新报告，摘要随事件和每周检查，行业/概念/预测按需要。无更新标记事件使用 30 日重叠窗口及未完成事项刷新；来源延迟保持真实日期并在后续调度补齐。

#### Scenario: Market is closed or source is late
- **WHEN** 非交易日运行日任务或来源尚无当日数据
- **THEN** 使用交易日历跳过或记录来源延迟，不把旧报价标成当日；后续调度补齐缺失交易日

#### Scenario: No new financial report
- **WHEN** 缓存与目录没有新的报告期或相关事件
- **THEN** 系统复用已有财务序列，不逐日全量重抓历史三表

### Requirement: Bounded source access and explicit failures
系统 MUST 按来源单并发、请求完成后至少三秒限速；巨潮保持已有五秒直连策略。重试有上限，403/429/挑战/传输失败和业务空结果分别记录，缺口按已登记备用处理，不绕过访问控制。BaoStock 原生 SDK 结果不得伪装成真实 HTTP 响应。

#### Scenario: Concurrent workers reach the same source
- **WHEN** 两个工作进程同时执行同来源任务
- **THEN** 来源请求串行且遵守间隔，租约失效的工作者停止提交；运行结果仍分别保存

### Requirement: Status accounts for all pages and partial results
系统 SHALL 提供计划、运行/恢复、状态、数据集覆盖及记录查询入口。统计必须覆盖超过 500 条的任务/记录并区分成功、空结果、失败、待归类、未完成和按策略未取正文；局部失败不得使其他已验证字段完全不可消费。

#### Scenario: More than 500 work items include a late failure
- **WHEN** 计划超过 500 项且最后一页存在失败
- **THEN** 汇总包含该失败与准确总数，整体保持部分完成；恢复和去重读取同样不截断

#### Scenario: New full-history synchronization is queued
- **WHEN** 网页发起新策略全历史同步
- **THEN** 系统持久化任务并以 202 返回任务 ID，由独立执行入口推进；排队不显示同步成功，已成功分区可消费但整体状态仍如实显示进度

### Requirement: Production execution is distinct from software acceptance
系统 SHALL 提供可复现的全历史和增量执行命令；新软件的默认构造、文档验证和测试不得自动运行生产归档。实际联网结果必须明确公司、数据集、时间范围及隔离库，未跑全历史和人工验收保持未完成。

#### Scenario: Build or validate the new configuration
- **WHEN** 执行注册表检查、离线测试或打开计划
- **THEN** 不访问真实来源、不修改生产库、不创建操作系统定时任务

### Requirement: New company resolution precedes scoped acquisition
系统 SHALL 接受股票代码、带交易所代码或公司名称，通过已登记主数据和必要的身份前置工作解析唯一公司/证券关系。保留现名/曾用名和有效日期；未知、同名多候选、非支持市场不得猜选公司或仅按代码前缀推断。缓存不足的零网络计划只能列出前置任务，状态查询不隐式联网。

#### Scenario: Ambiguous company name
- **WHEN** 输入名称对应多个上市主体或多种证券且无法唯一匹配 A 股标的
- **THEN** 返回候选和需明确的身份，目标财务及正文请求为零；不会使用默认 600519 或任意第一条结果

#### Scenario: Unique company outside the first seven
- **WHEN** 一个新 A 股代码被唯一解析且该市场/公司类型适配可用
- **THEN** 为该公司创建通用数据与八步需求计划，行业专用缺口独立列出；未启用同行时不抓旧七家公司

### Requirement: Industry profile is versioned and does not silently default
系统 MUST 分开供应商行业标签、可用主营证据、报表 companyType 和研究行业画像。根据版本化规则确定通用、行业专用和条件问题；主营混合时组合适用画像并按分部处理。未知或相互矛盾时可继续独立通用获取，行业相关问题待补，不能静默采用普通工商估值或跳过所有行业要求。

#### Scenario: Financial company has conflicting classification
- **WHEN** 标签、报表类型或主营证据不能支持同一个行业画像
- **THEN** 保存矛盾及候选，保留已知通用事实；相关行业/估值问题显示待确认，不触发通用 FCFF 作为兜底

### Requirement: Peer selection is bounded, explained and separate from fact truth
系统 SHALL 优先使用目标公司适用的已选同行版本，否则按同一分类体系发现候选并依据主营、模式、地域、期间及规模规则形成有理由的研究同行。自动规则选择独立于供应商事实及用户确认，最多选六家进入默认全历史获取；不足时保留不足，不递归扩展同行的同行。不同估值指标可有不同可比子集，亏损公司不应因此从经营比较中删除。

#### Scenario: Same label without comparable business
- **WHEN** 候选与目标拥有同一概念标签，但缺少共同主营和模式证据
- **THEN** 候选不能自动成为已可比同行；必要元数据/主营筛查可安排，候选缺口不阻断目标公司独立财务获取

#### Scenario: Loss-making peer remains operationally relevant
- **WHEN** 同行通过经营可比性规则但当期亏损，PE 不具备经济可比性
- **THEN** 保留其经营数据与同行身份，只在 PE 比较中排除并解释原因，不用负 PE 填补比较数量
