# 模型版本更新报告

本文只定义基线模型与新模型的对比、三级归类和报告生成规则；报告版式见`report-templates/model-version-update-report.md`。

## 1. 输入与输出

输入为同一Run Request中的`comparison`、两个模型版本的TestPlan/GenerationRun/EvaluationResult、人工修订信号和审计版本。输出固定为：

```text
var/reports/model-version-updates/<comparison_id>.md
var/reports/model-version-updates/<comparison_id>.json
```

JSON遵循`schemas/model-version-report.schema.json`并作为机器事实源，Markdown由模板渲染。手工编辑Markdown不能反向修改Evaluation或Comparison JSON。确定性报告器只生成可复核统计和证据包，并写入`analysis_required=true`；最终变化说明、问题影响、行动和验收必须由执行Agent结合代表性媒体与Benchmark锚点撰写。

## 2. 可比性门槛

正式升降结论要求两版使用相同的Feed、Prompt、场景、模式、Provider中立生成合同、重复策略、Benchmark、Score Schema、预处理和Judge版本。同Provider版本对比时，关键生成配置也必须完全相同。XMAX与Lucy跨Provider对比时，允许模型ID、ProviderID及其无法等值的原生参数（如XMAX `quality/fps`与Lucy `resolution/self_anchor`）不同，但两侧原始配置都必须保留供审计。

不一致时报告状态为`not_comparable`。系统列出差异和已有事实，但不得计算或宣称“提升多少”。如果只缺少部分场景，状态为`partial`并在场景表中明确排除。每对Run还必须使用一致的评分口径：适用细则集合和有效维度权重任一不同，该配对都不进入分数差、标准差或稳定性统计。

## 3. 聚合口径

先在相同Feed、Prompt、模式和重复序号上比较，再进行维度、场景和总体聚合。Case表一行只保存一次Run，所有批次统计均由报告器临时计算，不能回填任一Case。当前Benchmark按相同核心场景分别比较Scenario Score、覆盖率、有效样本数、离散程度和Hard Gate变化，不计算跨场景Canonical变化。报告Schema中的Canonical字段仅为旧Benchmark兼容；对当前Benchmark必须为空且不得据此得出升降结论。

同一`模型版本 + feed编号 + prompt编号`重复组的主分数为算术平均百分比；生成失败、结果无效和阻断型Hard Gate按0%进入平均。报告中“得分”默认都是0–100%的平均分；两版相减、标准差和不稳定率差使用百分点，只有相对变化使用`%`。详细数据同时保留逐Run分数、样本数、中位数、最小值、最大值、标准差、P25/P75、成功率和Hard Gate失败数。

维度变化必须能回溯到细则变化。报告必须从当前Benchmark枚举所有适用维度和全部细则，每条只出现一次；两侧分别标明已评分、部分评分、不适用、不可评或未覆盖。报告JSON的`criterion_results`对两版每条细则保存百分比均值、差值、两侧可评数和证据Run。历史版本没有细则结果时不得用维度分反推，必须Replay后才做细则升降结论。

P.2批次生成成败、P.3同输入重复稳定性、P.4离线生成时效性、RP.1启动与画面交付、RP.2稳定与恢复、RP.3持续端到端时延与抖动不进入单视频分数，但必须在对比报告中逐项出现；当批次不适用或证据不足时明确标状态，不得省略或伪造结论。

两版比较必须使用相同计划重复数或完成配对后再聚合，不能用样本数不同的裸平均数直接比较。缺失配对和不可评样本单独报告，不得静默剔除。

## 4. P0/P1/P2三级总结

- P0：先报告新模型总体分数变化，再列达到当前Score Schema显著改进条件的场景和维度。
- P1：列入持平区间且没有新增Hard Gate失败的场景和维度。
- P2：列出达到劣化条件的项目；任何新增Hard Gate失败无条件进入P2。

`P0/P1/P2`是报告章节，不代表任务调度优先级。显著改进、持平和劣化阈值必须来自已发布Score Schema的`comparison_policy`；缺失时只报告数值变化，不能自行发明统计阈值。

每个P0/P1/P2项必须按“关键数据→变化/问题说明→行动与验收”呈现。关键数据至少包含两版平均分、百分点差和证据引用；说明必须把数字翻译为实际变化或问题及用户影响，不得重复评分锚点表。

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

成功退出前验证报告文件存在、P0/P1/P2章节均出现、所有请求场景均出现、全部Benchmark细则与P.2/P.3/P.4/RP.1/RP.2/RP.3均有结果或状态、审计引用可解析。只有完成Agent针对性分析后，才可将该Markdown当作最终分析交付。
