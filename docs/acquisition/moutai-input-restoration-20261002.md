# 贵州茅台原文、同行和行情输入恢复

2026-10-02，按用户已核实的十二项缺口直接恢复资料连接，补齐正式获取入口，并重建本轮研究包。

| 必需输入状态 | 修复前 | 本轮恢复后 |
|---|---:|---:|
| 就绪 | 299 | 302 |
| 原文可读、待模型阅读 | 2 | 11 |
| 待补充或处理 | 12 | 0 |
| 合计 | 313 | 313 |

恢复后的分布与 `25b7a1f` 引用的完整轻量样本一致。14 条定位证据、2025 年报、2026 中报及五粮液、泸州老窖、山西汾酒三家同行均接入。既有 285 个财务事实的 ID、值、单位、期间完全一致；总股本仍为 1,250,081,601 股，引用日期从 2026-05-28 更新到本轮行情的 2026-09-30。54 道研究题、401 项要求继续保留。

## 本轮真实行情

通过公开 `request_materials(material_types=["market_quote"])` 进入结构化 runtime，查询窗口为 2026-09-18 至 2026-10-02，只选择估值所需字段。真实取得 1 页、8 行，最新返回日为 **2026-09-30**。

| 字段 | 原始精度值 |
|---|---:|
| 价格 | 1258.62 元/股 |
| 总市值 | 1573377704650.62 元 |
| PE TTM | 19.32089778 倍 |
| PB MRQ | 6.26211023 倍 |
| PS TTM | 9.08214902 倍 |

运行 `structured-run-24aead248279479cc306587b`，工作区任务 `j_0a47ac3c7dad18f8c09ebb90`。重复同一公开请求时 acquisition attempt、原始 snapshot 和结构化 record 计数均未增加。

## 正式连接和获取链

- `projection_roots` 读取主体当前事实，`peer_projection_roots` 只读取同行，`evidence_roots` 只读取原文和辅助材料。较早日期的有效原文索引可以复用；源哈希、公司、期间与已知披露截止日均校验，旧目录中的主体行情不会因连接同行或原文而混入。
- `report_documents` 复用有效原件；缺失时由 CNINFO 目录选最新完整中文年报和中报，再调用 acquisition 按需正文 snapshot 与解析。目录中的其他正文不下载。章节只标记原文可读，模型仍需阅读分析。
- `peer_facts` 只补画像登记同行的缺失比较指标，年度财务限定最新完整年度，行情限定截止日前十四天。同行原有版本可复用，具体日期在读取结果中保留。
- `market_quote` 提供价格、市值、股本和 PE/PB/PS；实际行情日独立于采集时间及研究截止日。
- `requirement_ids` 中的原文、同行和行情缺口也能路由到上述任务。已有原文的阅读缺口不会被误当成重新下载需求。
- 单项失败不跳过其他公司或数据集。已完成运行可先物化，未完成运行仍保留恢复位置；完整空查询结束本次查询，缺数据的输入继续显示缺口。
- 目录普通超时后通过正式恢复运行复用已核验页面及空窗口，原查询时间范围不变；连续失败也只请求未完成页，原失败记录和 proof/snapshot 保留，哈希损坏明确报错。
- 新材料形成候选，由 `adopt_snapshot` 采用。补采继续绑定已采用的输入，独立冻结每轮导出，并保留已采用的原文、报表整理和附加产物；采集期间如活动快照变化，旧任务不能把候选挂到新快照。
- 组合补采调用子任务的正式恢复入口，遵守运行锁和重试次数，未完成项不标完成。采用候选时，已登记的 API 补充资料和行业分类经哈希复核后，与快照切换在同一事务中追加绑定；失败整体回滚。旧正文、判断、计算和审阅仍需结合新输入重新检查。
- reporting bridge 补入逐题覆盖与同行指标所引用的真实来源。当前桥接有 490 facts、12 dimensional facts、8 events；45 个来源引用全部闭合。

## 从缺失状态获取的真实验证

