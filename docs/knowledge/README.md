# 八步研究知识库

知识库按现有 54 个研究问题给分析 Agent 返回操作步骤、必需证据、反例、适用边界和固定版本引用。公司事实由分析 Agent 在读取后使用；知识查询不计算公司的结论。

正文在 `docs/methodology/knowledge/`，版本化目录在 `config/methods/knowledge/catalog.v1.json`。原 `MethodRegistry`、财务公式及历史报告不改动。

## 本地使用

在当前工作树根目录使用 PowerShell：

```powershell
$env:PYTHONPATH = 'src'
python -X utf8 -m analysis.knowledge validate
python -X utf8 -m analysis.knowledge build --bundle-id my-candidate-v1
python -X utf8 -m analysis.knowledge read ES02.Q08 --bundle-id my-candidate-v1 --industry industrial --business-type manufacturing
python -X utf8 -m analysis.knowledge coverage --bundle-id my-candidate-v1
```

同一 `bundle-id` 不允许覆盖。修改正文、目录或指标定义后，审阅身份必须重新核查，并构建新版本。默认仓库为 `var/research/knowledge-store`，可用全局参数 `--store` 指定其他目录。

省略 `--bundle-id` 只读取已通过完整发布门的默认版本；尚未完成全量验收时返回 `no_default_release`。显式候选版本可试用。`--detail` 返回全文及案例；普通结果返回执行要点及 `detail_ref`，通过 `expand` 或 Python API 继续读取。限制输出长度时，必须检查 `complete`，再按 `continue_ref` 展开。

```python
from analysis.knowledge import KnowledgeService

kb = KnowledgeService.from_catalog(
    'config/methods/knowledge/catalog.v1.json',
    'var/research/knowledge-store',
)
guide = kb.read('ES02.Q08', bundle_id='my-candidate-v1',
                context={'industry': 'industrial', 'business_type': 'manufacturing'})
full = kb.expand(guide['methods'][0]['detail_ref'])
```

`available` 表示存在可读方法；`question_complete` 表示本题必需通用路径是否齐全。使用前还须查看 `context_status`、各方法的 `applicability` 和 `required_inputs`：未知行业需要补上下文，银行不能套普通营运资本方法。`pending` 指标表示尚未证明与实际字段口径相符；这不妨碍读取理论指导，但不能据此声称数据可直接取得。

## 回归与完整发布分别运行

```powershell
python -X utf8 -m pytest tests/knowledge/regression tests/test_method_registry.py
python -X utf8 scripts/validate_method_library.py
python -X utf8 scripts/validate_knowledge_release.py --bundle-id my-candidate-v1 --output var/research/knowledge-store/release-report.json
# 真实发布门单独运行；默认 pytest 会排除 knowledge_release 标记
python -X utf8 -m pytest tests/knowledge/release -o addopts= -m knowledge_release
```

发布命令返回码 0 表示所有门通过，1 表示验收未齐，2 表示输入或版本错误。它只检查，不修改默认包。完整测试默认从当前目录构建临时候选；检查既有冻结候选时设置 `KNOWLEDGE_STORE`、`KNOWLEDGE_BUNDLE`，实际 Agent 与人工记录文件通过 `KNOWLEDGE_ACCEPTANCE` 或命令的 `--acceptance` 指定。

当前知识候选的真实 Agent 样例和人工发布记录属于合并前遗留验收项，已按用户决定延期。默认回归门不运行 `tests/knowledge/release`，该目录仍可用上面的命令单独复核；在记录补齐前，候选保持未发布并返回 `no_default_release`。

来源与案例审阅直接取自被冻结的内容记录，外部文件不能替换它们。实际 Agent 样例和人工抽查分别记录。人工项尚缺时，完整发布门仍然失败；不能把模型审阅登记为人工通过。通过后可用 Python 的 `publish_default(bundle_id, acceptance)` 发布，发布会再次校验。

## 维护

来源、规则、指标定义或方法需要更正时，调用 `record_change(kind, object_id, reason)`。它列出受影响方法、问题和案例，暂停相应当前推荐；旧版本保留原文并提示更正。无关路径保持可用，有有效替代方法时保留替代路径。源文件修改不会自动覆盖历史快照。

原文缓存不纳入 Git；引用位置、真实访问记录和项目整理方法纳入版本控制。公开访问与整份材料再分发权分别记录。来源核查见 [source-probes](source-probes/)，测试顺序见 [core-tdd-evidence.md](core-tdd-evidence.md)，需求验证对应见 [traceability.md](traceability.md)。
