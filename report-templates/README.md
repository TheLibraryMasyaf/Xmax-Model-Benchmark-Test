# 报告模板目录

本目录只保存最终交付报告的版本化模板，不保存某次真实测试结果。

模型版本更新使用 `model-version-update-report.md`。生成后的报告写入：

```text
var/reports/model-version-updates/<comparison_id>.md
```

单模型版本或某次单批次测试使用 `single-version-evaluation-report.md`，建议输出到：

```text
var/reports/single-version/<report_id>.md
```

执行Agent不得修改模板章节含义或省略P0/P1/P2三级总结。正式自动化报告类型必须有对应生成器；当前单版本模板可由Agent从已有产物填写，但不得声称`report model-update`已能渲染它。不在一个模板中混入无关用途。
