## Purpose

本能力为公司业务与商业模式资料采集提供唯一、版本化且可审计的来源与查询目录，使系统只能调用已审核的访问方式，并能解释每个来源为何被尝试、跳过或禁止进入 LLM 处理链路。

## ADDED Requirements

### Requirement: 版本化主采与按需补充角色
registry 1.7.0 SHALL 固定 CNINFO 1.6.0 为 primary、SSE 1.4.0 为 on_demand 并引用 supplements_source_id=cninfo.disclosures。历史未声明角色的定义 SHALL 维持其主采解释。角色变化 MUST 新建来源版本；默认主采计划 SHALL 为补充来源生成无 I/O 的 on_demand_supplement 静态覆盖。当前 SSE DQBG 合同只支持定期报告，不得假定两个来源历史材料完全相同。

#### Scenario: 默认计划保留补充来源状态
- **WHEN** 使用 registry 1.7.0 为沪市公司生成默认主采计划
- **THEN** 巨潮适用查询 SHALL 进入必需计划，上交所 SHALL 保留 on_demand_supplement 静态处置且零 I/O

#### Scenario: 显式按需补缺
- **WHEN** 调用方引用已终结父运行中真实存在的必需材料缺口并指定适用的补充来源
- **THEN** 系统 SHALL 仅生成该来源支持的 query family 与缺口时间范围内的 ad_hoc reconcile；无缺口、未终结、市场不适用或协议不支持 SHALL 在 I/O 前拒绝

### Requirement: 首发检索参数与材料类型分离
registry 1.6.0 及后续版本的 CNINFO 首发检索 SHALL 使用经公开客户端协议核对的 category_sf_szsh，单项参数不附加分号。检索命中只能作为发现目录，不能自动将每行称为招股说明书。系统 SHALL 对标题和已验证正文前部单独分类，保留版本、标题类型、正文类型、证据状态及冲突。

#### Scenario: 首发类别返回不同材料
- **WHEN** 首发查询同时返回招股说明书、附录、上市公告书和股票发行公告
- **THEN** 材料分类 SHALL 保留四种不同类型，标题与正文不一致 SHALL 标为 requires_review，不得据查询类别覆盖实际材料类型

#### Scenario: 其他融资材料也含上市公告书字样
- **WHEN** 归档公告标题或正文封面明确指向债券或认沽/认购权证的上市公告书
- **THEN** 分类 SHALL 标为 other_financing，不能因“上市公告书”字样归入首次上市材料；新版分类派生 MUST 保留输入文本来源与旧派生版本

#### Scenario: 会议资料在正文中引用报告或招股书
- **WHEN** 正文前部先明确标识股东大会会议资料、董事会或监事会决议、议案，其后再出现定期报告或招股材料字样
- **THEN** 分类 SHALL 保留会议或决议公告属性，不得把议题引用当作材料封面；报告自身的更正或延期披露公告仍须与报告正文分开

#### Scenario: 报告标准声明与英文封面
- **WHEN** 报告封面之后存在董事会“本公告内容”真实性声明，或已验证封面使用 ANNUAL REPORT 加明确年份的英文标题
- **THEN** 分类 SHALL 根据实际报告标题保留报告类型，不得将正文声明作为报告相关公告标题；英文年报与其摘要 SHALL 分开

### Requirement: CNINFO nullable 空结果使用独立版本合同
CNINFO 新公告 schema 2 SHALL 仅在 page 1、`announcements` 存在且为 null、`totalAnnouncement` 为严格整数 0、`hasMore` 为布尔 false 时允许零行；出现的 `totalRecordNum|totalSecurities|totalpages` MUST 为严格整数 0。响应 MUST 只包含这六类字段及可选的 null `classifiedAnnouncements|categoryList`，未知字段、错误标记和不支持形态不得生成 no_data。此解释 MUST 由冻结的 schema id/version 启用，旧 schema 1 重放仍拒绝 null。新 registry 1.4.0 SHALL 只升级 CNINFO definition 为 1.3.0、公告 schema 与 execution key，不扩大端点、请求类别、速率、保存或 LLM 权限，也不声明旧 checkpoint 兼容。

