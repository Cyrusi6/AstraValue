# Continuation context (derived; source wins)

- Change: eight-step-production-pipeline-v1 / spec-driven；用户名称末尾v解析为唯一v1。
- Root: `D:/估值模型-worktrees/fact-materialization-ultra`。
- Branch: `codex/fact-materialization-ultra`；实施起点 HEAD `ee7a5e315a3fd6487c219ecfe5d70fb075371bc3`。
- Recorded at: 2026-09-14。
- Authorized scope: 仅贵州茅台黄金报告；前端2.3/第3节暂停，不合并、不推送。
- Current gate: 6.5用户阅读签署；2.1、2.2保持未完成。6.6仅在签署后执行。

## Required reading

- 用户显式技能：`D:/估值模型/.agents/skills/openspec-apply-change/SKILL.md`；读取策略为同仓 `docs/openspec_context.md`。当前worktree内技能是较旧副本，不能覆盖用户显式路径。
- 当前任务：tasks.md第6节、proposal最新优先级及Non-goals、design Decisions 4/13–15及Migration Plan。
- 合同：specs/eight-step-analysis-pipeline/spec.md全部三个Requirements；structured-fact-materialization中的期间、血缘、不可变、逐题覆盖和轻量合同。
- 事实：阶段日志末尾2026-09-14黄金候选条目；验收文档的文件、数字和边界。
- 代码入口：reporting_bridge.build_report_request → report_semantics.semantic_inputs/full_coverage → ReportBuilder；exports读取同一ReportVersion。

## Verified state

- 6.1–6.4完成，30/38；本记录不代替实时CLI任务状态。
- 候选只有v3 `997818e8-3ffb-4ce5-baf8-03a78b60a418`，快照`ds-f4b21a712678ce7ec4fa`。
- 真实输出目录：`tmp/moutai-golden-v1/reports/600519/v3-997818e8`；不要为了阅读或复核再调用structured report追加版本。
- 303核心值、297显示单元格、三项annual/Q4关系、31源描述符、旧8份报告manifest核对通过。
- 32页PDF全页查看；Excel0错误单元格，四个改动视图用artifact-tool只读渲染检查。
- pytest 50 passed/1 skipped（原PDF可选测试未启用；实际PDF已另行生成并查看）；方法库/compileall/strict/diff-check通过。
- 从冻结请求/方法内存复算快照一致；首次保存被旧derived事实元数据不可变检查拒绝，已保持原v1投影元数据后生成候选，未覆盖旧事实。
- Python连接尝试0；原文哈希核对，未运行新联网来源验收。
- 全局黄金清单结构通过，但0/10人工样本，不与本轮门混淆。
- 未解决：54题pending、378审计缺口、行业数值输入/精确原文可得时间/确认预测仍缺；候选草稿暂不评级。
- 人工未签署；见docs/acquisition/moutai-golden-report-acceptance.md，用户签署不能由AI代填。

## Source identity

恢复时刷新CLI根、产物集合与以下哈希；正文丢失时重读当前依赖合同。

| Path | SHA-256 |
| --- | --- |
| `openspec/config.yaml` | `b8d8b3000ffbc57ac4ca1a508d321c2db225dc5edb6d8e10c05b73fa3d5bec55` |
| `openspec/changes/eight-step-production-pipeline-v1/.openspec.yaml` | `e6a45440b7b6b8096938fece82a34e09416bcecfda2ceef5ba18c854e4341abf` |
| `openspec/changes/eight-step-production-pipeline-v1/design.md` | `0904b1b4bddbb8e48d2b703fc4c57a1ecbfdfd1270b6ed2c1e600c2ba0198145` |
| `openspec/changes/eight-step-production-pipeline-v1/proposal.md` | `03eb0109ca68514f9000d036610a307bbf113eb6f1c72d70e293607fe24af5ca` |
| `openspec/changes/eight-step-production-pipeline-v1/specs/ashare-universe-batch-processing/spec.md` | `a1824f06b16bacfe36d78b21b7431ca39139f82e84ea7286d1061dcaa16ba2d2` |
| `openspec/changes/eight-step-production-pipeline-v1/specs/eight-step-analysis-pipeline/spec.md` | `2b43b4ab4d9ea4201cec7ff11ac503c43e2831b2cb72dc270e137f5b05610040` |
| `openspec/changes/eight-step-production-pipeline-v1/specs/structured-acquisition-repair/spec.md` | `e581d9f640485a509a2ae03039157c54238c8420314e852190de41f3423cc65b` |
| `openspec/changes/eight-step-production-pipeline-v1/specs/structured-fact-materialization/spec.md` | `71435a2f93daf5e3902c6c7ff991b52d9dfbec386e06ee80bdc69d25a5d848d4` |
| `openspec/changes/eight-step-production-pipeline-v1/tasks.md` | `f96c776c11416d0c049cf0de37d41035bf994ee85840dadde90bc525e0fc6868` |
| `docs/acquisition/moutai-golden-report-acceptance.md` | `6462f32785d4cce7f1fb4d45612c16c361a7e89226124b13926602d31de123db` |
| `src/analysis/structured/report_semantics.py` | `c685d39754098a13f70945f96140fdc6d05f685d816939c0bd3ecd266938c15c` |
| `src/analysis/structured/reporting_bridge.py` | `8867baa984375af59c2089a9aeaca62a0cca1cdc3c59f44db102f82866382b1f` |
| `src/analysis/structured/research_lite.py` | `648b8270e37c842e460000fa9cea4940c2c7fe311c7a6eabf26842afd68db24f` |
| `src/analysis/reporting.py` | `cc342fdfae7d498f0e48c001a236f3a3d27201751b1186622acfc65d98673576` |
| `src/analysis/exports.py` | `1b15084130d0360e3dd5711743b865a00408fbe50752eeaf0686b336c006e217` |
| `src/analysis/models.py` | `d276fd3ccd1c1cc086a8ea4ac1537ce9d0f54dba72b5b2e32307693d0646f362` |
| `tests/structured/test_report_golden_semantics.py` | `f85fbfef5e178f426cb25557c569426d0734d53e1e1960f66caf30172be9da31` |
