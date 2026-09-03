## Purpose

本能力定义治理与管理层资料采集的稳定问题范围、历史覆盖、来源角色和跨 scope 身份，使每次运行都能证明查过什么、缺什么，并且不会与其他研究范围误共享状态或把发现线索升级为正式事实。

## ADDED Requirements

### Requirement: 治理采集范围具有全链路稳定身份
系统 SHALL 将治理采集标识为独立 acquisition scope，并 MUST 在运行、覆盖项、查询计划、checkpoint、证据 manifest、最新可消费批次以及 API/CLI 过滤条件中同时固定 acquisition_scope、question_set_id、question_set_version、query_pack_version 和 source_registry_version。不同 scope 或问题集的状态不得互相解析、推进或遮蔽。

#### Scenario: 商业模式和治理使用相同来源
- **WHEN** 同一公司通过同一正式来源分别执行 business_model 和 governance_management 运行
- **THEN** 系统 SHALL 保留两个 scope 的独立 coverage、checkpoint 和 latest-consume selector，不得因 ticker 与来源相同而共享进度

#### Scenario: 问题集升级
- **WHEN** 治理问题语义或历史窗口发生实质变化并发布新版本
- **THEN** 新运行 SHALL 固定新问题集与查询包版本，旧运行、manifest 和 snapshot 仍按原版本可解释

### Requirement: v1 问题集完整覆盖治理主题
governance_management v1 问题集 MUST 精确包含 GOV.Q01.OWNERSHIP_CONTROL、GOV.Q02.PLEDGE_FREEZE、GOV.Q03.BOARD_COMMITTEES、GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND、GOV.Q05.REMUNERATION_INCENTIVES、GOV.Q06.RELATED_PARTIES、GOV.Q07.EXTERNAL_AUDIT、GOV.Q08.INTERNAL_CONTROL_CORRECTIONS、GOV.Q09.REGULATORY_DISCLOSURE、GOV.Q10.LITIGATION_COMMITMENTS 和 GOV.Q11.GOVERNANCE_RULES。每个问题 MUST 映射一个或多个批准查询，或具有明确的无适用来源处置；标题分类和事件 taxonomy 不能替代问题级覆盖证明。

#### Scenario: 校验完整问题追踪
- **WHEN** validator 加载 governance_management v1 问题集和查询包
- **THEN** 上述精确 ID 集 SHALL 均具有历史范围、预期记录类型以及批准查询或机器可读无适用来源理由，任何缺失、重复或未知 ID MUST 使校验失败

#### Scenario: 标题命中但正文不足
- **WHEN** 一个公告标题命中“管理层变动”而正文没有足够证据支持具体人员、职务或时间
- **THEN** 对应查询覆盖可记录已取得材料，但系统 MUST 不把标题命中视为事实抽取完成或问题覆盖完整

### Requirement: 历史窗口按问题语义生成
系统 SHALL 按问题语义生成覆盖窗口：控制关系、管理层任期、审计机构、监管事项、重大诉讼和承诺覆盖上市以来；薪酬、关联交易和年度治理/内控明细覆盖最近五个完整会计年度；激励、质押和其他跨期事项覆盖与目标时点相交的完整生命周期。调用方不得用固定数量上限或统一五年窗口把覆盖结果标为完整。

#### Scenario: 上市超过十年的公司执行 baseline
- **WHEN** 系统为上市超过十年的公司规划管理层任期 baseline
- **THEN** 查询计划 SHALL 从上市日或可证明的最早正式边界开始，并且不得因默认 5 年或 10 年窗口截断后仍声称 complete

#### Scenario: 跨越五年以上的激励计划
- **WHEN** 一项激励计划在最近五年窗口之前设立但仍影响查询时点
- **THEN** 系统 SHALL 获取其可得的设立、授予、归属、变更和终止生命周期，不得只保存最近五年增量

#### Scenario: 可变当前状态网页
- **WHEN** 公司治理网页只展示当前名册且不存在已冻结历史版本
- **THEN** 系统 SHALL 从首次可证明归档版本开始记录当前观测，且不得把该页面伪装成上市以来完整 baseline

### Requirement: v1 权威来源和发现来源严格分层
治理 v1 的 authoritative 来源 MUST 限于发行人正式披露、证监会及其派出机构、证券交易所正式披露和监管资料。AKShare、聚合接口、搜索结果页及搜索摘要 MUST 标记为 discovery_only，只能帮助定位正式原文；法院、工商和中登来源 MUST 在 v1 保持 deferred。来源角色不得仅因内容一致、人工阅读或 Codex 引用而自动升级。

