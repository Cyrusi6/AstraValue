# 八步轻量核心版人工验收清单

更新日期：2026-09-14。统一数据截止日：2026-09-13。profile：`eight-step-lite-v1.0.0`。pack 版本：`eight-step-lite-pack-v1.0.3`。

本清单用于人工确认七家公司轻量研究输入是否可读、口径是否清楚、引用是否足够支持后续判断。用户于 2026-09-14 在当前线程明确回复“全部通过”，据此勾选本清单全部项目。`source_text_available` 不等于问题已回答，目录闭合也不等于公告正文已读或不存在风险。

## 1. 验收信息与公共前置项

- 验收人：当前项目用户（未提供真实姓名）
- 验收日期：2026-09-14
- 确认原文：`全部通过`
- 验收基线 Git revision：`3ba4013`
- 使用的数据截止日：2026-09-13

- [x] 已确认工作目录为 `D:/估值模型-worktrees/fact-materialization-ultra`，分支为 `codex/fact-materialization-ultra`。
- [x] 已确认七个 `last-run-audit.json` 均指向下表 pack-id，且 `cache_reused=true`、`performed_network_io=false`。
- [x] 已运行 `python scripts/validate_eight_step_lite.py --output-root tmp/eight-step-lite-v1 --as-of 2026-09-13 --output tmp/eight-step-lite-v1/acceptance.json`，结果为 `status=passed`。
- [x] 已核对期后目录文件 SHA256 为 `34978C0715A2994265839B06DDBF047B7857796522BAB1EB0833B1FD0DE5A7DF`。
- [x] 已理解 54 题质量状态仍为 `pending`；本次只验收轻量输入，不验收评级、目标价、交易指令或完整研究结论。
- [x] 已理解未下载的期后公告需要按实际问题触发，不能从目录闭合推断“无重大变化”或“无风险”。

证据读取通用命令：

```powershell
python -m analysis.structured.research evidence `
  --pack '<下表对应包目录>' --evidence-id '<下表 evidence ID>' `
  --page 1 --max-tokens 2000
```

## 2. 公司验收索引

| 公司 | 当前 pack-id | 目录数量/页数 | 年度营业收入抽查 | 2026Q2 单季营业收入抽查 | A/C/D evidence ID |
|---|---|---:|---|---|---|
| 贵州茅台 600519 | `lite-pack-065abeb394705c9d8c2f83b8` | 62/3 | `168838102514.79 CNY`，`structured-fact-f2ea7cdee32c2257e45e3f80fd26f9fd` | `36794008743.97 CNY`，`structured-derived-3f1a9de43cfef3c16d7c204e64257467` | `document-evidence-ba53f03489a17690caf1de5f` / `document-evidence-6475101dcdafea077d1dc151` / `document-evidence-480b50fda9283fd632edbc42` |
| 五粮液 000858 | `lite-pack-593222037ebdcfe7330f0392` | 82/3 | `40528509770.23 CNY`，`structured-fact-c1b3e6979622108a6218442d9cfe2b88` | `5578650377.50 CNY`，`structured-derived-6b40d2859e3b4a818112fcbc34f85e8a` | `document-evidence-787fb2280229d849dfcd4e9a` / `document-evidence-75bc7794d339cb2348e3146f` / `document-evidence-86619607bf2d85612bdcb4d1` |
| 泸州老窖 000568 | `lite-pack-0e644a1e04dbbbb6b71697ea` | 65/3 | `25731010647.32 CNY`，`structured-fact-53dae38bc3a8c812dc4ddeb3bc798bc1` | `2447050461.57 CNY`，`structured-derived-c3394a59e12835beeec88d3a3a5f8ccb` | `document-evidence-441ce5900426df3fca2dca6d` / `document-evidence-ab16c52a5fd2999db1211ef7` / `document-evidence-8aec74da00964a266b6a2378` |
| 古井贡酒 000596 | `lite-pack-73fb6e5164e69813a81b8ec6` | 70/3 | `18831982591.24 CNY`，`structured-fact-6dbdeda8b42c98cc9b8d7e31d1a90eb2` | `2685621120.01 CNY`，`structured-derived-33f223f2e5d4a03fa88c6c5ee3438969` | `document-evidence-952f156aafe0bcc3c6c4d0cc` / `document-evidence-afa6a5d399cf16a7308b86af` / `document-evidence-64801bae45ba4737c7bcce1d` |
| 洋河股份 002304 | `lite-pack-b3ac9774d1b387c62da6a796` | 54/2 | `19211057613.05 CNY`，`structured-fact-57313f1992942795e4ed2c5d8d12238e` | `2354694663.88 CNY`，`structured-derived-af32734a233b8a261a4f7535ba4abed5` | `document-evidence-65b97c4916736286aa7d5bf9` / `document-evidence-5742b89dd8d66fc15cc9520b` / `document-evidence-d0481d07102e7b4e1c29f3ac` |
| 山西汾酒 600809 | `lite-pack-fdcae8c545ce08870e793c29` | 56/2 | `38718257657.74 CNY`，`structured-fact-4fdff0f5989df200b1ab55307e6a7981` | `6121159104.86 CNY`，`structured-derived-6ca67acd68c2adbd5c2cc04163e770fd` | `document-evidence-4bd2f40f6e30b0092389d4c2` / `document-evidence-99f40ec4c37eef275fca314e` / `document-evidence-6601bf6dc4bf5e2bba21303d` |
| 今世缘 603369 | `lite-pack-5dd0bbf34ba85e8b73c3aafd` | 65/3 | `10180698281.27 CNY`，`structured-fact-ae0f49c5e1b718e7367bc6979e2f6f5e` | `2112931554.80 CNY`，`structured-derived-3c7db993dd6a68e0fa7a468a7c210cfd` | `document-evidence-3ac0fcdfe137db3f0084e564` / `document-evidence-60e38022f2a6a2f0e678b38d` / `document-evidence-b5b3573e9d24f096a82d6318` |

