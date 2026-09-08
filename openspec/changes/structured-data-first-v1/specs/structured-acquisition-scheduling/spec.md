## Purpose

为已选公司集合建立可恢复的结构化接入任务，使首次可得全历史与后续财务、行情和事件更新有清晰的范围、完成依据和缺口，重复执行能复用成功证据，来源故障不会被汇总或自动调度掩盖。

## ADDED Requirements

### Requirement: Versioned company and history scope
系统 SHALL 默认使用贵州茅台及五粮液、泸州老窖、山西汾酒、洋河股份、古井贡酒、今世缘七家公司，保存目标/同行角色、选择依据和版本。每家取得适用数据集可免费提供的全部历史，展示五年/十二季度不得限制获取。

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
