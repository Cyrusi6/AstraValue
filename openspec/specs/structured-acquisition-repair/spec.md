# structured-acquisition-repair Specification

## Purpose

为结构化生产采集提供显式、有限、可审计的终态失败补采能力，在保留原始失败证据与冻结查询语义的前提下补齐可恢复数据，并阻止并发执行、无限重试或不等价来源替换污染覆盖结论。

## Requirements

### Requirement: Repair wave 精确冻结目标
系统 MUST 由操作者显式给出的原 structured run、失败原因和数据集过滤条件生成不可变 repair manifest，只纳入当前状态为 `failed` 且符合过滤条件的 job。`succeeded`、`no_data`、`pending`、`retryable` 和 `partial` job MUST 不进入终态失败补采；未完成 job 继续走原 `resume` 路径。

#### Scenario: 仅选择登录失败的终态 job
- **WHEN** 原 run 同时包含成功、空结果、待执行、可重试以及两次 BaoStock 登录失败的 job
- **THEN** repair manifest 只列出两次登录失败且已终态的 job，并冻结原 run/job、公司、数据集、期间、原尝试数、原因、namespace 和配置 hash

#### Scenario: 过滤后没有目标
- **WHEN** 操作者的 run、数据集和原因过滤条件没有匹配任何终态失败 job
- **THEN** 系统返回可审计的空计划且网络 I/O 和新 attempt 均为零

### Requirement: Repair 尝试追加且有界
系统 SHALL 为每个 manifest job 在原失败 attempt 之后追加 repair attempt，每次人工授权的 repair wave 对同一 job 至多增加两次尝试。系统 MUST 不删除、改写或伪装原 attempt、snapshot、page、record、coverage 和 run 事件；重复执行同一 manifest MUST 复用已完成状态且不得突破冻结的追加上限。

#### Scenario: 一次补采成功
- **WHEN** 已有两个失败 attempt 的 job 在 repair attempt 中取得并完整提交数据
- **THEN** 原两个失败 attempt 保持可查，新成功 attempt 追加保存，该 job 转为成功且只有对应覆盖可以推进

#### Scenario: 补采再次耗尽
- **WHEN** 同一 manifest job 的两次 repair attempt 都失败
- **THEN** 系统保留全部四次 attempt，停止继续领取该 job，并把该 repair item 明确报告为 `exhausted`

#### Scenario: 崩溃后重复执行
- **WHEN** repair wave 在部分 job 提交后中断并以同一 manifest 恢复
- **THEN** 已成功 job 不重抓，未完成 item 从数据库事实恢复，任何 job 的追加 attempt 数不超过 manifest 上限

### Requirement: BaoStock 会话探针和批内复用
针对 BaoStock repair wave，系统 MUST 在创建任何 job attempt 前完成一次匿名登录探针；登录失败时整轮 MUST 为零 job attempt 并返回 `source_unavailable`。登录成功后，系统 SHALL 在该有界 repair round 内复用同一会话执行查询，同时继续遵守原来源单并发、最小间隔、租约续期和结果集耗尽合同，并在退出时关闭会话。

#### Scenario: 登录探针失败
- **WHEN** BaoStock 匿名登录返回非零错误码或连接异常
- **THEN** repair round 返回原始规范化诊断，目标 job 的 attempt 数不变，系统不循环登录每个 job

#### Scenario: 单次登录补采多个 job
- **WHEN** 登录成功且一轮选择多个 BaoStock 失败 job
- **THEN** 系统只建立一次匿名会话，逐 job 保存独立 attempt/snapshot/page/record，并对每次查询继续应用来源门和租约保护

### Requirement: 共享 discovery snapshot 保持可证明谱系
系统 MUST 允许同一 storage namespace、冻结来源版本、规范查询页和内容 hash 的 discovery snapshot 跨物理计划复用，但页面提交前 MUST 存在 discovery observation 证明该 snapshot 与当前 job 的物理查询关系。跨 namespace、查询不一致、来源不一致或缺 observation 的 snapshot MUST 在写入前被拒绝。

#### Scenario: 市场级日历跨公司复用
- **WHEN** 多家公司执行相同 BaoStock 市场日历查询并取得完全相同响应字节
- **THEN** 后续公司可通过各自 discovery observation 引用同一不可变 snapshot，页面和记录保留各自 job/attempt 谱系且不会触发 snapshot lineage mismatch

#### Scenario: 无有效 observation 的复用被拒绝
- **WHEN** snapshot 属于相同 namespace 但没有与当前物理查询关联的 discovery observation
- **THEN** 页面 bundle 原子回滚并报告明确的 lineage mismatch，不能仅凭相同内容 hash 放行

### Requirement: 渠道切换必须显式且等价
系统 MUST 默认使用原 job 冻结的 BaoStock 来源。原渠道不可用时，系统只能输出来源不可用证据和待评估范围；不得自动把其他供应商结果写成 BaoStock。只有备用来源已建立独立版本身份，并以真实样本验证字段语义、单位、期间、主键、排序/分页、PIT 可用时间、许可和缺失语义等价后，系统才可通过单独 repair wave 使用该来源，且事实 MUST 保留真实来源身份。

#### Scenario: 原渠道仍可用
- **WHEN** baseline 结束后的独立 BaoStock 登录与代表性查询探针成功
- **THEN** repair wave 使用原渠道，不触发备用来源搜索或重复抓取成功范围

#### Scenario: 没有合格备用来源
- **WHEN** BaoStock 持续不可用且候选免费渠道缺字段、历史范围、行键或许可等价证据
- **THEN** 系统保持缺口并输出 `fallback_unqualified`，不得用近似数据关闭原 dataset 覆盖

### Requirement: Repair 只能由操作者在主执行之后启动
系统 SHALL 提供只读计划、显式执行和状态输出，但 MUST 不安装计划任务、不自动监控生产 supervisor、不自行等待后触发。执行前 MUST 核对原 supervisor 已退出、活动租约为零、数据库/data-root/namespace/冻结 revision 与 manifest 一致，并在不一致时零 I/O 失败。

#### Scenario: 原 baseline 仍在运行
- **WHEN** 操作者尝试执行 repair 而原 supervisor 进程或目标 run 活动租约仍存在
- **THEN** repair 在登录或供应商查询前停止，不创建 attempt，不修改生产事实

#### Scenario: 人工消息后启动
- **WHEN** 原 supervisor 已结束且操作者明确调用冻结 manifest
- **THEN** 系统才执行有界补采，并把自动测试、真实补采结果和人工验收状态分别报告

### Requirement: Repair 状态和证据完整可核对
系统 MUST 输出 manifest hash、冻结 revision/namespace、目标和实际 attempt 数、按公司/数据集/原因的成功、空结果、失败、耗尽、未执行及来源状态。输出不得包含凭据或原始敏感正文；生产补采完成也不得自动标记独立人工验收通过。

#### Scenario: 部分补采完成
- **WHEN** 一批 repair item 中同时出现成功、空结果、来源不可用和耗尽
- **THEN** 状态输出逐类列出数量和 job 标识，保留 pagination/row-key/safe-through 结果，不以部分成功报告整批完成
