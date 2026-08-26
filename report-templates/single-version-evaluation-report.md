# XMAX 单版本/单批次评测报告

> 用于单个模型版本的一次指定批次。正文只保留结论、关键数据、问题、行动和审计信息，不复述设计过程或讨论上下文。
>
> Benchmark全部评分细则和非评分指标都必须出现；缺失时标明状态。相同信息只呈现一次。

## 0. 报告信息

| 项目 | 内容 |
| --- | --- |
| Report ID | `{{ report_id }}` |
| 模型版本 | `{{ model_version }}` |
| 测试日期 | `{{ tested_at }}` |
| Plan / Run Batch / Evaluation Batch | `{{ plan_id }}` / `{{ run_batch_id }}` / `{{ evaluation_batch_id }}` |
| Benchmark版本 | `{{ benchmark_version }}` |
| Score Schema版本 | `{{ score_schema_version }}` |
| Scenario Pack版本 | `{{ scenario_pack_version }}` |
| Judge版本集合 | `{{ judge_versions }}` |
| 预处理版本 | `{{ preprocessor_version }}` |
| 生成模式 | `offline / realtime / mixed` |
| 请求场景 | `{{ requested_scenes }}` |
| 报告状态 | `complete / partial` |

## 1. 测试范围与数据完整性

| 项目 | 数量 | 占比 | 说明 |
| --- | ---: | ---: | --- |
| 计划Case | `{{ planned_case_count }}` | 100% | `{{ plan_scope }}` |
| 已完成GenerationRun | `{{ completed_run_count }}` | `{{ completed_rate }}` | `{{ details }}` |
| 生成失败 | `{{ generation_failure_count }}` | `{{ generation_failure_rate }}` | 进入总分时0% |
| 已完成评测 | `{{ evaluated_run_count }}` | `{{ evaluation_coverage }}` | `{{ details }}` |
| 部分或全部不可评 | `{{ unassessable_count }}` | `{{ unassessable_rate }}` | `{{ missing_evidence }}` |
| Hard Gate失败 | `{{ hard_gate_failure_count }}` | `{{ hard_gate_failure_rate }}` | `{{ gate_ids }}` |
| 人工修订 | `{{ human_override_count }}` | `{{ human_override_rate }}` | `blind / ai_assisted / none` |

完整性结论：`{{ coverage_conclusion }}`

## 2. 总分与总体分布

> 得分统一使用0–100%；未映射场景标记`missing_scene_weight_rule`。

| 总分口径 | 平均 | 中位数 | 最小 | 最大 | 标准差（百分点） | P25 | P75 | 有效Run |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Scenario Score | `{{ scenario_mean }}` | `{{ scenario_median }}` | `{{ scenario_min }}` | `{{ scenario_max }}` | `{{ scenario_stdev }}` | `{{ scenario_p25 }}` | `{{ scenario_p75 }}` | `{{ scenario_n }}` |

| 运行与稳定性指标 | 结果 | 数据来源 |
| --- | ---: | --- |
| P.2有效视频率/无效输出率/失败分布 | `{{ generation_success_and_failure }}` | Frozen Run Batch + P.1 |
| P.3同输入重复稳定性 | `{{ repeat_group_stability }}` | Frozen Run Request；`repeat_count`可配置；逐Case表合并展示组内标准差与稳定性 |
| Hard Gate通过率 | `{{ hard_gate_pass_rate }}` | EvaluationResult |
| 可评维度覆盖率 | `{{ assessable_dimension_rate }}` | Judgment |
| RP.1启动与画面交付 | `{{ realtime_delivery }}` | RTC/Harness timestamps + Per-frame facts |
| RP.2稳定与恢复 | `{{ realtime_stability_and_recovery }}` | 异常脚本/长会话事实 |

### 2.1 P.3同输入重复稳定性

> 仅在同输入且评分口径一致的重复组内计算；标准差单位为百分点，口径不一致组不参与稳定性判定。

| 指标 | 结果 | 定义 |
| --- | ---: | --- |
| 稳定组 / 可判定组 | `{{ stable_group_count }}/{{ classified_group_count }}` | 组内Case总分总体标准差≤10分 |
| 不稳定组 / 可判定组 | `{{ unstable_group_count }}/{{ classified_group_count }}` | 组内Case总分总体标准差>10分 |
| 组间不稳定率 | `{{ unstable_group_rate_percent }}` | 不稳定重复组数÷可判定重复组总数；不做组均分两两比较 |
| 组内标准差分布 | `{{ group_standard_deviation_stats }}` | 各重复组总体标准差的均值、中位数、最小值、最大值 |
| 评分口径不一致组 | `{{ basis_mismatch_group_count }}` | 组内适用细则或有效维度权重不一致，单独标注、不判稳定性 |

