## 1. Repair manifest 与目标冻结

- [x] 1.1 新增内容寻址 repair manifest 模型、规范 JSON/hash 校验和读写接口；用单元测试证明篡改、错误 schema、空 pin、重复 job 和超过两次追加预算均被拒绝。
- [x] 1.2 实现只读 repair plan，从指定 run 按 `failed`、dataset 和 reason 精确筛选，冻结 baseline attempt 数、上下文 hash、namespace 和代码 revision；测试成功/no_data/pending/retryable/partial 不入选、空计划零网络且相同输入幂等。

## 2. Snapshot 谱系与离线恢复

- [x] 2.1 将 structured page 的共享 snapshot 校验扩为“创建计划匹配或当前计划存在有效 discovery observation”，保持 namespace/attempt/run/lease 校验；测试跨公司相同日历可复用，跨 namespace、无 observation 和其他计划 observation 原子拒绝。
- [x] 2.2 扩展未投影 snapshot 查询以发现 observation 关联的共享 snapshot；用两家公司日历集成测试证明第二家公司 repair 可离线 replay、记录/字段/覆盖完整且网络调用为零。

## 3. 有界 repair runtime 与 BaoStock 会话

- [x] 3.1 实现 manifest 驱动的 repair candidates 和执行入口，使用现存最大 retry ordinal 追加 attempt，并按 manifest 的 baseline count + 两次预算恢复；测试一次成功、两次耗尽、崩溃重入、已成功不重抓及普通 `resume` 仍不领取终态失败。
- [x] 3.2 实现 BaoStock 登录探针先行和 repair round 内单会话复用，逐查询继续走来源门和租约；测试登录失败零 attempt、多 job 只有一次 login/logout、查询失败独立记录以及 finally 关闭会话。
- [x] 3.3 在 `StructuredDataService` 和 CLI 增加只读 `repair-plan`、显式 `repair-run`、`repair-status`，保持 API 不变；CLI 测试验证参数、退出码、敏感路径过滤和无隐式等待/监控。

## 4. 运维入口与文档

- [x] 4.1 新增定向补采监督脚本，执行前核对 revision、DB/data-root、namespace、manifest hash、原 supervisor 已退出和活动租约为零；以隔离库端到端测试证明不满足条件时零 I/O、满足时输出逐 job/数据集/原因状态且不安装任务计划。
- [x] 4.2 更新 structured runtime 运维文档，给出 baseline 结束后 plan/status/run 命令、原渠道探针与 fallback 等价门、回滚和独立人工验收边界；检查文档命令可解析且不把待执行生产补采写成已完成。

## 5. 验证与后续生产门

- [x] 5.1 运行 repair 聚焦测试、`tests/structured`、后端全量、`compileall`、注册表校验、strict OpenSpec 和 `git diff --check`；分别记录实际退出状态，不用局部测试代替全量。
- [x] 5.2 待当前生产 supervisor 结束且用户发出消息后，在隔离 store 执行一次 BaoStock 单登录/多 job 真实样本和共享日历离线 replay；如实记录来源可用性、login/logout 次数、网络 I/O 与失败，不在等待期间自动监控或执行。
- [x] 5.3 经用户消息确认后，对生产终态生成 manifest、备份并显式执行有界 repair；逐项核对 attempt/snapshot/page/row-key/pagination/coverage/safe-through，若原渠道不可用则先完成独立等价来源变更，不静默换源、不创建 incremental。
- [ ] 5.4 由独立执行者完成人工抽样与签署；自动测试、隔离真实样本和生产 repair 均不得代替人工验收。
