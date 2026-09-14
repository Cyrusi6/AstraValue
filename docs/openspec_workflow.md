# OpenSpec 使用指南

本项目使用 OpenSpec 管理“单次软件变更”的需求、可观察行为、技术设计和实施任务。当前固定版本为 `1.12.0`，Codex 集成位于 `.agents/skills/`，项目配置位于 `openspec/config.yaml`。项目技能采用[按任务读取的上下文约定](openspec_context.md)，无需全量重读、例行文件哈希或固定交接模板。

OpenSpec 不替代以下权威内容：

- `计划.md`：长期产品目标和业务不变量；
- `docs/methodology/` 与 `config/methods/`：财务方法论及机器可执行规则；
- `阶段日志.md`：实际测试、真实同步、报告版本和可信边界；
- pytest、真实数据核验和人工黄金样本验收：实现正确性的外部门禁。

## 目录结构

```text
.agents/skills/openspec-*/        Codex 可调用的 OpenSpec skills
openspec/config.yaml              本项目上下文和规则
openspec/specs/                   已归档、代表当前行为的规格
openspec/changes/<change-id>/     正在进行的单次变更
  proposal.md                     为什么改、改什么、影响什么
  specs/<capability>/spec.md      可观察行为与 WHEN/THEN 场景
  design.md                       技术方案、权衡、迁移与回滚
  tasks.md                        可追踪的实施和验证清单
```

## 推荐工作流

在 Codex 中直接调用项目 skills。若当前旧任务没有显示新 skills，开启一个新的 Codex 任务后再调用。

### 1. 探索问题（可选）

```text
$openspec-explore 评估历史 point-in-time 估值需要锁定哪些来源、available_at 和快照不变量，先不要实现。
```

这一步只用于澄清问题，不应修改业务代码。

### 2. 创建变更提案

```text
$openspec-propose 为 pledge-event-chain-v1 创建变更：支持质押、部分解除、全部解除、更正和乱序公告；至少用两家公司真实样本核对。明确非目标、受影响层、失败降级、来源去重与验收门。
```

完成后先人工审阅 `openspec/changes/<change-id>/`，重点确认：

- 范围和 Non-goals 是否清楚；
- 每项 Requirement 是否使用 `SHALL/MUST`，并至少有一个四级标题的 WHEN/THEN Scenario；
- 是否覆盖缺失、冲突、更正、乱序、未来数据污染和历史快照不可变等适用边界；
- 是否只链接现有方法论和计划，没有复制出第二套权威文档。

### 3. 严格校验规格

```powershell
openspec status --change pledge-event-chain-v1
openspec validate pledge-event-chain-v1 --type change --strict --no-interactive
```

只有严格校验通过，且关键设计问题已经决策后，才进入实现。

### 4. 按任务实施

```text
$openspec-apply-change pledge-event-chain-v1
```

实现过程中按 `tasks.md` 逐项完成并保留验证证据。需求或设计发生实质变化时，先更新变更包：

```text
$openspec-update-change pledge-event-chain-v1
```

不要让代码先行、规格事后补写。

### 5. 验证

```text
$openspec-verify-change pledge-event-chain-v1
```

OpenSpec verify 是 AI 审查，不是强制质量门。仍需按影响面运行：

```powershell
python scripts/validate_method_library.py
python scripts/validate_golden_samples.py
python -m pytest
npm.cmd ci --prefix frontend
npm.cmd run build --prefix frontend
openspec validate pledge-event-chain-v1 --type change --strict --no-interactive
```

涉及数据和估值时，还要单独记录 SQLite/DuckDB/Parquet 一致性、真实在线样本和人工黄金样本状态。未执行、网络降级或仍待人工核验时必须明确写出，不能等同于通过。

### 6. 同步与归档

```text
$openspec-sync-specs pledge-event-chain-v1
$openspec-archive-change pledge-event-chain-v1
```

归档会把 delta specs 合并到 `openspec/specs/`。归档前必须确认任务完成、严格校验通过、自动化门禁通过，并在 `阶段日志.md` 中只记录真实观察到的结果。

## 常用 CLI

```powershell
openspec --version
openspec list
openspec list --specs
openspec show pledge-event-chain-v1
openspec status --change pledge-event-chain-v1
openspec validate --all --strict --no-interactive
openspec doctor
```

升级 OpenSpec 前先保存本地 Skill 定制；`openspec update` 可能覆盖入口文件，升级后合入上游必要契约并检查引用，不直接丢弃项目的精简规则。当前全局配置已关闭匿名遥测，并启用 `propose, explore, apply, update, sync, archive, verify` 七个工作流。

## 禁止项

- 不把令牌、`.env.*`、授权数据集、原始公告、数据库、浏览器资料或生成报告放入 OpenSpec 或 Git；
- 不用 OpenSpec 规格替代财务研究，也不把 `content_status: skeleton` 自动视为成熟方法；
- 不因 pytest 通过而声称真实在线样本或人工黄金样本通过；
- 不在任务未完成或关键证据缺失时仅凭 verify 结果归档。
