# 完整发布验收 TDD 记录

- 分支：`codex/knowledge-verification`；基线 `f69a4bf`。
- 对应规格：K08-S03/S04、K07-S05，验证完整发布不由日常回归或旧审阅记录代替。
- 首次运行：`python -X utf8 -m pytest tests/knowledge/regression/test_release_gate.py -q --tb=short`。
- 红灯：14 个测试失败，原因均为明确的 `release gate is not implemented` 断言；测试已成功收集，Python 与 pytest 可用，不是运行环境故障。
- 测试包含充分证据通过、部分覆盖、同数量错误问题、缺人工/Agent 冒充人工/人工失败/旧版本/未关闭问题、旧方法身份、缺理由、旧 Agent 样例等。
- 测试中的人工审阅全部为夹具，不是本知识库已通过人工验收的记录。
- 实现后首次运行：13 通过、1 失败；定位为测试夹具的可变列表同时被用作权威清单和待验收清单，复制夹具列表后保留原有断言，未放宽验收标准。
- 绿色验证：同一命令 14 个测试全部通过。验证对象是发布条件核算程序，不是实际知识内容或人工验收已通过。
- 第二个切片先增加冻结审阅适配测试：14 通过、2 失败，失败原因是 `frozen review adapter is not implemented`；测试已单独提交，之后才实现适配器，防止外部验收记录覆盖来源审阅或将旧包验收升级到新包。
## 2026-09-19 实际候选发布命令

- 先提交 `test_release_command.py` 和独立 `release/test_full_release.py`（`e4662a3`），实跑得到 4 failed，全部明确为缺少可执行发布命令。
- 实现 `scripts/validate_knowledge_release.py` 后，3 项命令回归 passed：读取真实冻结候选、报告未知版本、不替换旧验收身份；命令不会发布默认包。
- 单独运行完整发布门得到 1 failed：真实首批内容覆盖 1/54，来源和案例记录通过；完整题覆盖、八步 Agent 样例与人工抽查尚缺。没有 skip/xfail，保留该实际状态。
