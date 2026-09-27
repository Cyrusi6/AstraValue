# 核心知识服务 TDD 记录

2026-09-19 首次运行（实现前）：`C:\Python314\python.exe -m pytest tests/knowledge/regression/test_core_service.py --tb=short`。

结果：26 failed, 1 passed in 0.90s。26 个新增知识行为在测试主体中明确报告 `K01/K07 knowledge API is not implemented: No module named 'analysis.knowledge'`；不是 pytest、Python 或既有依赖缺失。既有 `MethodRegistry` 读取及计算方法包兼容探针通过。此时尚未创建 `src/analysis/knowledge/`。

测试覆盖：K01 读取/固定分母/分层长度/映射依赖；K02 指标定义与原文证据分离；K03 来源定位及同源归并；K04 缺上下文与银行不适用；K05 非发布内容、缺少/失败审阅和正文身份；K07 原文与指标历史复现、来源更正传播、覆盖下降及旧接口兼容。内容语义、真实来源核验和人工内容验收不由这些合成软件夹具证明。

实现后运行：`C:\Python314\python.exe -m pytest tests/knowledge/regression/test_core_service.py tests/knowledge/regression/test_release_gate.py tests/test_method_registry.py --tb=short`，44 passed in 1.89s。包含 27 个核心场景、14 个独立发布门场景及既有方法注册测试。实现期间先出现重复包 ID 报错优先级的 1 项失败，改为优先拒绝覆写后通过；未修改测试预期。

当前只有软件行为通过；真实目录内容、Agent 配对案例及人工抽查仍需主集成独立记录。快照发布默认入口调用单独的完整发布门，不存在跳过人工门的默认开关。

第二切片先运行 `C:\Python314\python.exe -m pytest tests/knowledge/regression/test_core_boundaries.py --tb=short`：4 failed, 4 passed in 1.66s。失败分别是有效替代路径未显示 available、依赖方法正文变化未纳入父方法审阅身份、仅首页定位未被拒绝、缺输入概念与所需口径未被拒绝。54 降为 53、同题骨架剩余路径、未解决分歧和 CLI 输出探针已通过。先保存失败证据，再修复实现。

接主集成审阅补充发布接口测试：`-k publish_gate` 首次 1 failed, 8 deselected in 0.22s，证实服务把外部 source_checks 直接传给发布门。预期改为从快照的实际内容审阅记录生成来源和案例检查，外部结果只能补 Agent 样例及人工记录。

修复以上五项后，`C:\Python314\python.exe -m pytest tests/knowledge/regression tests/test_method_registry.py --tb=short`：55 passed in 4.85s。未调整原失败断言；所有快照与审阅身份测试保留。无依赖的既有方法身份算法保持兼容，新增父方法身份包含必需依赖方法的内容身份。

主 Agent 的真实读取反馈触发第三切片：银行方法内层虽写明不适用，顶层 available 容易混淆内容可用与当前可用；CLI 也未提供内容需要的 business_type。先运行 `-k 'top_level or business_type'`：2 failed, 9 deselected in 0.65s，分别为缺少 context_status 和不识别 --business-type。新增顶层适用性及可使用标记，但保留通用内容覆盖和行业适用性两个独立维度。

修复后相同完整相关回归：57 passed in 3.77s。银行案例顶层 `context_status=not_applicable, executable=false`，缺上下文为 `needs_context`，已满足选择条件且题内通用路径完整才为 `executable=true`。CLI 新增 business_type/period_type/model_purpose 选择参数。输入数据就绪不由该标记保证。

最后针对两个具体疑点补测试：`-k 'frozen_body or dependency_applicability'`，2 failed, 11 deselected in 0.40s。证实初验后、冻结前正文变化可能创建无匹配审阅的包；父方法未继承必需依赖方法的行业不适用性。这两项均先复现，再修复。

两项修复后相关回归：59 passed in 4.36s。创建候选时复核实际冻结正文与指标所形成的身份；适用性沿必需依赖传递，并保留反例、限制和内容覆盖维度。核心代码至此完成本轮计划的回归范围。
