# XMAX 模型版本更新评测报告

> 用于同一组 Feed × Prompt × 场景下的基线版本与新版本对比。正文只保留结论、关键数据、变化说明、行动和审计信息；不可比或缺失的数据必须明确标注。

## 0. 报告信息

| 项目 | 内容 |
| --- | --- |
| Comparison ID | `{{ comparison_id }}` |
| 测试日期 | `{{ tested_at }}` |
| 基线模型版本 | `{{ baseline_model_version }}` |
| 新模型版本 | `{{ candidate_model_version }}` |
| Benchmark / Score Schema / Scenario Pack | `{{ benchmark_version }}` / `{{ score_schema_version }}` / `{{ scenario_pack_version }}` |
| Judge版本集合 | `{{ judge_versions }}` |
| 测试计划 / 生成配置哈希 | `{{ plan_hash }}` / `{{ generation_config_hash }}` |
| 请求场景 | `{{ requested_scenes }}` |
| 报告状态 | `{{ report_status }}` |

## 1. 可比性与覆盖范围

### 1.1 可比性结论

`{{ comparability_conclusion }}`

只有测试输入、生成配置、重复策略和评分口径一致的配对才进入升降计算。评分口径包括适用细则集合与有效维度权重；不一致配对必须排除。

| 检查项 | 结果 | 对升降计算的影响 |
| --- | --- | --- |
| `{{ comparability_item }}` | `{{ comparability_result }}` | `{{ comparability_impact }}` |

### 1.2 场景覆盖

| 场景 | 模式 | 可比较配对 | 基线纳入Run | 新版纳入Run | 结论边界 |
| --- | --- | ---: | ---: | ---: | --- |
| `{{ scene }}` | `offline/realtime` | `{{ n }}` | `{{ n }}` | `{{ n }}` | `{{ level }}` |

## 2. 总体结果与三级总结

> P0/P1/P2表示新版的明显改进、持平和劣化。最终报告必须由执行Agent按“关键数据→变化/问题说明→行动与验收”补全针对性分析，不得只列数字。

### 2.1 总分变化

| 分数口径 | 基线平均分 | 新版平均分 | 变化（百分点） | 相对变化（%） | 结论 |
| --- | ---: | ---: | ---: | ---: | --- |
| 请求场景加权Scenario Score | `{{ score }}` | `{{ score }}` | `{{ delta_points }}` | `{{ delta_percent }}` | `{{ conclusion }}` |

一句话结论：新模型总分`{{ 提升/下降/持平 }}` `{{ delta_points }}`个百分点，相对变化 `{{ delta_percent }}`%，主要变化来自 `{{ top_drivers }}`。

### P0 — 新模型明显改进

| 排名 | 对象 | 基线平均分 | 新版平均分 | 变化（百分点） | 关键数据 | 变化说明 | 行动与验收 |
| ---: | --- | ---: | ---: | ---: | --- | --- | --- |
| 1 | `{{ item }}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{ evidence_ids }}` | `{{ improvement }}` | `{{ action }}` |

改进总结：`{{ improvement_analysis }}`

### P1 — 新模型持平

| 对象 | 基线平均分 | 新版平均分 | 变化（百分点） | 关键数据 | 变化说明 | 行动与验收 |
| --- | ---: | ---: | ---: | --- | --- | --- |
| `{{ item }}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{ evidence_ids }}` | `{{ tie_explanation }}` | `{{ action }}` |

持平总结：`{{ tie_analysis }}`

### P2 — 新模型劣化

任何新增Hard Gate失败必须进入本节，不得因整体分数上升而省略。

| 严重度 | 对象 | 基线平均分 | 新版平均分 | 变化（百分点） | 关键数据 | 问题说明 | 行动与验收 |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| `blocker/high/medium/low` | `{{ item }}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{ evidence_ids }}` | `{{ regression }}` | `{{ action }}` |

劣化总结：`{{ regression_analysis }}`

## 3. 分场景结果

> 每个请求场景必须单独报告；平均分与标准差均基于可比较配对，标准差单位为百分点。

