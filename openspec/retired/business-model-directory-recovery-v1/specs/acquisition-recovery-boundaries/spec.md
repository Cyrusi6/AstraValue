## ADDED Requirements

### Requirement: 重复恢复的冻结范围下界
公共 reconcile selector SHALL 对普通父运行保留固定来源 overlap 与最早可得日约束；对 reconcile 父运行，子起点 MUST 不早于父 target 的 effective range 起点，且不得截掉所选精确目标。新 target SHALL 冻结 `bounded-parent-range-v1`、等于自身 effective start 的 `range_floor` 以及下界来源。名义 overlap 不变。API、CLI 和内部运行时 MUST 共用此计算，旧持久计划执行不得重算范围。

#### Scenario: 首次补缺
- **WHEN** baseline 或 incremental 的失败窗口首次恢复
- **THEN** 系统 SHALL 保留来源固定 overlap，并独立保留精确父时间片

#### Scenario: 回看连续失败
- **WHEN** reconcile 的最早回看窗口失败且继续创建后继
- **THEN** 子计划 MUST 覆盖原失败位置，但最早起点不得向父冻结范围之前扩展；后续多代也遵守此下界

#### Scenario: 旧计划执行
- **WHEN** 软件升级后执行已有持久化 reconcile 计划
- **THEN** 系统 SHALL 使用该旧计划冻结的范围和来源，不改写其 target、事件、attempt、coverage、snapshot 或 checkpoint

### Requirement: 父范围和依赖关系可验证
selector MUST 使用父运行冻结来源定义，核对 target 父身份、原计划引用、有效范围与已持久 discovery 计划。所选 query SHALL 属于 target query 及递归 prerequisite 闭包，且 source/version、partition 和分页语义一致。完整 legacy target 可接续；缺失/无时区/颠倒/越界范围、未知策略或身份矛盾 MUST 在创建 run、plan 或 I/O 前失败关闭。策略版本、floor 和 floor_source MUST 作为整体出现；legacy 必须三者均缺失，显式 v1 floor MUST 等于其 effective start，floor_source MUST 与上代运行模式及身份一致。

#### Scenario: 历史 target 无策略字段
- **WHEN** 旧 reconcile 没有新策略字段，但身份、带时区范围及持久计划相互一致
- **THEN** selector SHALL 使用其 effective start 限制新范围，并为新 target 冻结 v1 字段

#### Scenario: 前置目录查询失败
- **WHEN** 父 target 是公告查询而失败位置是该查询的 bootstrap 依赖
- **THEN** 系统 SHALL 允许选择该依赖的精确位置，保持相同有界恢复规则

#### Scenario: 父元数据与计划矛盾
- **WHEN** target 范围不能覆盖所选计划、原计划引用错误、依赖关系/分页错误或策略未知
- **THEN** 系统 MUST 拒绝新规划，旧数据及网络调用数量保持不变，不回退无界 overlap

### Requirement: 目录和正文证明保持独立
目录恢复 SHALL 沿用既有同位置 barrier resolution 和条件正文复用合同。上交所目录只作为独立交叉证据；504、失败或本地文件存在 MUST 不被解释为巨潮 no_data 或本次 unchanged。新正文版本 SHALL 保留真实来源、可用时间、snapshot 和血缘；旧版本及失败不可变。

#### Scenario: 已有正文返回304
- **WHEN** 合格条件请求获得304且锚点哈希完整、未隔离
- **THEN** 系统 SHALL 新增当前成功观测并复用旧正文，不重复下载或创建伪造版本

#### Scenario: 目录持续失败
- **WHEN** 巨潮目录仍超时但本地正文或上交所目录存在
- **THEN** 巨潮屏障 MUST 保留；真实恢复与两次增量验收不得宣称完成

#### Scenario: 精确恢复成功
- **WHEN** 同位置后继提交完整发现证明及所需正文观测
- **THEN** 系统 SHALL 只解决获得证明的屏障，保留其他缺口与所有旧失败记录，并通过一致性与目录集合复核确认接续状态
