# 模型版本更新报告

本文只定义基线模型与新模型的对比、三级归类和报告生成规则；报告版式见`report-templates/model-version-update-report.md`。

## 1. 输入与输出

输入为同一Run Request中的`comparison`、两个模型版本的TestPlan/GenerationRun/EvaluationResult、人工修订信号和审计版本。输出固定为：

```text
var/reports/model-version-updates/<comparison_id>.md
var/reports/model-version-updates/<comparison_id>.json
```

JSON遵循`schemas/model-version-report.schema.json`并作为机器事实源，Markdown由模板渲染。手工编辑Markdown不能反向修改Evaluation或Comparison JSON。

## 2. 可比性门槛

正式升降结论要求两版使用相同的Feed、Prompt、场景、模式、关键生成配置、重复策略、Benchmark、Score Schema、预处理和Judge版本。允许的差异只有被测`model_version`及Run固有ID/时间。

不一致时报告状态为`not_comparable`。系统列出差异和已有事实，但不得计算或宣称“提升多少”。如果只缺少部分场景，状态为`partial`并在场景表中明确排除。

## 3. 聚合口径

先在相同Feed、Prompt、模式和重复序号上比较，再进行维度、场景和总体聚合。Case表一行只保存一次Run，所有组统计均由报告器临时计算，不能回填任一Case。报告同时保留：

- Canonical Score变化：跨场景稳定口径。
- Scenario Score变化：用户本次请求场景下的动态权重口径。
- 覆盖率、有效样本数、离散程度和Hard Gate变化。

同一`模型版本 + feed编号 + prompt编号`重复组的主分数为算术平均百分比；生成失败、结果无效和阻断型Hard Gate按0%进入平均。详细数据同时保留逐Run分数、样本数、中位数、最小值、最大值、标准差、P25/P75、成功率和Hard Gate失败数。

维度变化必须能回溯到细则变化。报告JSON的`criterion_results`对两版每条可评细则保存百分比均值、差值、两侧可评数和证据Run。历史版本没有细则结果时不得用维度分反推，必须Replay后才做细则升降结论。

两版比较必须使用相同计划重复数或完成配对后再聚合，不能用样本数不同的裸平均数直接比较。缺失配对和不可评样本单独报告，不得静默剔除。

## 4. P0/P1/P2三级总结

- P0：先报告新模型总体分数变化，再列达到当前Score Schema显著改进条件的场景和维度。
- P1：列入持平区间且没有新增Hard Gate失败的场景和维度。
- P2：列出达到劣化条件的项目；任何新增Hard Gate失败无条件进入P2。

`P0/P1/P2`是报告章节，不代表任务调度优先级。显著改进、持平和劣化阈值必须来自已发布Score Schema的`comparison_policy`；缺失时只报告数值变化，不能自行发明统计阈值。

## 5. 场景要求

Run Request的`requested_scene_ids`是强约束。每个请求场景必须在报告中拥有独立小节，包含权重Profile/规则、分项变化、P0/P1/P2归类、证据ID和结论。全局平均不能替代分场景报告。

## 6. 人工信号

报告区分纯AI原始比较和应用Human Override后的最终比较。人工信号必须标明`blind`或`ai_assisted`；两套数值均保留可追溯关系。人工修订可以改变最终P0/P1/P2归类，但不能覆盖原AI结果。

## 7. 生成器合同

目标模块：`src/xmax_test/reporting/comparison.py`、`classification.py`、`renderer.py`和`repository.py`。生成器必须：验证可比性、生成JSON、从固定模板渲染Markdown、校验所有请求场景存在、拒绝未替换占位符，并保存模板哈希与生成器版本。

目标命令：

```bash
xmax-test report model-update --request config/run-request.json
```

成功退出前验证报告文件存在、P0/P1/P2章节均非空、所有请求场景均出现、审计引用可解析。