### 3.x `{{ scenario_id }} — {{ scenario_name }}`

- 模式：`{{ scene_mode }}`
- 可比较配对：`{{ comparable_pairs }}`
- 场景平均分变化：`{{ scene_score_delta }}`

| 维度（ID与名称） | 基线平均分 | 新版平均分 | 变化（百分点） | 基线/新版可评数 | 分类 | 变化说明 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `{{ dimension }}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{ n }}` | `{{ class }}` | `{{ explanation }}` |

- 典型证据：`{{ run_ids / evaluation_ids / artifact_uris }}`
- 场景结论：`{{ scene_conclusion }}`

## 4. 全量维度变化

| 维度（ID与名称） | 基线平均分 | 新版平均分 | 变化（百分点） | 基线/新版可评数 | 基线/新版标准差（百分点） | 分类 | 变化说明 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `{{ dimension_id }}` {{ dimension_name }} | `{{ baseline_dimension_score }}` | `{{ candidate_dimension_score }}` | `{{ dimension_delta }}` | `{{ dimension_assessable_counts }}` | `{{ dimension_stdev_points }}` | `{{ dimension_class }}` | `{{ dimension_explanation }}` |

## 5. 全量细则变化

> 从Benchmark枚举全部细则，每条只出现一次；两版分别标记已评分、部分评分、不适用、不可评或未覆盖。不得由维度分反推细则分。

| 维度 | 细则 | 基线状态与平均分 | 新版状态与平均分 | 基线/新版可评数 | 基线/新版标准差（百分点） | 变化（百分点） | 分类 | 变化说明 |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |
| `{{ dimension }}` | `{{ criterion_id }}` {{ criterion_name }} | `{{ baseline_criterion_result }}` | `{{ candidate_criterion_result }}` | `{{ criterion_assessable_counts }}` | `{{ criterion_stdev_points }}` | `{{ criterion_delta }}` | `{{ criterion_class }}` | `{{ criterion_explanation }}` |

## 6. 批次、稳定性与实时运行指标

| 指标 | 基线结果 | 新版结果 | 变化/结论 | 问题说明 |
| --- | --- | --- | --- | --- |
| P.2 批次生成成功与失败 | `{{ baseline_p2 }}` | `{{ candidate_p2 }}` | `{{ p2_delta }}` | `{{ p2_explanation }}` |
| P.3 同输入重复稳定性 | `{{ baseline_p3 }}` | `{{ candidate_p3 }}` | `{{ p3_delta }}` | `{{ p3_explanation }}` |
| RP.1 启动与画面交付 | `{{ baseline_rp1 }}` | `{{ candidate_rp1 }}` | `{{ rp1_delta }}` | `{{ rp1_explanation }}` |
| RP.2 稳定与恢复 | `{{ baseline_rp2 }}` | `{{ candidate_rp2 }}` | `{{ rp2_delta }}` | `{{ rp2_explanation }}` |

## 7. 人工信号与AI结论修订

| 项目 | 数量/结论 |
| --- | --- |
| 独立人工评测样本 | `{{ n }}` |
| AI结果反馈样本 | `{{ n }}` |
| Human Override | `{{ n }}` |
| 因人工信号改变的P0/P1/P2结论 | `{{ details }}` |

人工结论必须标注`blind`或`ai_assisted`，并保留原AI结果。

## 8. 发布建议

- 建议：`{{ release_recommendation }}`
- 阻断原因：`{{ blockers_or_none }}`
- 建议补测场景：`{{ follow_up_scenes }}`
- 建议修复重点：`{{ priorities }}`
- 可接受风险：`{{ accepted_risks_or_none }}`

## 9. 审计附录

- Plan、Run、Evaluation、Comparison产物路径：`{{ artifact_uris }}`
- Benchmark、Scenario Pack和Judge内容哈希：`{{ hashes }}`
- 权重、Hard Gate与评分口径轨迹：`{{ weight_and_gate_audit }}`
- 原始自动评测和人工信号索引：`{{ raw_index }}`
- 报告生成器版本及模板哈希：`{{ reporter_version }}` / `{{ template_hash }}`