#### Scenario: 有证明的 null 空结果
- **WHEN** 新版已冻结公告请求取得通过状态、挑战和 JSON 校验的 page 1 响应，且全部 nullable 空结果条件满足
- **THEN** 系统 SHALL 规范化为零行，冻结原始字节并生成唯一 terminal proof，只有完整证明闭合后才记录 no_data

#### Scenario: 错误或矛盾响应不得吞成空集
- **WHEN** null 公告响应包含正计数、布尔/字符串计数、缺失权威计数、hasMore 非 false、未知字段、错误标记或非第一页位置
- **THEN** 系统 MUST 拒绝解析并保持材料缺口，不得生成 no_data 或越过失败位置推进水位线

#### Scenario: 旧版本解释不可追溯变化
- **WHEN** 对旧 schema 1 的已冻结 null 响应执行无网络重放
- **THEN** 解析 MUST 继续失败，旧 run、快照、checkpoint 和注册表哈希保持不变

### Requirement: 版本化且不可变的来源定义
系统 SHALL 以不可变版本保存每个 `SourceDefinition`，且每个版本 MUST 包含稳定的来源定义 ID、版本、真实上游身份、显示名称、权威级别、访问方式、请求与逐跳重定向的允许域名/路径、许可与使用限制、可保留内容类型、适用公司/市场/主题、刷新频率、增量与重叠回看策略、来源时区与发布时间字段语义、响应 schema/MIME、请求速率与最大并发、超时/重试边界、最大响应字节数、有效期、查询定义以及 LLM 处理策略。已被运行引用的版本不得原地修改；任何实质变化 MUST 创建新版本并保留旧版本。

#### Scenario: 运行固定来源定义版本
- **WHEN** 系统创建一次采集运行
- **THEN** 运行记录 SHALL 固定每个来源定义的 ID、版本和注册表内容哈希，后续注册表更新不得改变该运行的解释结果

#### Scenario: 来源策略发生变化
- **WHEN** 某来源的端点、许可、适用主题、查询或 LLM 策略发生变化
- **THEN** 系统 SHALL 创建新的 `SourceDefinition` 版本，并保持引用旧版本的运行、尝试和快照可读

#### Scenario: 来源返回越界重定向或超大响应
- **WHEN** 任一重定向跳离固定版本允许的域名/路径，或响应超过该版本声明的最大字节数
- **THEN** 系统 SHALL 在读取越界正文前停止、记录机器可读策略/传输状态，并且不得跟随到未批准站点或创建正式证据

#### Scenario: v1 来源并发配置
- **WHEN** validator 加载 business_model v1 的四个来源定义
- **THEN** 每个定义的最大来源并发 SHALL 为 1，并具有正的最小请求间隔；要求更高并发的定义版本必须在本 change 中失败校验

### Requirement: 注册表严格校验与失败关闭
系统 MUST 在发起任何请求前校验注册表的 schema 版本、唯一键、请求及重定向 allowlist、查询参数边界、时间字段语义、分页终止证明、响应 schema/MIME、请求预算、许可状态、有效期和内容保留/LLM 策略。缺失必填字段、版本冲突、未知访问方式或越出允许边界的定义 MUST 使对应来源失败关闭，且不得临时退回硬编码 URL、默认全局网络配置或自由文本 provider。

#### Scenario: 非法注册表定义
- **WHEN** 一个来源版本缺少上游身份或允许域名，或者查询模板可访问未审核域名
- **THEN** 系统 SHALL 在联网前拒绝该版本、记录可诊断错误，并且不产生正式证据

#### Scenario: 注册表内容可复现
- **WHEN** 审计者使用运行固定的注册表版本和内容哈希重新加载定义
- **THEN** 系统 SHALL 得到相同的来源、查询、适用性和访问策略集合

