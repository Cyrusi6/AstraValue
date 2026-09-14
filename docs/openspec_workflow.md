# OpenSpec 使用指南

当前唯一实施入口为 [eight-step-production-pipeline-v1/tasks.md](../openspec/changes/eight-step-production-pipeline-v1/tasks.md)。主产品职责见[计划](../计划.md)，按任务读取上下文见[约定](openspec_context.md)。

## 权威与历史

- `计划.md`：四层产品职责、模型自主研究及业务不变量。
- 当前变更的 proposal/specs/design/tasks：为什么改、可观察行为、设计和唯一任务清单。
- `docs/methodology/` 与 `config/methods/`：知识和公式；skeleton不构成已核实理论。
- `阶段日志.md`：追加实际运行、数据及人工验收记录，旧记录不倒改。
- `openspec/specs/` 与 `openspec/changes/archive/`：既有主规格和已归档历史。
- [openspec/retired](../openspec/retired/README.md)：退出活动队列的4份历史变更，保留未完成状态，不是完成归档，也不再独立启动。
- 轻量核心包文档保留技术合同，移除重复实施清单和“给Sol启动指令”；所有新实施以当前任务为准。

## 操作

在目标worktree运行CLI，以实际返回的root、具体产物路径和规则为准；版本用 `openspec --version` 检查，不假定全局安装版本。`.agents/skills/openspec-*`提供工作流入口，`CLAUDE.md`引用`AGENTS.md`，不复制两套规则。

```powershell
openspec status --change eight-step-production-pipeline-v1 --json
openspec instructions apply --change eight-step-production-pipeline-v1 --json
openspec validate eight-step-production-pipeline-v1 --type change --strict --no-interactive
```

探讨用explore，修订已有计划用update，已授权实施用apply；范围变化先协调相关规格与设计。verify只作审查，不替代验收；本变更仍有未完成任务，不归档。不要用 `openspec update` 在未审阅差异时覆盖项目技能。

## 验证

文档调整运行OpenSpec strict、引用检查和 `git diff --check`。行为变化按影响范围执行相称测试；只有涉及前端才构建前端，涉及存储投影才检查对应一致性。真实缓存、联网、模型研究、成品渲染和用户人读分别记录；任务勾选、自动导出、非空claims均不能代替后几项。

## 清理与恢复

本次清理见[记录](project-consolidation.md)。历史合同不是重复垃圾，保留到历史区；旧开发分支退出，本地数据不随分支删除。新研究流程不受旧“评级须用户确认”要求约束，旧报告和确认字段保留兼容读取。