表中 evidence ID 顺序均为 A 业务、C 质量风险、D 治理资本。对应定位依次为：茅台 page 11/16/17；五粮液 page 61/34/24；泸州老窖 page 13/73/30；古井贡酒 page 64/53/31；洋河 page 18/44/26；山西汾酒 page 12/9/16；今世缘 page 7/17/32。

## 3. 贵州茅台 600519

包目录：`tmp/eight-step-lite-v1/600519/2026-09-13/lite-pack-065abeb394705c9d8c2f83b8/`。

- [x] A 业务：主体、主营和产品/渠道表述符合原文；已读取 A evidence，并确认截取段落没有丢失否定、条件或表头上下文。
- [x] B 财务：2025 年营业收入与索引值、单位和事实 ID 一致。
- [x] B 财务：2026Q2 单季营业收入与索引值一致，已查看 `derived_from_fact_ids`，没有把累计值直接冒充单季值。
- [x] C 风险：已读取 C evidence；审计、现金债务和风险内容没有被自动概括为“无异常”。
- [x] D 治理资本：已读取 D evidence；计划、进展和实际执行没有混写。
- [x] E/F：同行可比限制、估值时点和未启用模型均表达清楚，没有合理价或安全边际结论。
- [x] 覆盖计数为 ready 305、source_text_available 11、pending 23；合同资产、商誉和客户供应商缺口未被隐藏。
- [x] 期后目录为 62 条/3 页且闭合；已记录需要进一步下载的公告 resource ID，或明确记录“本轮不触发正文但不据此判断无事项”。
- [x] 冲突数为 0；未发现引用路径、单位、主体或期间口径的新冲突。
- [x] 公司结论：通过（用户于 2026-09-14 在当前线程明确确认“全部通过”）；补充问题：无。

## 4. 五粮液 000858

包目录：`tmp/eight-step-lite-v1/000858/2026-09-13/lite-pack-593222037ebdcfe7330f0392/`。

- [x] A 业务：主体、主营和产品/渠道表述符合原文；已读取 A evidence 并核对上下文。
- [x] B 财务：2025 年营业收入与索引值、单位和事实 ID 一致。
- [x] B 财务：2026Q2 单季营业收入及其 `derived_from_fact_ids` 可复核。
- [x] C 风险：已读取 C evidence；风险筛查没有被写成事实定性。
- [x] D 治理资本：已读取 D evidence；计划和实际执行状态分开。
- [x] E/F：同行比较和估值约束清楚，没有自动评级。
- [x] 覆盖计数为 ready 316、source_text_available 12、pending 11；合同资产可选缺口未隐藏。
- [x] 已抽查 30 条 `reported_ratio_rounding_difference` 中至少 1 条，确认报告比例、独立复算比例和分母语义边界同时保留。
- [x] 期后目录为 82 条/3 页且闭合；已记录需追加正文的 resource ID 或不触发理由。
- [x] 公司结论：通过（用户于 2026-09-14 在当前线程明确确认“全部通过”）；补充问题：无。

## 5. 泸州老窖 000568

包目录：`tmp/eight-step-lite-v1/000568/2026-09-13/lite-pack-0e644a1e04dbbbb6b71697ea/`。

- [x] A 业务 evidence 已读取，主营、产品和渠道表述符合上下文。
- [x] 2025 年营业收入与索引值、单位和事实 ID 一致。
- [x] 2026Q2 单季营业收入及其输入事实可复核。
- [x] C 风险 evidence 已读取，未从结构化空值推断无事项。
- [x] D 治理资本 evidence 已读取，计划与实际分开。
- [x] E/F 同行可比限制和估值约束表达清楚。
- [x] 覆盖计数为 ready 305、source_text_available 12、pending 22；合同资产和商誉可选缺口未隐藏。
- [x] 已抽查 30 条金额/比例差异中至少 1 条，确认两种值并存且分母语义仍待确认。
- [x] 期后目录为 65 条/3 页且闭合；已记录需追加正文的 resource ID 或不触发理由。
- [x] 公司结论：通过（用户于 2026-09-14 在当前线程明确确认“全部通过”）；补充问题：无。

## 6. 古井贡酒 000596

