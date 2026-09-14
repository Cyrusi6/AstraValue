## Purpose

让公告目录承担事件发现和重要材料定位，正文只按固定重要报告和具体研究问题获取并精读，保留原文、页码与内容版本的复用关系，减少重复解析和无关摘要，同时不把未选择正文误判为采集失败。

## ADDED Requirements

### Requirement: Lightweight catalog and mandatory important reports
系统 SHALL 保留公告轻量目录，完整中文年报、中报固定进入精读队列，历史由近到远处理；季报和其他材料按触发条件选择。独立审计 PDF、英文年报不采正文，目录保留明确选择理由。

#### Scenario: Mixed announcement catalog
- **WHEN** 目录同时包含中文年报、年报摘要、英文年报和独立审计报告
- **THEN** 完整中文年报进入固定队列，其余按策略保留目录；未选正文不成为目录失败或正文缺失屏障

### Requirement: Versioned quantitative and event triggers
系统 MUST 落实字段附录 R01–R12 的阈值、事件、输入字段和比较约束。数值触发只影响阅读调度，不改变字段获取或直接产生风险结论；亏损切换、零/负基数、缺分母与口径不同均按附录处理。

#### Scenario: Material quarterly change
- **WHEN** 同口径单季营收同比变化绝对值达到 20%，或毛利率同比变化达到三个百分点
- **THEN** 生成含规则版本、实际输入、阈值与目标章节的阅读理由；不生成自动风险评级

#### Scenario: Missing trigger input
- **WHEN** 必要同期值、正净资产分母或单位定义缺失
- **THEN** 记录输入缺口而不是“未触发所以无异常”；明确控制权、关键人员或研究问题事件仍可触发

#### Scenario: Incomplete annual dividend
- **WHEN** 当前利润年度尚有未实施分配而上年已完成
- **THEN** 不将当前暂已支付金额与上年全年计算下降；取消计划等独立事件仍可触发

### Requirement: Targeted corrections and baseline semantics
系统 SHALL 仅对关联已使用报告/字段且存在实质修订、异常或具体问题的更正安排阅读，不因标题含“更正”就默认下载。首次历史基线不将每条旧事件当新变化，仍有效事项与研究问题可触发。

#### Scenario: Unrelated correction
- **WHEN** 新目录中的更正与当前使用材料和研究问题没有关联
- **THEN** 保留目录及选择理由，不发起默认正文请求

### Requirement: Merge reading work and reuse content
系统 MUST 将同公司、材料和内容版本的多个触发合并为一个问题包；优先已有正文、MinerU 解析、章节和旧答案，按内容 hash 去重，不因来源 URL 或主题不同重复下载/解析。输出记录新增/更新事实、解释依据、引用和未回答问题。

#### Scenario: One report triggers multiple topics
- **WHEN** 同一季报同时触发营收、现金流和存货条件，且已有有效解析
- **THEN** 生成一个含三项理由的定向阅读任务，引用已有解析与章节，额外下载和 MinerU 请求为零

### Requirement: Required reading resolves question inputs with explicit evidence
系统 SHALL 将八步问题所需的非结构化输入映射到具体重要报告章节和提取字段，复用已有 BM 主题、事实表和解析。只有绑定公司、期间、字段/原文及页码或历史 HTML 段落定位的可用证据才能满足该输入；材料存在、文本解析完成、候选关键词命中均不等于已回答。无披露、无可靠定位或阅读未完成各有原因，缺口不触发全部公告正文抓取。

#### Scenario: Annual report parsed but required quantity is unanswered
- **WHEN** 年报已有 MinerU 解析，但产量要求没有带单位和期间的有效证据
- **THEN** 产量问题保持待补，生成或复用经营数据章节的定向阅读项；不再下载和解析该文件，也不以营收估计产量

#### Scenario: Source explicitly states an event is absent
- **WHEN** 有效报告章节明确说明该公司在覆盖期间未实施股权激励
- **THEN** 保存有出处的无实施事件记录并据此判定该期间激励条款子问题不适用；不推广到公司所有历史期间
