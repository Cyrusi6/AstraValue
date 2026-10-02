# 已退出活动队列的历史变更

2026-09-14按用户要求集中实施入口。这里保存历史合同和进度证据，不构成并行执行指令；移动不等于完成验收，也没有把未完成delta自动同步为主规格。当前唯一实施清单是[八步生产流水线](../changes/eight-step-production-pipeline-v1/tasks.md)。已归档主规格继续在 `openspec/specs/`。

| 变更 | 原任务状态 | 保留用途 |
|---|---|---|
| structured-data-first-v1 | 68/68 | 结构化采集历史实现与合同 |
| business-model-directory-recovery-v1 | 10/10 | 目录恢复历史证据 |
| business-model-acquisition-v1 | 111/113 | 采集底座及尚未完成的历史验收 |
| governance-management-data-foundation-v1 | 已删除 | 独立治理编排已删除；保留的取证、事件重建和快照契约已迁入当前生产变更与 `docs/project-cleanup-history.md` |

治理、资本及来源能力仍为当前研究的依赖；相关未完成能力被实际问题触发时，在当前任务中明确输入缺口及验收，不能因为旧目录退出就宣称已经具备。无需重新启动整份旧计划。历史阶段日志路径保留原记录，查询时将 `openspec/changes/<上述名称>` 映射至 `openspec/retired/<上述名称>`。