稳定性结论：`{{ repeat_stability_conclusion }}`；组明细放入逐Case表。

总体结论：`{{ overall_conclusion }}`

## 3. 执行摘要

### 3.1 一句话结论

`{{ one_sentence_summary }}`

### 3.2 表现较好的维度

| 排名 | 维度 | 得分 | 有效权重 | 覆盖样本 | 表现好的可见证据 | 结论依据 |
| ---: | --- | ---: | ---: | ---: | --- | --- |
| 1 | `{{ dimension_id / name }}` | `{{ score_percent }}` | `{{ weight }}` | `{{ n }}` | `{{ evidence_ids_and_summary }}` | `{{ threshold_or_relative_rank }}` |

强项总结：`{{ strengths_summary }}`

### 3.3 表现不足的维度

| 排名 | 维度 | 得分 | 受影响Run | 主要问题 | 典型证据 | 用户影响 | 根因置信度 |
| ---: | --- | ---: | ---: | --- | --- | --- | ---: |
| 1 | `{{ dimension_id / name }}` | `{{ score_percent }}` | `{{ n }}` | `{{ visible_failure }}` | `{{ evidence_ids }}` | `{{ impact }}` | `{{ confidence }}` |

短板总结：`{{ weaknesses_summary }}`

## 4. 全量评分维度得分

> 必须列出当前模式下全部适用维度，不能只展示高分或低分项。不适用和不可评必须分开。

| 维度（ID与名称） | 模式 | 维度原始分 | 归一化得分 | 有效权重 | 加权贡献 | 可评Run/总Run | 标准差（百分点） | 状态 | 证据 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `{{ dimension_id }} {{ dimension_name }}` | `{{ mode }}` | `{{ raw_score }}/2` | `{{ score_percent }}` | `{{ effective_weight }}` | `{{ weighted_contribution }}` | `{{ assessable_n }}/{{ total_n }}` | `{{ stdev_percent_points }}` | `strong / acceptable / weak / unassessable / inapplicable` | `{{ evidence_ids }}` |

维度排名与权重解读：`{{ dimension_analysis }}`

## 5. 评分细则得分

> 从Benchmark枚举全部细则，每条只出现一次；缺失时标记不适用、不可评或未覆盖；禁止把维度分平均拆给各细则。

| 维度 | 细则ID与名称 | 状态 | 归一化得分 | 可评Run/应出现Run | 不可评 | 不适用 | 未覆盖 | 标准差（百分点） | 0 / (0,1) / 1 / (1,2) / 2 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `{{ dimension_id / name }}` | `{{ criterion_id / name }}` | `已评分 / 部分评分 / 不适用 / 不可评 / 未覆盖` | `{{ score_percent }}` | `{{ assessable_n }}/{{ eligible_n }}` | `{{ unassessable_n }}` | `{{ inapplicable_n }}` | `{{ uncovered_n }}` | `{{ stdev }}` | `{{ n0 }}/{{ n0_to_1 }}/{{ n1 }}/{{ n1_to_2 }}/{{ n2 }}` |

细则覆盖结论：`{{ criterion_coverage_conclusion }}`

## 6. 分场景详细结果

> 按用户本次指定的场景逐个复制本节。不能用全局平均代替场景结论。

### 6.x `{{ scenario_id }} — {{ scenario_name }}`

- 场景目标：`{{ scenario_goal }}`
- 模式：`offline / realtime`
- Weight Profile及命中规则：`{{ profile_and_rules }}`
- 样本量：`{{ n }}`
- Scenario Score：`{{ scenario_score_percent }}`
- 生成成功率：`{{ generation_success_rate }}`

| 维度 | 得分 | 权重 | 标准差 | 主要优点 | 主要问题 | 证据 |
| --- | ---: | ---: | ---: | --- | --- | --- |
| `{{ dimension }}` | `{{ score }}` | `{{ weight }}` | `{{ stdev }}` | `{{ strength }}` | `{{ weakness }}` | `{{ evidence_ids }}` |

场景结论：`{{ scene_conclusion }}`

## 7. 典型Good Case与Bad Case

### 7.1 Good Case

| Case编号 | 总分 | 场景 | 主要优点 | 可复查证据 | 是否具有代表性 |
| --- | ---: | --- | --- | --- | --- |
| `{{ case_number }}` | `{{ score_percent }}` | `{{ scene }}` | `{{ strength }}` | `{{ evidence_uri }}` | `yes / no` |

