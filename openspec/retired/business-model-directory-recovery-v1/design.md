## Context

首次 reconcile 将固定 overlap 拆成回看与精确目标两段。回看失败后再次减去 overlap 会持续扩大历史范围。公共 selector 位于 `src/analysis/acquisition/reconcile.py`，API、CLI 和 orchestrator 均使用它；planner 已保留精确目标边界。

## Goals / Non-Goals

本次修复重复恢复的范围边界与可审查性，保持首次回看、精确位置证明和已有正文复用。范围之外不变更查询接口、分类、解析、方法库或财务结论。遵守 [计划.md](../../../计划.md) 和原业务采集 checkpoint 合同。

## Decisions

1. 新 target 保存 `range_policy_version=bounded-parent-range-v1`、`range_floor=effective_range.time_start` 与 `range_floor_source`。该 floor 是供下一代使用的已冻结范围下界；`overlap_days` 仍为来源名义值。
2. 普通父运行保留原 overlap 算法。父运行为 reconcile 时，start 额外取父 effective range 下界的最大值。保留目标原时间片；当前 query 最早可得日若导致目标被截断则拒绝。没有证据支持7天拆分，不在此次修复中改变物理查询语义。
3. 由现有 runtime 冻结定义解析器提供父来源定义。校验 legacy/v1 target 的带时区范围、父身份和原计划引用，父实际 discovery 计划来源、query 依赖闭包、partition、pagination，以及每个查询的时间片并集。业务查询的 bootstrap 失败必须可选择，不要求其 query 与父 target query 相同。缺失、矛盾、未知版本在创建子 run/I/O 前拒绝；不递归重写或重验整个历史 checkpoint 链。
4. 存储沿用 reconcile_target JSON，无 DDL 和 registry 版本变化。新增元数据只影响新规划；旧计划执行继续读持久化字段。API、CLI 无新必填参数，报告/导出可直接展示 target 元数据。前端无需改动。
5. 正文沿用条件请求：304 只有完整有效快照才复用，200 按真实哈希处理。目录外部失败不是无数据，上交所目录证据保留独立来源，不能清除巨潮屏障。不得手动清屏障或反复下载本地已有正文来替代目录证明。

## Risks / Trade-offs

严格父范围验证可能暴露旧人工构造的不完整 target；对此拒绝新接续而保留历史，不回退无界算法。限定父范围意味着新增更早历史复核须从原普通父运行显式规划，避免把周期性历史扩展隐藏在失败恢复中。外部504仍可能阻断真实验收，软件修复不保证服务端响应。

PIT、来源独立性、许可、修订与缺失语义不变；不通过另一来源或本地缓存制造本次成功/历史可用时间。

## Migration Plan

独立 worktree 聚焦测试后跑项目门。生产库保持原位置和 namespace，先只读规划两处剩余父运行，再备份并冻结新代码 revision/hash 的执行上下文，直连、单并发、按来源限速执行。成功清零后才执行两次独立增量及目录集合、SQLite/导出一致性和旧证据保持验证。持续外部失败保留待完成项并报告。

回滚可切回原代码执行旧已冻结计划，但应停止创建可能扩窗的旧算法后继；不回滚或删除新追加证据。提交时不包含生产数据库、生成报告或密钥。

## Open Questions

无阻碍实施的需求待决。真实联网及原人工结构化签署按实际状态独立记录。
