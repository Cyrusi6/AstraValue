# 项目整理记录（2026-09-14）

当前工作目录 `D:/估值模型-worktrees/fact-materialization-ultra`，分支 `codex/fact-materialization-ultra`。用户明确要求移除其他开发分支，不再保留并行任务。

## 实际清理

- 本地分支24→2：仅 `main` 与 `codex/fact-materialization-ultra`。
- 注册worktree29→2：主仓库 `D:/估值模型`（main）与当前目录。
- 10个无改动、无忽略数据且无独有提交的worktree直接删除；其余17个退出注册，工作文件保存到备份，忽略数据保持原路径。
- 4份旧活动变更在当时移入历史目录，原任务状态不变；这段记录描述历史，不构成当前执行入口。
- 移除轻量文档中的重复实施步骤和Sol启动指令，统一产品规则、当前任务与工作流。历史 skill 和依赖读取文档已在 2026-10-03 一并移至仓外归档。

## 恢复依据

本地备份目录：`D:/估值模型-backups/consolidation-20260914`。

- `all-refs.bundle`：删除其余非空分支之前的完整Git引用与历史，已用git bundle verify验证；包含主仓库未提交修改的stash（dd9b37efd74efbfd98794aec1681599d82b80aaf）。
- `inventory-before.json`、`empty-worktrees-removed.json`、`retired-worktrees.json`：原路径、分支、HEAD及备份路径对应关系。
- `worktree-*`：其余17个旧worktree的工作文件，包含未提交和未跟踪代码；忽略数据已移回原路径，不在这些备份内。

可先用 `git bundle list-heads <bundle路径>` 查旧引用，再显式fetch所需引用到新的恢复分支；主仓库改动可从上述stash提交恢复。备份worktree的旧`.git`指针不再有效，恢复代码时另建明确checkout后复制所需文件，不直接在备份运行Git。

## 数据保留

贵州茅台冻结输入仍引用 `D:/估值模型-worktrees/business-model-cninfo-direct-empty/var/pilots/` 等旧路径。原件、数据库及可复核输出保留，不重绑namespace。这些目录现在是历史数据位置，不再是注册worktree或活动开发分支。306个Python/pytest/egg-info缓存目录已移入备份，不能因看到旧路径就删除证据。

本次未推送、合并或删除远程分支。main保留原提交，主仓库未提交工作保存后切到main；当前分支承载本次规划纠偏。

## 清理后核验

历史规划严格验证通过。清理后重新验证旧茅台包/报告：31个来源描述符、303条核心数值、297个显示单元格通过，Excel错误单元格为0；这只说明清理未破坏这些已验证证据，不表示旧报告人读通过。旧验证器仍输出manual_acceptance=pending，当前用户反馈以黄金验收文档的人读未通过为准。

改动文档本地链接检查发现退休历史design有3个原已缺失的AKShare参考镜像链接；移入历史前这些相对路径在当前worktree也不存在，未作为现行执行依赖。当前活动规划和新指导文件链接无缺失。删除了已失效的当前变更context.md，后续实施可按实际状态重建派生导航。
