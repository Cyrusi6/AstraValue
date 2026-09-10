## Context

参见 [proposal.md](proposal.md) 的动机。现有 structured runtime 把 `max_attempts=2` 解释为一个 job 的普通执行上限，`resume_candidates` 明确排除 `failed`；因此终态失败不能通过普通 `resume` 恢复。BaoStock 当前在每个 job 内独立 `login/logout`，一次登录故障会连续消耗许多 job 的尝试。另一方面，discovery snapshot 的内容身份不含物理计划 ID，完全相同的市场日历响应会正确复用同一 snapshot，但页面提交目前又要求 snapshot 的创建计划等于当前 job 计划，导致第二家公司以后必然冲突；实际 discovery observation 已经保存了当前 attempt、计划和共享 snapshot 的关系。

当前生产 supervisor 绑定 revision `d568aca` 且仍在运行。实现必须位于独立 worktree，不能改变其工作树、已加载代码、生产库或 data-root；本 change 交付后也只准备人工命令，不监控或自动启动。

## Goals / Non-Goals

**Goals:**

- 用可哈希的外部 manifest 冻结一次人工授权 repair wave 的精确 job 与追加尝试预算，并利用数据库事实实现崩溃恢复和幂等。
- 在不改变普通 `run/resume` 语义的情况下，为 manifest 中的终态失败 job 提供独立执行入口。
- BaoStock repair 在产生 job attempt 前探测登录，并在一轮内复用会话；每个查询仍通过原来源门与租约。
- 允许有 observation 证明的同内容 discovery snapshot 安全跨计划复用，优先重放已有日历响应而非重新联网。

**Non-Goals:**

- 不增加普通 baseline 的两次尝试上限，不让 `resume` 自动拾取终态失败，不删除或更新旧 attempt。
- 不在代码中预置未经真实样本验证的 BaoStock 替代源，不把东方财富相近字段冒充 BaoStock 原始数据。
- 不新增后台 worker、任务计划、轮询器或自动生产执行；不创建 incremental，不改变报告/导出/前端或人工验收状态。

## Decisions

### 1. 使用内容寻址 repair manifest，不新增数据库迁移

新增 `analysis.structured.repair` 定义 manifest schema、规范 JSON 和 SHA-256。计划阶段只读生产库，逐项保存 `run_id/job_id/company/ticker/dataset/scope/time`、原 attempt 数、失败原因、namespace、冻结上下文 hash、代码 revision 和 `max_additional_attempts=2`。manifest ID 由不含自身 hash 的规范内容计算；执行阶段必须重新计算并核对。

剩余预算定义为 `baseline_attempt_count + max_additional_attempts - current_attempt_count`。成功或 `no_data` 后该 item 永不再领取；进程崩溃后当前 attempt 数仍来自 append-only 数据库，因此重复执行同一 manifest 不会产生无限预算。attempt ID 继续使用 job、retry ordinal 和 page number 的稳定身份。

选择外部 manifest 而非新增 repair 表，是因为授权对象本身属于生产操作证据，现有 attempt/run/snapshot 表已经完整保存执行结果；增加模块迁移只会扩大当前生产恢复的部署风险。manifest 必须写入用户指定的 Git 忽略证据目录，不能提交数据库路径、原始响应或凭据到 Git。

替代方案是在 `structured_jobs` 上增加重试预算或重置 attempt；前者改变普通 resume 合同，后者破坏历史，均拒绝。另一个替代方案是创建重复 baseline run，但现有 dedupe key 会正确解析回原 run，且不能表达人工授权的新增尝试预算。

### 2. Repair 是 runtime/service 的独立入口，普通状态机保持不变

新增 repair candidate 构造器，只接收已验证 manifest item；它读取当前 job/attempt/events，拒绝非终态基线、过滤漂移、上下文不符或预算耗尽。candidate 的 retry ordinal 使用现存最大 ordinal 加一。repair 入口按原 run 领取同一 execution lease，逐项调用既有 `_execute_job`，使 snapshot、页面、记录、字段和 coverage 仍走原原子提交路径。

CLI/运维脚本分为 `plan`、`run`、`status`：`plan` 零网络生成 manifest，`run` 每次显式执行有限轮次，`status` 零网络对账。脚本不含 wait/PID polling；原 supervisor 是否结束由执行前 lease、显式进程参数和操作者检查共同保证。

不新增 API，是因为这是受控生产恢复入口，不应被常规同步或网页意外触发。service 方法便于测试和 CLI 复用；报告、导出与前端继续读取原 job 的最终记录和覆盖，不需要新格式。

### 3. BaoStock repair 先登录后建 attempt，并复用轮内会话