### 7.2 Bad Case

| Case编号 | 总分 | 场景 | 失败维度/细则 | 可见现象 | 影响 | 证据 | 建议复测 |
| --- | ---: | --- | --- | --- | --- | --- | --- |
| `{{ case_number }}` | `{{ score_percent }}` | `{{ scene }}` | `{{ dimension_or_criterion }}` | `{{ visible_failure }}` | `{{ impact }}` | `{{ evidence_uri }}` | `{{ retest_scope }}` |

## 8. P0/P1/P2改进优先级

> P0/P1/P2表示改进优先级。每项必须在关键数据后解释数据对应的实际问题，再给出行动与验收；保持简短。

### P0 — 发布/可用性阻断，必须优先修复

适用于Hard Gate失败、生成不可用、核心指令大面积失败、严重结构崩坏或已发布阈值明确判定为阻断的问题。

| 优先问题 | 关键数据 | 问题说明 | 行动与验收 |
| --- | --- | --- | --- |
| `{{ issue }}` | `{{ key_metrics_and_evidence }}` | `{{ observed_problem_and_user_impact }}` | `{{ action_acceptance_and_retest_scope }}` |

### P1 — 明显短板，建议下一轮优先改进

适用于多样本重复出现、显著影响质量或场景完成度，但未构成Hard Gate阻断的问题。

| 优先问题 | 关键数据 | 问题说明 | 行动与验收 |
| --- | --- | --- | --- |
| `{{ issue }}` | `{{ key_metrics_and_evidence }}` | `{{ observed_problem_and_user_impact }}` | `{{ action_acceptance_and_retest_scope }}` |

### P2 — 局部优化，可在核心问题后改进

适用于少数Case、轻微体验问题、高方差但平均尚可、或不影响主要任务完成的优化项。

| 优先问题 | 关键数据 | 问题说明 | 行动与验收 |
| --- | --- | --- | --- |
| `{{ issue }}` | `{{ key_metrics_and_evidence }}` | `{{ observed_problem_and_user_impact }}` | `{{ action_acceptance_and_retest_scope }}` |

## 9. 人工信号与AI结论修订

| 项目 | 数量/结论 |
| --- | --- |
| 独立人工评测样本 | `{{ n }}` |
| AI结果反馈样本 | `{{ n }}` |
| Human Override | `{{ n }}` |
| 因人工信号变化的分数/优先级 | `{{ details }}` |
| 未处理或低置信度信号 | `{{ details }}` |

人工修订前后结果都必须保留，不得覆盖原AI Judgment。

## 10. 最终结论与建议

- 当前总体水平：`{{ overall_level_and_basis }}`
- 最可靠的能力：`{{ strongest_capabilities }}`
- 最主要的短板：`{{ main_weaknesses }}`
- P0建议：`{{ p0_summary_or_none }}`
- P1建议：`{{ p1_summary_or_none }}`
- P2建议：`{{ p2_summary_or_none }}`
- 建议下一步：`continue_testing / targeted_retest / block_current_use / ready_for_comparison`
- 建议补测场景：`{{ follow_up_scenes }}`
- 当前不能得出的结论：`{{ unsupported_conclusions }}`

## 11. 审计附录

- Plan、Run、Evaluation产物路径：`{{ artifact_uris }}`
- Benchmark、Scenario Pack、Judge和生成配置哈希：`{{ hashes }}`
- 权重Profile、命中规则和Hard Gate轨迹：`{{ weight_and_gate_audit }}`
- 原始Judgment、CV/Metric输出和人工信号索引：`{{ raw_index }}`
- 本次逐Case分数：`{{ case_scores_percent }}`

## 12. 逐Case结果

> 同组Case连续排列；标准差和稳定性使用合并单元格。

| Case | Run | 状态 | 场景 | 重复序号 | 分数 | 组内总分标准差（百分点） | 稳定性 | Evaluation |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |
| `{{ case_number }}` | `{{ run_id }}` | `{{ status }}` | `{{ scenario_id }}` | `{{ repeat_index }}` | `{{ score_percent }}` | `{{ merged_repeat_group_standard_deviation }}` | `{{ merged_repeat_group_stability }}` | `{{ evaluation_id }}` |
- 报告生成者/执行Agent：`{{ reporter_identity }}`
- 模板路径及哈希：`report-templates/single-version-evaluation-report.md` / `{{ template_hash }}`
