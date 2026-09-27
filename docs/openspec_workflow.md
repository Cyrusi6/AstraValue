# OpenSpec 使用指南

本项目使用 OpenSpec 管理单次软件变更的需求、可观察行为、设计和实施任务。当前唯一生产实施入口是 [eight-step-production-pipeline-v1/tasks.md](../openspec/changes/eight-step-production-pipeline-v1/tasks.md)；项目产品职责见[计划](../计划.md)，任务上下文按[约定](openspec_context.md)读取。固定版本和本地技能以 `openspec --version` 与 `.agents/skills/` 的实际内容为准。

## 权威与历史

- `计划.md`：四层产品职责、模型自主研究及业务不变量。
- 当前变更的 proposal/specs/design/tasks：本次软件变更的需求、行为、设计和唯一任务清单。
- `docs/methodology/` 与 `config/methods/`：知识和公式；`content_status: skeleton` 不构成已核实理论。
- `阶段日志.md`：追加实际运行、在线数据和人工验收，旧记录不倒改。
- `openspec/specs/` 与 `openspec/changes/archive/`：主规格和已归档历史；退出活动队列的历史变更保留追溯，不是完成归档，也不再独立启动。

## 操作

在目标 worktree 运行 CLI，以实际返回的 root、产物路径和规则为准。按任务读取上下文，不要求例行全量文件哈希或固定交接模板。

```powershell
openspec status --change eight-step-production-pipeline-v1 --json
openspec instructions apply --change eight-step-production-pipeline-v1 --json
openspec validate --all --strict --no-interactive
```

探讨用 explore，修订已有计划用 update，已授权实施用 apply；范围变化先同步规格与设计。verify 只作审查，不替代验收；当前变更仍有未完成任务时不归档。

## 验证

文档调整运行 OpenSpec strict、引用检查和 `git diff --check`。行为变化按影响范围执行相称测试；涉及前端才构建前端，涉及存储投影才检查对应一致性。自动测试、真实联网、模型研究、成品渲染和用户人读分别记录，任何一项不能替代其他验收。

```powershell
python scripts/validate_method_library.py
python -m pytest
npm.cmd run build --prefix frontend
openspec validate --all --strict --no-interactive
```

## 清理与恢复

本轮项目精简见[清单](project-cleanup-inventory.md)、[决策](project-cleanup-decisions.md)和[历史](project-cleanup-history.md)。历史合同、快照和报告为可复核证据，不因删除旧运行入口而批量删除；在真实回采、自动测试和人工黄金样本验收完成前，不删除承载材料的 worktree、分支或远程引用。新研究流程不受旧“评级须用户确认”要求约束，旧报告和确认字段保留兼容读取。

## 禁止项

- 不把令牌、`.env.*`、授权数据集、原始公告、数据库、浏览器资料或生成报告放入 OpenSpec 或 Git；
- 不用 OpenSpec 规格替代财务研究，也不把 skeleton 自动视为成熟方法；
- 不因 pytest、verify 或自动导出通过而声称真实在线样本或人工黄金样本通过；
- 不在任务未完成或关键证据缺失时仅凭 verify 结果归档。