repair 入口先从冻结配置确认所有本轮目标 provider 为 `baostock`，取得 run lease 后调用一次匿名登录。若登录失败，释放 lease、返回 `source_unavailable`，不调用 `_execute_job`，因此 job attempt 数不变。登录成功时设置只在当前 runtime 实例和上下文管理器内有效的“已认证 SDK 会话”；`_execute_sdk` 在该上下文中跳过每 job 的 `login/logout`，但仍为每个 query 获取来源门、执行 lease guard，并由外层 finally 退出会话。

普通 `run/resume` 不进入该上下文，继续保持原单 job 会话行为，避免本 change 改变已冻结 baseline 的执行语义。修复完成后可另行评估是否把持久会话推广为普通路径。

### 4. 页面谱系以 snapshot 加 observation 关系验证

保留 raw snapshot 的内容寻址身份和创建计划，不复制相同字节。`structured_pages` 写入时仍要求 page attempt 属于当前 job/run/lease；snapshot 必须属于相同 namespace，并满足以下之一：

1. snapshot 的创建物理计划就是当前 job 计划；或
2. `discovery_observations` 中存在该 snapshot 与当前 job 计划的关系，且该 observation 的 attempt 也属于当前 run/计划。

`unprojected_snapshot_ids(job_id)` 同时查找创建计划直接匹配的 snapshot，以及由当前 job attempt 的 discovery observation 关联但尚未投影的 snapshot。这样已经取得字节、只在页面提交阶段失败的 `baostock_calendar` 可以离线 replay；跨 namespace、不同规范查询、来源版本不符仍由现有 snapshot/冻结查询校验拒绝。

替代方案是在 discovery snapshot 内容身份中加入 plan ID，会为相同市场数据重复保存 snapshot，并破坏现有内容寻址复用，故不采用。

### 5. 渠道决策由运行证据驱动，fallback 不自动发生

repair runner 的唯一内建执行渠道仍是原 job 冻结的 BaoStock。计划完成后的独立探针如果成功，直接补采；若失败，runner 输出规范错误、受影响 manifest items 和 `fallback_required=true`，不消费 job 尝试。

备用来源不能只靠“字段名相近”通过。后续若实际需要换源，必须先提供每个数据集的字段/单位/期间/主键/分页/PIT/许可真实样本矩阵，建立新的 source definition/version 和独立 repair manifest，并在记录中保留真实来源身份。`baostock_balance` 可与现有东方财富财务数据做需求覆盖对照，但不能据此关闭 BaoStock 数据集覆盖；`baostock_adjust` 与市场日历也分别评估，不设全局替代。

### 6. 部署、回滚与兼容

实现分支从生产 revision 创建。部署前停止所有绑定该 DB 的 worker，确认 clean revision、namespace、活动租约和三层备份；先运行 schema/manifest/status 只读检查和隔离库测试，再由操作者生成生产终态 manifest。因为没有数据库迁移，回滚只需停止 repair runner 并回到原代码；已经追加的 attempt/snapshot/page/record 是合法不可变历史，不删除。普通 CLI/API、两次重试规则、注册表 hash 和 `PRAGMA user_version` 保持兼容。

## Risks / Trade-offs

- [外部 manifest 被复制或篡改] → 内容 hash、revision/namespace/context pins 和逐 job 当前状态共同校验；相同 manifest 重放受固定 attempt 上限约束。
- [持久 SDK 会话中途断开] → 该 job 按原结果分类，剩余 item 不超过 manifest 预算；下一显式 round 重新登录，不无限自愈。
- [跨计划 snapshot 误关联] → 必须存在同 namespace、当前 run/plan 的 discovery observation，并继续核对冻结 query canonical 和 source version；负例测试覆盖跨 namespace/无 observation。
- [原 supervisor 尚未结束] → 同一 run lease 冲突在供应商 I/O 前失败；运维脚本另要求操作者提供已结束的 supervisor PID/最终 summary，不实现后台等待。
- [替代源能满足研究需求但不能复刻原 dataset] → 保持原缺口，单独登记备用来源事实与需求路由，不能修改原 dataset 的成功状态。
- [repair 成功掩盖曾经失败] → 状态输出同时报告 baseline attempts 与 repair attempts，原事件永远保留；人工验收继续独立。

## Migration Plan

1. 在独立 worktree 完成代码、聚焦/全量测试、strict OpenSpec 和离线 manifest 演练，不接触运行中的生产库。
2. 提交实现但不推入当前 frozen production workspace，不启动监控或自动任务。
3. 用户通知原 supervisor 结束后，核对最终 summary、活动租约、revision、DB/data-root/namespace 和备份，再运行 `plan` 生成终态 repair manifest。
4. 先执行只读 status 与 BaoStock 单登录探针；成功才显式运行 repair。日历 lineage repair 优先 replay 已有 snapshot，记录网络 I/O 是否为零。
5. 若 BaoStock 仍不可用，停止原渠道执行并生成 fallback 评估证据；完成独立来源版本变更前不写替代数据。
6. 完成后逐 job 核对 attempt、snapshot、分页/行键、records、coverage 和 safe-through；人工验收仍由独立执行者签署。
