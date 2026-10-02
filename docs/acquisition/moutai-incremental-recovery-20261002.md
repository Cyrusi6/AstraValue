# 贵州茅台增量、空查询与恢复验收（2026-10-02）

本轮 22 个数据集已经完成真实增量。最终运行共 28 个任务：18 个返回数据、10 个完整查询后为空、0 个失败、0 个待恢复、0 个因依赖暂缓。重复运行没有增加请求、快照、记录或字段。

实现提交：`652f07d`（`codex/fact-materialization-ultra`）。

完整空查询允许以后更新，包括首次查询就为空的情况。未完成项目保留原窗口，本轮独立补采；再次失败不影响其他数据集，只有依赖缺失输入的任务暂缓。恢复会核查已保存页，异常页重新请求，有效终页补写完成证明。

## 本轮范围与结果

| 结果 | 数据集 |
|---|---|
| 返回数据，12 个数据集 / 18 个任务 | `balance_fields`、`capital_projects`、`capital_raise`、`cashflow_fields`、`cashflow_quarter`、`company_basic`、`controller`、`income_fields`、`income_quarter`、`management_trades`、`repurchase`、`violation` |
| 本次完整查询后为空，10 个数据集 / 10 个任务 | `dividend`、`equity`、`float_holders_history`、`holders_history`、`management_roster`、`management_salary`、`rd`、`segments`、`staff_pay`、`subsidiaries` |
| 仍未完成或依赖暂缓 | 无 |
| 用户已决定延期，9 项 | `customers_peer`、`guarantee`、`litigation`、`seo`、`allotment`、`bond_issuance`、`goodwill`、`pledge`、`unlock_peer` |

延期只作用于贵州茅台默认选择；显式 `--dataset` 可补采，其他公司默认范围不变。9 项旧空响应、快照和报告证据保持原样，未标成成功或公司无事项。

本次保存 542 条观察记录，与历史业务主键和供应商内容逐项比较后，全部是已知内容：新增业务键 0、变化业务行 0。后续真正新增记录的入库由受控上游变化测试验证；这次真实接口没有出现新增披露。

## 真实运行

复用已验收的 `tmp/moutai-api-final-20261002/analysis.db` 和 `data`，没有重新进行全量 baseline，也没有手工修改水位线。

| 阶段 | 运行 | 结果 |
|---|---|---|
| 延期后首次增量 | `structured-run-39b089e305c5c99f472513c3` | 18 有数据 / 10 空；完成 3 个任务后退出进程，另一进程接续，已完成任务未重跑 |
| 旧空窗口恢复 | `structured-run-206f288a1ba06a8174e91a00` | 只复查上述 10 个增量空窗口，追加完整查询证据，全部明确为空 |
| 恢复后下一轮增量 | `structured-run-67d161b56ba15438df9f7020` | 18 有数据 / 10 完整为空 / 0 未完成，后续计划可创建 |
| 最终代码真实运行 | `structured-run-76bb54846a81e7b14b02980b` | 18 有数据 / 10 完整为空 / 0 未完成；再次执行同参数无网络请求 |

最终真实执行时间为 UTC 12:45:49–12:49:05，约 3 分 15 秒。运行前的 2,579 条记录内容不变。重复前后均为 165 attempts、88 snapshots、3,121 records、53,264 fields；这组计数属于整个验收数据库。新增观察的原始内容相同，因此复用已有内容快照。

旧空窗口恢复阶段还独立核验了 99 条旧 coverage、2,037 条旧记录、57 个旧原始文件及 9 项延期 coverage 均未改变。

## 恢复及失败隔离测试

自动测试覆盖以下行为：

- 首次与连续增量查询完整为空，后续仍能联网；已有数据保留，后来出现记录可正常入库。
- HTTP 超时、503、供应商错误及漏页保持未完成，原窗口留给恢复；其他数据集继续执行。
- 第 2 页已经保存但发现重复，恢复请求序列为 `1 → 2 → 2`，未跳到第 3 页；修复前的页继续保留。
- 新 incremental 的恢复任务复用已核验的第 1 页，只联网补取异常第 2 页。
- 有效终页保存后，在 coverage 或完成事件写入前分别模拟中断，恢复均不联网，补写完成证明后可被物化读取。
- 公司类型缺失时，只暂缓依赖它的报告查询，并列出缺失的前置 job。
- 研究工作区多数据集任务跑完独立可运行部分，保存可用候选快照及未完成清单，保持 `partial`；后续恢复成功才标记完成。

这些故障场景使用受控响应与中断注入。真实茅台运行本轮没有出现超时或失败，进程关闭后接续另有真实运行证据。

最终全量 Python 为 **1745 passed、14 skipped、1 failed**，耗时 401.142 秒；唯一失败是既有 `tests/knowledge/release/test_full_release.py::test_all_54_questions_and_recorded_acceptance_are_ready`，缺 Agent 样例和人工 review。本次采集与工作区相关测试全部通过。OpenSpec strict 为 2 passed / 0 failed，`git diff --check` 通过。

## 证据与复现

证据目录：`tmp/moutai-incremental-20261002/`，保持 Git 忽略，不提交数据库或原始供应商数据。

- `acceptance.json`、`verify.py`：首次增量、跨进程恢复、重复运行及业务内容比较。
- `completion-acceptance.json`、`verify_completion.py`：10 项旧窗口恢复、下一轮增量及旧证据保护。
- `final-live-acceptance.json`、`verify_final.py`：最终代码真实运行与汇总，SHA-256 `ff46da4cae121206ad955f03c877ed47e355da40b38b30fcc814aa24d0cc105a`。
- `final-regression.xml` / `.log`：最终全量 Python 回归；故障修复的定向过程另保存在 `recovery-targeted.log`。

下一轮直接创建新的 `structured plan --mode incremental`。本轮没有未完成窗口；若后续出现失败，`status.summary.unfinished` 会返回原窗口、`resume_page`、原因、缺少的前置任务及 `resume_action`。原运行尚有预算时用 `resume`，已耗尽时可通过新 incremental 或显式 reconcile 安排原窗口恢复。

本项完成后，分支合并仍需完成已列明的真实治理资料、知识发布和报告人工验收。9 项延期资料继续作为后补清单。