### Requirement: 来源与业务问题的穷尽式适用性解析
系统 SHALL 从固定版本的业务问题清单和 `SourceDefinition` 查询定义生成来源 × 问题 × 时间范围计划。对合法、允许访问、适用于目标公司且与问题相关的每个主采查询 MUST 安排尝试；按需补充来源 MUST 保留有版本依据的静态处置，除非显式启动补缺；调用方不得通过省略来源或查询来把覆盖结果伪装为完整。对不适用或策略禁止的组合 MUST 生成带机器可读理由的 `policy_skipped` 覆盖项。

#### Scenario: 适用查询全部进入计划
- **WHEN** 一个有效来源版本包含三个适用于目标公司的业务问题查询
- **THEN** 运行计划 SHALL 包含三个查询的完整时间分片，除非某分片具有注册表声明的 `policy_skipped` 理由

#### Scenario: 沪市公司不适用深交所查询
- **WHEN** 为沪市公司 `600519` 生成 v1 覆盖计划
- **THEN** 主采巨潮及已 enabled 的主采贵州茅台 IR 适用查询 SHALL 被安排；上交所按新版本角色保留 on_demand_supplement 静态处置，旧版本角色不变；IR 尚为 `pending_policy/disabled` 时 SHALL 生成静态策略处置，深交所组合 SHALL 以“不适用市场”理由保留在覆盖清单中，二者均不得发起未批准 I/O

### Requirement: 物理查询去重与覆盖多对多追踪
每个 `SourceQueryDefinition` MUST 提供稳定 `execution_key`、query family、`query_stage=discovery`、所覆盖的业务问题 ID、时间/分页边界、响应 schema/version、discovery body 保留策略、资源提取规则、`fetch_policy=metadata_only|required_attachment` 和 canonical 资源规则。计划器 SHALL 以固定来源版本、请求方法/端点、`execution_key`、规范参数、查询分区、时间分片和分页语义生成物理计划单元，并在尚未创建 attempt 的 plan-only 阶段以不可变 `PhysicalQueryCoverageLink(plan_item_id, coverage_entry_id)` 建立多对多关系；同一物理发现查询不得仅因业务问题不同而重复联网。执行时每个 discovery/fetch attempt SHALL 引用对应 plan item，retry 可产生多个引用同一 plan item 的 attempt。上述任何影响实际请求或终止证明的要素不同，均不得错误合并。每个覆盖项 MUST 能经 plan link 反向列出实际贡献的 attempts，不能用共享查询结果省略问题级覆盖状态。

#### Scenario: 一个定期报告查询覆盖多个问题
- **WHEN** 同一来源、时间分片和参数的 `periodic_report` 查询同时映射 Q01、Q02、Q05 和 Q07
- **THEN** plan-only 运行 SHALL 创建一个物理 discovery plan item 和四个 PhysicalQueryCoverageLink；执行时 attempt 引用该 plan item，四个问题不得触发四次相同网络查询

#### Scenario: 查询参数不同不得合并
- **WHEN** 两个查询虽属同一 query family 但公告类别、市场分区或时间边界不同
- **THEN** 系统 SHALL 生成独立物理执行单元并保留各自覆盖关系，不得因 `execution_key` 配置错误而丢失任一范围

### Requirement: v1 业务采集来源边界
本能力的 v1 业务采集范围 MUST 仅包含巨潮资讯、上海证券交易所、深圳证券交易所和一个选定的贵州茅台官方投资者关系网站。第四个定义在 exact domain/path、许可与 LLM 策略完成人工核对前 MUST 保持 `pending_policy/disabled`；核对批准必须创建新定义版本，批准前不得产生正式证据。架构 SHALL 允许以后逐项增加监管部门、政府统计、行业协会和合法公开独立来源，但新增类别在获得独立定义版本和人工批准前不得成为 v1 正式证据，也不得形成通用互联网爬虫入口。

#### Scenario: 生成 v1 来源清单
- **WHEN** 调用方查询 `business_model` v1 的版本化来源目录
- **THEN** 系统 SHALL 返回上述四类来源定义、版本及 enabled/pending_policy 状态，并且不得混入聚合搜索结果、付费源或任意网站

