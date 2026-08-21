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

命令会生成同名Markdown和JSON，并强制校验批次中的模型版本，不扫描历史全库。模板`report-templates/single-version-evaluation-report.md`是人读字段合同；CLI按同一口径动态渲染全量维度、细则、Case和P0/P1/P2建议，不留未替换占位符。

## 2. 分数口径

- 总分和Case分始终用0–100%。
- Canonical Score和Scenario Score必须分开报告。
- 生成失败、结果无效和阻断型Hard Gate按0%进入统计。
- 一组重复实验同时报告逐Case分数、平均、中位数、最小/最大、标准差和P25/P75。
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

这是改进优先级，不是版本更新报告的P0改进/P1持平/P2劣化分类。每条建议必须写明受影响样本、证据、建议动作、验收指标的来源和复测范围。

## 6. 完成检查

- 总分、全量适用维度、细则覆盖说明、强项、短板和P0/P1/P2建议都已填写。
- 所有请求场景有独立小节。
- 生成失败、不可评、Hard Gate和人工修订没有被平均值隐藏。
- 每个Good Case/Bad Case都有可回读证据。
- 报告中没有未替换占位符；未取得数据明确标记。
- 报告没有宣称未执行的版本对比。