包目录：`tmp/eight-step-lite-v1/000596/2026-09-13/lite-pack-73fb6e5164e69813a81b8ec6/`。

- [x] A 业务 evidence 已读取，主营、产品和渠道表述符合上下文。
- [x] 2025 年营业收入与索引值、单位和事实 ID 一致。
- [x] 2026Q2 单季营业收入及其输入事实可复核。
- [x] C 风险 evidence 已读取，未把筛查结果写成舞弊或无风险结论。
- [x] D 治理资本 evidence 已读取，计划与实际分开。
- [x] E/F 同行可比限制和估值约束表达清楚。
- [x] 覆盖计数为 ready 317、source_text_available 12、pending 10；合同资产可选缺口未隐藏。
- [x] 已抽查 30 条金额/比例差异中至少 1 条，确认两种值并存且分母语义仍待确认。
- [x] 期后目录为 70 条/3 页且闭合；已记录需追加正文的 resource ID 或不触发理由。
- [x] 公司结论：通过（用户于 2026-09-14 在当前线程明确确认“全部通过”）；补充问题：无。

## 7. 洋河股份 002304

包目录：`tmp/eight-step-lite-v1/002304/2026-09-13/lite-pack-b3ac9774d1b387c62da6a796/`。

- [x] A 业务 evidence 已读取，主营、产品和渠道表述符合上下文。
- [x] 2025 年营业收入与索引值、单位和事实 ID 一致。
- [x] 2026Q2 单季营业收入及其输入事实可复核。
- [x] C 风险 evidence 已读取，未从目录或空记录推断无事项。
- [x] D 治理资本 evidence 已读取，计划与实际分开。
- [x] E/F 同行可比限制和估值约束表达清楚。
- [x] 覆盖计数为 ready 316、source_text_available 12、pending 11；合同资产可选缺口未隐藏。
- [x] 已抽查 30 条金额/比例差异中至少 1 条，确认两种值并存且分母语义仍待确认。
- [x] 期后目录为 54 条/2 页且闭合；已记录需追加正文的 resource ID 或不触发理由。
- [x] 公司结论：通过（用户于 2026-09-14 在当前线程明确确认“全部通过”）；补充问题：无。

## 8. 山西汾酒 600809

包目录：`tmp/eight-step-lite-v1/600809/2026-09-13/lite-pack-fdcae8c545ce08870e793c29/`。

- [x] A 业务 evidence 已读取，主营、产品和渠道表述符合上下文。
- [x] 2025 年营业收入与索引值、单位和事实 ID 一致。
- [x] 2026Q2 单季营业收入及其输入事实可复核。
- [x] C 风险 evidence 已读取，未从结构化空值推断无事项。
- [x] D 治理资本 evidence 已读取，计划与实际分开。
- [x] E/F 同行可比限制和估值约束表达清楚。
- [x] 覆盖计数为 ready 305、source_text_available 11、pending 23；合同资产、商誉和客户供应商缺口未隐藏。
- [x] 期后目录为 56 条/2 页且闭合；已记录需追加正文的 resource ID 或不触发理由。
- [x] 冲突数为 0；未发现引用路径、单位、主体或期间口径的新冲突。
- [x] 公司结论：通过（用户于 2026-09-14 在当前线程明确确认“全部通过”）；补充问题：无。

## 9. 今世缘 603369

包目录：`tmp/eight-step-lite-v1/603369/2026-09-13/lite-pack-5dd0bbf34ba85e8b73c3aafd/`。

- [x] A 业务 evidence 已读取，主营、产品和渠道表述符合上下文。
- [x] 2025 年营业收入与索引值、单位和事实 ID 一致。
- [x] 2026Q2 单季营业收入及其输入事实可复核。
- [x] C 风险 evidence 已读取，未从目录或空记录推断无事项。
- [x] D 治理资本 evidence 已读取，计划与实际分开。
- [x] E/F 同行可比限制和估值约束表达清楚。
- [x] 覆盖计数为 ready 305、source_text_available 11、pending 23；合同资产、商誉和客户供应商缺口未隐藏。
- [x] 期后目录为 65 条/3 页且闭合；已记录需追加正文的 resource ID 或不触发理由。
- [x] 冲突数为 0；未发现引用路径、单位、主体或期间口径的新冲突。
- [x] 公司结论：通过（用户于 2026-09-14 在当前线程明确确认“全部通过”）；补充问题：无。

## 10. 总体签署

- [x] 七家公司均已完成上面的逐项核对，没有用自动测试代替人工阅读。
- [x] 所有“条件通过”或“不通过”事项均已记录具体公司、requirement/fact/evidence/resource ID 和所需动作。
- [x] 已确认本次签署只覆盖 `eight-step-lite-v1.0.0`、截止日 2026-09-13 的七家白酒轻量输入，不扩展到完整 54 题研究、其他行业、报告、前端或全市场批处理。
- [x] 总体结论：通过（用户于 2026-09-14 在当前线程明确确认“全部通过”）。

总体问题与补充动作：

______________________________________________________________________________

______________________________________________________________________________

验收人记录：当前项目用户（未提供真实姓名）　日期：2026-09-14