| 路径 | 实际结果 |
|---|---|
| 已有年中报复用 | 复用 2 份原件和 2 份解析索引，0 网络请求，32 条可定位原文记录 |
| 空原文输入根 | 5 个目录/引导响应、2 个正文 snapshot；只下载年报和中报，解析取得 51 条定位原文，核心包选入 14 条 |
| 空同行输入根 | 三家同行经过 3 轮完成，后两轮由公开 `resume_task` 在新进程继续；12 个结构化请求/响应，各家年度收入、毛利率及 PE/PB 均可读取 |
| 重复公开请求 | 上述三条路径均复用同一任务，attempt、snapshot 和 record 计数不增加 |

三条路径产生的候选均为 302 项就绪、11 项原文可读；独立测试目录与主交付研究状态分开。主交付沿用已有三同行版本；缺失同行获取链另以真实联网候选验证。

自动回归覆盖原件损坏、未来披露、摘要/英文排除、空响应、分页中断、正文失败隔离、已保存内容续跑、默认报告缺口路由、目录用途隔离及候选材料继承。恢复检查还覆盖连续超时、原窗口冻结、有效前缀复用、子任务运行锁和重试上限，以及采用候选时的事务回滚与并发冲突。

2026-10-03 收尾验证：全量 Python **1909 passed、14 skipped、1 failed**，400.96 秒。唯一失败为既有知识发布验收 `test_all_54_questions_and_recorded_acceptance_are_ready`，缺 `agent_samples` 和 `human_review` 记录；未放宽检查。跳过项为 11 项需显式开启的 Docker 沙箱、2 项本地缓存及 1 项 PDF 视觉检查。完整记录为 `full-final-regression.xml` / `full-final-regression.log`；较早的 `full-regression.*` 保留为修复过程记录。

最终真实输入只读复验 **11 项通过**，未发起网络请求、旧文件哈希不变；结果为 `acceptance-final.json`。前端 `npm run build`、OpenSpec strict（2 passed / 0 failed）及 `git diff --check` 通过。上述结果验证输入和获取链，报告成品及人工阅读仍待后续执行。

## 交付入口和证据

- 研究配置：`tmp/moutai-input-restoration-20261002/workspace-config.json`；默认 `config/research_workspace.json` 同步登记有效输入，原工作区状态保留。
- 研究任务：`r_e5e30487e019b5d45967ff1f`，新状态根为上述目录的 `state`。
- 已采用快照：`lite-pack-1a6c2dc3d94fab9839c3f58d`。
- `inputs.json`、`acceptance.py`、`acceptance-final.json`：旧样本描述符、逐项核验脚本及最终 11 项通过结果；177 个旧文件和 9 个当前绑定文件哈希不变。
- `restore.py`、`market-job.json`、`market-repeat.json`、`market-inputs.json`：公开行情任务、重复请求和实际字段。
- `probe_public_materials.py`、`verify_public_probes.py`、`public-probes.json`：缺失资料补采、跨进程恢复和重复请求验证。

同一 `research_id` 在旧修复目录中也存在，继续生成本轮报告必须使用这里的新配置；旧 `workspace-config-repaired-v3.json` 仍绑定修复前的资料范围。公开读取命令为：

```powershell
Set-Location 'D:\估值模型-worktrees\fact-materialization-ultra'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH=(Resolve-Path src).Path
python -m analysis.research.cli get_research_brief `
  --config tmp/moutai-input-restoration-20261002/workspace-config.json `
  --arguments '{"research_id":"r_e5e30487e019b5d45967ff1f"}'
```

输入恢复和获取链已验证。新报告生成、用户阅读验收及分支合并继续按原任务顺序进行；保留旧数据、旧包和现有 worktree。

最终清理前仍需保全 Git 忽略的证据文件：两份复用原件目前位于 `business-model-cninfo-direct-empty` worktree 的数据目录，冻结包继续引用这些原路径。清理该 worktree 前必须保证原件及历史引用仍可读取。
