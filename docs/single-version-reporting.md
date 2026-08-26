# 单版本/单批次评测报告

本文只定义一个模型版本在一次指定测试批次中的绝对表现报告。模板见`report-templates/single-version-evaluation-report.md`；它不替代两版可比性报告。

## 1. 适用范围

- 某个模型版本的第一轮完整测试。
- 一次特定场景、特定Feed/Prompt子集或回归集的测试。
- 只需要回答“当前版本表现如何”，不需要声明相对其他版本提升或劣化。

输出建议写入：

```text
var/reports/single-version/<report_id>.md
```

使用`config/single-version-report.example.json`复制一份请求，必须填写精确的Run Batch和Evaluation Batch：

```bash
xmax-test report single-version --request config/single-version-report.json
```

命令会生成同名Markdown和JSON，并强制校验批次中的模型版本，不扫描历史全库。模板`report-templates/single-version-evaluation-report.md`是人读字段合同；CLI按同一口径动态渲染全量维度、细则、Case和P0/P1/P2证据包，不留未替换占位符。

报告器必须从Benchmark正向枚举全部评价要求，不能只从本批已经产生的EvaluationResult反向收集。所有0/1/2评分细则统一在唯一一张“细则结果”表中列出状态、分数、分布和覆盖缺口，不得再复制一张内容相同的“完整性”表；`reporting_metrics`因不使用0/1/2，单独列结构化指标。没有数据时仍保留对应行并标记不适用、不可评、未执行实验或未覆盖。

正式报告正文必须简洁：不复述设计取舍、Agent执行过程或对话上下文；同一数字和规则不在多个章节重复解释。摘要只保留总体分、覆盖、主要强弱项和下一步；P0/P1/P2每项按“关键数据→问题说明→行动与验收”呈现，问题说明必须把数字翻译成实际失败现象和用户影响。

面向读者的标准差统一换算为0–100分上的“百分点”：维度和细则的0–2原始标准差乘以50，总分标准差直接使用0–100分结果。P.3只在同输入且评分口径一致的重复组内判定；组内适用细则或有效维度权重不一致时标记为`basis_mismatch`，不进入稳定/不稳定分母。

确定性报告器只生成可复核统计、可信度诊断和证据包，并写`analysis_required=true`；它不得用固定脚本冒充执行Agent生成针对性建议。最终建议必须由执行Agent读取证据包、代表性媒体和Benchmark锚点后撰写。P.3会记录实际参与统计的`member_run_ids`，并按模型、Feed、Prompt文字、Prompt素材、配方、生成参数、场景与模式重建重复组；缺少来源、引用Manifest外Run或成员不一致都会把报告降为`diagnostic`，不得作为正式结论发布。

## 2. 分数口径

- 总分和Case分始终用0–100%。
- 当前Benchmark只报告Scenario Score；`canonical_score`为兼容字段且必须为`null`。
- 生成失败、结果无效和阻断型Hard Gate按0%进入统计。
- P.2/P.3只作为批次统计，不产生0/1/2评分，也不回灌单视频。
- 一组重复实验同时报告实际重复次数、成员Run、完成率、有效率、平均、中位数、最小/最大、标准差和离散范围。
- P.3正式报告必须给出稳定组数、不稳定组数、不稳定组占比，并汇总各组Case总分的总体标准差；不得只报告重复组总数。
- 当前报告诊断策略`repeat-score-population-sd-10-v1`以组内Case总分总体标准差大于10分判为不稳定，小于等于10分判为稳定。“组间不稳定率”定义为不稳定重复组数除以可判定重复组总数，不做组均分两两比较。该策略不是Benchmark单视频0/1/2评分线；调整阈值必须升级`policy_id`。
- P.3摘要不单独列不稳定组明细；逐Case表按重复组连续排列，并用HTML `rowspan`合并“组内总分标准差”和“稳定性”两列。机器可读JSON的每条Case同时保存`repeat_group_id`、`repeat_group_standard_deviation`和`repeat_group_stability`。
- 不适用维度从分子和分母同时剔除；不可评要单独报告覆盖缺口。

## 3. 维度与细则

细则分是评分事实，维度分只能来自EvaluationResult对可评细则的等权汇总。必须列出当前模式的全部适用维度和细则，不能只挑选极端分数。

细则分只能来自：

1. EvaluationResult中融合后的`criterion_results`；
2. 用于审计的Judgment.`criterion_results`与CV/Metric原始指标；
3. 经验证的人工细则级评分及其修订轨迹。

新口径Evaluation必须产出细则结果；旧历史Evaluation只有维度分时，对应细则写“旧口径，未产出细则级得分，需Replay”。禁止从维度平均分反推细则分。

批次细则表直接使用Evaluation Batch Manifest的`metadata.aggregate.criterion_summary`，或用`aggregate_evaluation_results()`从同批EvaluationResult重算。每条至少报告可评数/总数、覆盖率、均值/百分比、中位数、最小/最大、标准差、P25/P75和分布。多Judge融合可以产生0与1、1与2之间的分数，分布不得强行四舍五入成0/1/2。

## 4. 强项与短板

强项和短板的判断依据必须优先来自Benchmark/Score Schema中已发布阈值。没有阈值时只能写“在本批次中相对排名较高/较低”，不得自行发明“优秀线”或“及格线”。

每条强项和短板至少绑定一个Run/Evaluation/Artifact证据；不能只复述维度名。

## 5. P0/P1/P2语义

单版本报告中：

- P0：发布或核心可用性阻断，必须优先修复。
- P1：多样本稳定出现或明显影响场景完成度的主要短板。
- P2：局部、轻微、高方差或不阻断核心任务的优化项。

这是改进优先级，不是版本更新报告的P0改进/P1持平/P2劣化分类。确定性报告器只负责列出受影响样本和证据；执行Agent给出的每条建议必须写明建议动作、验收指标的来源和复测范围。

## 6. 完成检查

- 确定性统计、全量适用维度、细则覆盖说明、强项/短板候选和P0/P1/P2证据包都已生成。
- 执行Agent已查看代表性媒体与证据包，并填写针对本批次的建议动作、验收依据和复测范围；未完成时保留`analysis_required=true`，不能把报告当作最终分析交付。
- 所有请求场景有独立小节。
- 生成失败、不可评、Hard Gate和人工修订没有被平均值隐藏。
- 每个Good Case/Bad Case都有可回读证据。
- 报告中没有未替换占位符；未取得数据明确标记。
- 报告没有宣称未执行的版本对比。