#### Scenario: IR 策略尚未批准
- **WHEN** 贵州茅台 IR 定义尚未完成 exact allowlist 和许可人工核对
- **THEN** 该定义 SHALL 保留在 v1 范围中但保持 `pending_policy/disabled`，其覆盖项 SHALL 明确 policy skip，且不得联网或生成正式证据

#### Scenario: 未来来源类型尚未批准
- **WHEN** 系统识别到一个可能相关的政府、协会或独立网站但注册表不存在已批准版本
- **THEN** 系统 SHALL 仅生成待审核候选，不得请求其正文或把其内容标记为正式证据

### Requirement: 许可、访问限制与 LLM 处理策略
每个来源定义 MUST 分别声明“是否允许自动访问”“是否允许归档原文”“是否允许保存派生文本”“是否允许提交给 LLM”及其依据和限制。运行时发现登录、验证码、付费墙、robots/服务条款限制或许可不明时，系统 MUST 停止该访问路径并记录对应状态，不得设计或调用绕过机制。`llm_processing=denied` 的材料即使允许本地归档，也不得进入 Codex 输入清单。

#### Scenario: 站点要求登录
- **WHEN** 已批准公开端点在运行时返回登录要求
- **THEN** 尝试 SHALL 终止为 `login_required`，不得提交凭据、模拟登录或继续抓取受保护内容

#### Scenario: 允许归档但禁止 LLM
- **WHEN** 来源许可允许本地保存原文但来源定义禁止 LLM 处理
- **THEN** 系统 SHALL 按允许的保留范围创建合规本地快照，但 MUST 阻止其进入任何 Codex 证据清单并记录策略理由

### Requirement: 注册表外来源候选隔离
系统 SHALL 将注册表外的新域名或网站保存为 `SourceCandidate`，包含候选 URL/域名、发现时间、发现上下文、建议上游身份和审核状态。候选 MUST 与正式 `SourceDefinition`、`SourceRecord` 和证据快照隔离；只有人工审核后创建新的注册表版本，后续新运行才可使用该来源。

#### Scenario: 发现未注册 IR 页面
- **WHEN** 已批准页面链接到一个不在允许域名与路径规则内的新网站
- **THEN** 系统 SHALL 创建 `pending_review` 候选并停止访问该网站，且该候选不得出现在正式证据、事实或报告来源中

#### Scenario: 候选被批准
- **WHEN** 人工补齐许可、上游身份、适用性、查询和 LLM 策略并批准候选
- **THEN** 系统 SHALL 通过新的注册表版本启用它，且不得追溯修改批准前运行的覆盖结果

### Requirement: 访问来源与上游证据身份分离
系统 MUST 分开记录访问来源身份和真实上游证据身份。同一公告通过巨潮、交易所或公司网站镜像取得时 SHALL 保留每次访问尝试，但 MUST 通过 canonical ID 与内容哈希关联为同一上游材料，不能据此声称来源独立或“双源一致”。

#### Scenario: 同一 PDF 存在两个官方镜像
- **WHEN** 巨潮和上交所返回相同 canonical 公告且内容哈希相同的 PDF
- **THEN** 系统 SHALL 保留两个访问尝试和镜像 URL，但证据独立性计算 MUST 视其为同一上游材料

### Requirement: 计划主题到查询的完整追踪
版本化业务问题清单 MUST 将 [计划.md](../../../../../计划.md) 第一步的采集主题逐项映射到稳定 question ID，并至少覆盖：公司起源与业务构成/盈利模式、分产品和分地区收入毛利、客户/供应商集中度与渠道、单位经济与定价披露、产能/产量/销量/库存/利用率/在建产能、资本开支周期、研发投入与效率披露、上下游关系、战略与重大经营变化、技术/成本/渠道/品牌等竞争力声明及其可观察证据。每个 question ID MUST 映射到一个或多个已批准 source query 或明确的无适用来源状态；该追踪只证明采集覆盖，不得生成定性判断。

#### Scenario: 校验问题追踪矩阵
- **WHEN** 加载 `business_model` v1 问题清单与四个来源定义
- **THEN** validator SHALL 证明上述每个计划主题都有 question ID 和 source query/无适用来源解释，任何遗漏 SHALL 使注册表校验失败
