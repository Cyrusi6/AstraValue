## Why

`structured-data-first-v1` 的七家公司生产 baseline 已暴露两类可恢复缺口：BaoStock 每个 job 重复匿名登录造成成批 `login failed`，以及市场级日历响应跨公司内容相同时复用 snapshot 后被页面谱系校验拒绝。现有 `resume` 在 job 达到两次尝试上限后不再领取，无法在保留原失败证据的前提下定向补齐这些范围。

## What Changes

- 新增显式、有限且可恢复的 structured repair execution：只领取指定原 run 中已终态失败、原因和数据集均匹配的 job，为一次人工授权的 repair wave 追加至多两次尝试，不清除原 attempt、页面、记录或覆盖历史。
- 为 BaoStock repair wave 增加“登录探针先行、单次登录、批内复用会话”；登录失败时整轮零 job attempt，避免把同一会话故障扩散成大量终态失败。
- 修正 discovery snapshot 的跨物理计划复用谱系：同一 namespace、冻结来源、规范查询页和内容 hash 相同的 snapshot，必须通过对应 discovery observation 证明当前 job 的查询关系后才可投影；不放宽跨 namespace、跨查询或无 observation 的拒绝。
- 提供只读计划、显式执行、状态和证据输出的 CLI/监督脚本。脚本只准备在当前 baseline 之后人工启动，不安装定时任务、不自动监控、不自动执行生产补采。
- 渠道决策保持显式：默认先使用已经出现大量成功 job 的 BaoStock 原渠道；若结束后的独立登录探针仍失败，输出 `source_unavailable` 和待评估清单。只有备用来源的字段语义、期间、行键、可得性、许可与来源身份被真实样本验证后，才允许另建来源版本和 repair wave，绝不把不等价来源静默写成 BaoStock。
- 非目标：不删除或重置失败历史，不提高普通 baseline 的自动重试上限，不创建 incremental，不修改成功/no_data job，不在本 change 内直接执行生产补采或代替人工验收。

## Capabilities

### New Capabilities

- `structured-acquisition-repair`: 定义终态失败 job 的追加式定向补采、BaoStock 会话复用、snapshot observation 谱系、渠道门控和人工启动边界。

### Modified Capabilities


## Impact

- 影响 `analysis.structured` 的调度/运行时、页面提交谱系校验、服务与 CLI，以及生产监督脚本和对应测试/运维文档。
- 继续使用现有 SQLite append-only attempts、raw snapshots、structured pages/records/coverage 和 run lease；不修改全局 `PRAGMA user_version`，不倒改原 baseline 冻结上下文。
- repair 取得的数据沿用原 job 的公司、数据集、期间和 PIT 边界；成功仅关闭该 job 的来源缺口，不能把 `no_data` 解释成未披露，也不能把软件测试、隔离样本或自动补采当成人工验收。
- 备用渠道若实际需要，将通过独立注册表/来源版本变更交付；本 change 只提供拒绝不等价自动回退的门控，不预先宣称任何备用来源可用。