#### Scenario: AKShare 返回管理层名单
- **WHEN** AKShare 返回与年报相同的姓名和职务
- **THEN** 系统 SHALL 只保存发现线索并重新定位正式原文，AKShare 结果不得进入权威 claim 或 snapshot

#### Scenario: 找不到正式原文
- **WHEN** discovery_only 来源给出一项重要质押或处罚线索但受控采集未找到合格正式原文
- **THEN** 系统 SHALL 保留 discovery lead 和对应 gap，不得把线索升级为权威事实

#### Scenario: 子任务发现延后来源
- **WHEN** 联网研究子任务找到法院、工商或中登材料
- **THEN** v1 SHALL 将其分类为 deferred 并阻止进入权威画像和 contextual evidence，不得以其内容填补覆盖

### Requirement: Coverage 证明执行而不制造负面事实
每个来源、问题、时间分片和查询 MUST 产生可持久化 coverage 结果，至少区分 success、no_data、partial、policy_skipped、not_applicable、restricted、retryable_failure 和 terminal_failure。no_data 只有在合法查询、完整分页和正常终止均得到证明时才可使用；任何 coverage 状态都不得自动推导“无质押”“无处罚”“无诉讼”或“未披露”。

#### Scenario: 完整查询返回空集
- **WHEN** 一个批准的监管查询完成全部分页并合法返回零条材料
- **THEN** coverage SHALL 标记 no_data 并保存终止证明，同时不得创建“公司无监管事项”的治理事实

#### Scenario: 查询中途受限
- **WHEN** 来源在第二页返回验证码、登录要求或 429 且剩余页未完成
- **THEN** coverage MUST 标记 restricted 或 retryable_failure 及未覆盖范围，不得降级为 no_data 或 complete

#### Scenario: 一个物理查询服务多个治理问题
- **WHEN** 相同来源、参数、时间分片和分页语义的定期报告查询同时覆盖多个治理问题
- **THEN** 系统 SHALL 只执行一次物理查询并为每个问题保留独立 coverage link，不能因去重丢失问题级状态

#### Scenario: 某问题只有权威单源
- **WHEN** 正式披露提供该问题唯一合格材料且未找到独立上游
- **THEN** 系统 SHALL 保留 authoritative_single_source 状态并允许使用该正式事实，不得伪造第二来源或删除该事实

### Requirement: 上游材料身份和访问镜像分离
系统 MUST 分别记录访问位置、真实上游材料、内容字节和独立性组。同一正式 PDF 通过巨潮、交易所或发行人镜像取得时 SHALL 保留各自访问 provenance，但 MUST 通过 upstream_material_id、content hash 和 independence_group 防止被计为多个独立来源。

#### Scenario: 两个镜像返回相同 PDF
- **WHEN** 巨潮和交易所返回同一上游公告且内容 hash 相同
- **THEN** 系统 SHALL 复用或关联同一内容 blob、保留两次访问观测，并在独立性计算中只计一个上游

#### Scenario: 同一 URL 内容变化
- **WHEN** 后续访问同一 URL 得到不同内容 hash
- **THEN** 系统 SHALL 创建新原始版本并保留版本链，不得覆盖旧字节或旧报告引用

### Requirement: 来源许可和访问边界失败关闭
每个治理来源定义 MUST 声明请求及逐跳重定向 allowlist、自动访问、原文归档、派生文本、LLM 处理、速率、并发、超时和响应大小策略。登录、验证码、付费墙、robots 或许可不明 MUST 停止对应访问路径并产生机器可读 coverage；系统不得调用绕过机制，也不得把受限材料泄漏给 Codex。

#### Scenario: 重定向离开 allowlist
- **WHEN** 正式查询重定向到未批准域名或路径
- **THEN** 系统 SHALL 在读取越界正文前停止并记录策略失败，不得创建正式证据

#### Scenario: 允许归档但禁止 LLM
- **WHEN** 来源策略允许本地冻结但禁止提交给 LLM
- **THEN** 系统 SHALL 保存合规原始快照并阻止其进入 Codex 输入包、工具返回和研究结果

#### Scenario: 来源访问失败
- **WHEN** 来源因网络、许可或速率限制暂时不可用
- **THEN** 系统 SHALL 保留可重试或受限状态与缺口，且不得阻塞其他来源的合法采集
