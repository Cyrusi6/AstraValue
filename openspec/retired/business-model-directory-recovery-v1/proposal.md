> 历史参考，已于2026-09-14退出活动队列；原任务状态保留，不作当前执行指令。当前任务见 [eight-step-production-pipeline-v1](../../changes/eight-step-production-pipeline-v1/tasks.md)。

## Why

公告目录补缺会为目标额外生成14天回看；该附加窗口失败后，对它再次 reconcile 又生成更早的14天窗口，导致原始历史正文已经完整但恢复范围持续向前延伸。用户要求优先修复目录复核与恢复流程，复用已有正文，并仅在需要时用上交所目录交叉核对。

## What Changes

- 为新 reconcile target 冻结 `bounded-parent-range-v1` 范围策略。首次恢复仍应用来源定义的 overlap；父运行已经是 reconcile 时，新起点不得早于父运行已冻结的 effective range 起点。
- 在创建子运行前验证父恢复范围的时区、区间、来源和持久计划关系；缺失、矛盾或不支持的恢复策略失败关闭。
- 保留旧运行、失败、barrier、正文快照及已有计划；只有同一精确位置的真实成功后继证据可以解决屏障。
- 沿用已有条件请求、哈希和有效快照检查。304复用本地正文，新内容继续保留新版本。上交所目录只提供独立出处的交叉核对，不冒充巨潮成功证明。
- 对现存两处回看屏障进行只读规划复核及直连限速恢复，具备安全条件后完成两次增量和最终保持验证；外部失败如实保留。

## Capabilities

### New Capabilities

- `acquisition-recovery-boundaries`: 定义重复恢复的有界范围、范围策略冻结、历史兼容和目录证据边界。补充尚未归档的 [business-model-acquisition-v1](../business-model-acquisition-v1/specs/acquisition-checkpoints/spec.md) 合同。

### Modified Capabilities

无已发布主规格需要改名或替换。

## Impact

- 修改公共 reconcile selector 和相关回归测试；API、CLI、runtime继续共用同一选择逻辑。
- 使用现有 `AcquisitionRun.reconcile_target` JSON 保存新增策略元数据；无DDL迁移，不变更来源HTTP/schema、registry或checkpoint来源兼容合同。
- 不更改财务方法、前端、正文解析或MinerU；不下载已确认且返回304的正文，不以本地文件存在代替来源复核。
- 遵守 [计划.md](../../../计划.md) 的来源、修订及PIT边界，实际运行证据记入 [阶段日志.md](../../../阶段日志.md)。生成报告、数据库、原始公告与密钥不进入Git。

## Non-goals

不删除失败或手动清屏障，不将7天子窗口响应当成原14天证明，不迁移生产库，不实现跨来源屏障替换，不重做业务事实分析，不代填人工黄金签署。

## Acceptance boundaries

自动化门验证首次回看、重复恢复不扩窗、精确屏障解决、旧计划/数据不变及条件复用。真实联网门仅由本次正式运行结果确定；504或其他访问失败不等于无数据。两处回看未闭合时不得声明生产接续完成。用户已声明人工验收完成，原结构化清单仍独立保留其实际状态。
