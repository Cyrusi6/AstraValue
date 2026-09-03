# 黄金样本人工验收

`manifest.json` 只定义覆盖范围和验收门槛。当前十家公司均为待人工核验，不能据此声称真实数据已经通过验证。

当一家公司完成核验后，在对应清单项中将 `status` 改为 `validated`，并设置位于本目录内的 `validation_file`。核验文件至少包含：

- `ticker`、`company`、`category`、`reviewer`、`reviewed_at`；
- 不少于三期的 `annual_periods`；
- 不少于十二期的 `quarter_periods`；
- 不少于 50 条 `fact_checks`。

每条 `fact_checks` 必须记录 `fact_id`、`metric_id`、`period_end`、`status`、`primary_source` 与 `crosscheck_source`。两个来源都要包含真实的 `upstream_source_id`，且二者不得相同。允许的核对状态为 `matched` 或 `explained_difference`。

日常结构检查：

```powershell
python scripts/validate_golden_samples.py
```

只有准备正式验收时才运行严格模式；仍有待核验样本时它会返回失败：

```powershell
python scripts/validate_golden_samples.py --strict
```
