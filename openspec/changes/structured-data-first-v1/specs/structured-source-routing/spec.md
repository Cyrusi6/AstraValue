## Purpose

规定目标公司与同行的结构化字段取得方式，使每个标准字段拥有明确的主要来源和可解释的缺口，同时完整保留供应商返回字段、真实上游、分页范围及版本证据，避免成功接口被默认重复采集或截断。

## ADDED Requirements

### Requirement: Versioned dataset and field registry
系统 SHALL 以版本化注册表登记字段附录中的全部 55 个数据集、实际接口、原字段、数据性质、期间/单位、行键、历史方式、更新时间与主备路由。规划 JSON 不得直接作为可执行请求配置。原字段未知或未映射时 MUST 保留并列为待归类，不能静默删除或将其一律当客观数值。

#### Scenario: New upstream field appears
- **WHEN** 已登记接口返回一个新字段
- **THEN** 系统保存完整响应及字段登记，将新字段标为待归类；其他已知有效字段继续使用

#### Scenario: Invalid primary route
- **WHEN** 同一标准字段、期间和维度存在两个默认主源，或路由指向未知数据集
- **THEN** 系统在任何来源请求前拒绝该配置并指出冲突键

### Requirement: Primary success avoids fallback
系统 SHALL 先使用有效缓存或字段主源；成功且通过程序检查时不得额外请求备选。缺值、失败或校验失败才触发已登记同义备选，记录具体字段、原因及实际来源；定义/期间/范围不相同的备选不得冒充缺失字段。

#### Scenario: Partial field gap
- **WHEN** 主源响应中营收有效而现金流字段缺失
- **THEN** 营收直接可用，只有现金流进入补源；共享备选响应中的其他字段不覆盖成功主字段

#### Scenario: No valid fallback
- **WHEN** 备选不可用或只提供不同口径字段
- **THEN** 系统记录明确缺口或显式过期缓存，不无限重试、不启动全公告正文归档

### Requirement: Complete pages and history boundaries
系统 MUST 对每个已选公司和数据集保存真实查询范围、总数、页/游标、稳定记录键与终页依据，统计和恢复同样覆盖全部记录。超过任意展示上限的数据不能被计为已完成；接口只提供当前快照时不得伪造历史。

#### Scenario: Segment history exceeds wrapper limit
- **WHEN** 主营分页接口共有 446 条且单页小于总数
- **THEN** 系统遍历到终页并核对去重后记录数；返回前 200 条或中途失败时保持部分完成和恢复位置

#### Scenario: Repeated or changing pages
- **WHEN** 来源重复返回同一页、声明总数变化或终页累计数不一致
- **THEN** 系统保留已取得页面和受影响范围，报告分页不完整，不推进该范围的完成水位

### Requirement: Honest empty and unavailable responses
系统 SHALL 区分有效空结果、字段空值、不适用、来源失败与未披露。东方财富明确 9201 空响应可记为供应商本次无记录，但不得当成公司历史不存在；BaoStock 必须在成功结果码和耗尽结果集之后才能记空结果。

#### Scenario: HTTP success contains error payload
- **WHEN** HTTP 200 中为错误页、未知失败码或缺少必要响应结构
- **THEN** 系统记录来源/解析失败而不是无记录，并保留有限诊断证据

#### Scenario: Legitimate zero result
- **WHEN** 查询满足该接口版本的合法空结果合同
- **THEN** 记录该公司/查询范围本次为空及证明，不生成“没有处罚/没有历史公告”等事实

### Requirement: Field nature and period semantics
系统 SHALL 分开金额、时点存量、年度、累计、单季和 TTM；只在输入完整且同口径时派生，并保存输入与公式版本。标签、预测、来源文本及模型估计必须独立于一类字段，包含同一响应中的质押预警与项目预测。

#### Scenario: Quarterly API is not single-quarter data
- **WHEN** BaoStock 以 quarter 参数返回报告期指标，或东方财富同时返回累计和单季
- **THEN** 按具体字段定义标记期间，不因调用参数名将全部值当单季；缺少定义的字段保留原始命名空间

#### Scenario: Dividend and valuation units
- **WHEN** 来源返回每十股分红、百分数换手率和现金流量净额口径市现率
- **THEN** 显式换算单位并保留原字段；不得将市现率改标为经营现金流口径或将分红预案标为已支付

### Requirement: Eight-step questions have explicit input contracts
系统 SHALL 为设计附录的每个八步问题登记稳定 ID、必需与可选数据项、适用行业/事件条件、期间需求和数据性质。每项必须定位到具体数据集原字段、有完整依赖的公式、重要报告章节或带原因的缺口；假设、论点、模型运行等已有研究对象独立引用，不能伪装为供应商事实。不得以“其他财务数据”“待探索接口”等占位表达可执行路径。跨题展开不得形成循环就绪依赖；问题选择只决定研究输入和按需项，不缩减已选公司的全部适用客观字段获取。

#### Scenario: Registry contains an unmapped required input
- **WHEN** 必需项只有字段名称而没有取得路径，或公式有未登记输入
- **THEN** 注册校验拒绝将其标为已支持；允许以显式缺口登记，保留受影响问题及待补原因

#### Scenario: Optional field is absent
- **WHEN** 问题全部必需输入可用，但可选的调研回答或机构预测缺失
- **THEN** 可选缺口单列，不降低该问题的数据就绪状态，不为填满可选项强制下载无关正文

### Requirement: Industry-specific support is independently evidenced
系统 MUST 区分行业字段的原名称已见、定义已确认、公司类型协议已验证和实际公司/期间已取得。金融等行业不能因普通企业响应含同名空列即认定已支持；无法取得的行业必需值保持缺口，不能以通用三表或全体上市公司均值替代。

#### Scenario: Bank field name exists only in an industrial sample
- **WHEN** 普通企业 F05 响应包含净息差列，但没有对应银行类型的字段定义和有效记录
- **THEN** 将银行净息差列为行业映射/实测待补，保留确切候选字段；其他已通过的银行通用数据可以独立使用

#### Scenario: Industry market share lacks a compatible denominator
- **WHEN** 只有目标及六家同行的销售额，缺少同产品/地区/期间的全行业规模
- **THEN** 不生成真实市场份额或行业 CR3/CR5；同业样本内占比只能用其独立定义显示，行业问题保留缺口
