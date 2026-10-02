# 治理资料与研究工作区绑定

治理资料没有独立的下载器、运行器或报告生成器。研究工作区只接受已经由共享 acquisition 链冻结的 `manifest_id`，再按研究问题选择 manifest member，经过取证、事件重建和治理快照后交给 reporting bridge。

`config/research_workspace.json` 当前将 `governance_sources` 保持为空。这是有意的能力缺口：仓库中没有可公开提交的真实治理 manifest、原始文件和来源角色映射，不能用 fixture、缓存或虚构 ID 冒充生产绑定。没有绑定时，`list_governance_materials` 和 `build_governance_snapshot` 返回 `registered_governance_manifest_required`，不会下载资料，也不会生成治理结论。

登记真实来源时，每项至少需要以下字段，并且必须与研究公司的 ticker 一致：

```json
{
  "company_id": "600519",
  "acquisition_db": "var/pilots/.../analysis.db",
  "data_root": "var/pilots/.../data",
  "manifest_id": "evidence-manifest:...",
  "source_roles": {
    "cninfo.announcements@1.0.0": "official_filing"
  }
}
```

这些路径和 ID 只能来自一次真实、已终结并通过完整性检查的 acquisition run。治理采集的补缺必须通过研究工作区 `request_materials`，不会在治理模块内启动第二条下载链。

当前自动回归覆盖空配置的明确 capability gap；真实治理 manifest、八步报告中的治理引用和人工阅读仍需单独验收。
