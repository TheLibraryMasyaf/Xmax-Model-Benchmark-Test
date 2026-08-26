# XMAX 模型版本更新评测报告

> 本模板用于对同一组 Feed × Prompt × 场景下的基线模型和新模型进行版本对比。所有占位符必须由结果或审计数据填入；没有证据时写“未取得/不可比较”，不得猜测。

## 0. 报告信息

| 项目 | 内容 |
| --- | --- |
| Comparison ID | `{{ comparison_id }}` |
| 测试日期 | `{{ tested_at }}` |
| 基线模型版本 | `{{ baseline_model_version }}` |
| 新模型版本 | `{{ candidate_model_version }}` |
| Benchmark版本 | `{{ benchmark_version }}` |
| Score Schema版本 | `{{ score_schema_version }}` |
| Scenario Pack版本 | `{{ scenario_pack_version }}` |
| Judge版本集合 | `{{ judge_versions }}` |
| 测试计划/配置哈希 | `{{ plan_hash }}` / `{{ generation_config_hash }}` |
| 请求场景 | `{{ requested_scenes }}` |
| 报告状态 | `complete / partial / not_comparable` |

## 1. 可比性与覆盖范围

### 1.1 可比性结论

`{{ comparability_conclusion }}`

只有 Feed、Prompt、场景标签、生成模式、关键生成配置、重复策略、Benchmark、Score Schema和Judge版本满足已发布的比较规则时，才可计算正式升降结论。不一致项必须列在下表；`not_comparable`时仍可报告事实，但不能宣称模型提升或劣化。

| 检查项 | 基线版本 | 新版本 | 是否一致 | 影响 |
| --- | --- | --- | --- | --- |
| Feed/Prompt/TestCase集合 | `{{...}}` | `{{...}}` | `{{...}}` | `{{...}}` |
| 场景标签和权重Profile | `{{...}}` | `{{...}}` | `{{...}}` | `{{...}}` |
| 生成模式和关键配置 | `{{...}}` | `{{...}}` | `{{...}}` | `{{...}}` |
| 重复次数和有效Run数 | `{{...}}` | `{{...}}` | `{{...}}` | `{{...}}` |
| Benchmark/Score Schema | `{{...}}` | `{{...}}` | `{{...}}` | `{{...}}` |
| Judge/预处理版本 | `{{...}}` | `{{...}}` | `{{...}}` | `{{...}}` |

### 1.2 场景覆盖

| 场景 | 模式 | 计划样本 | 基线有效Run | 新版有效Run | 自动评测覆盖率 | 结论可信度 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `{{ scene }}` | `offline/realtime` | `{{ n }}` | `{{ n }}` | `{{ n }}` | `{{ percent }}` | `{{ level }}` |

## 2. 三级总结

### P0 — 新模型总分提升与明显改进

#### 总分变化

| 分数口径 | 基线模型 | 新模型 | 变化值 | 变化比例 | 结论 |
| --- | ---: | ---: | ---: | ---: | --- |
| Canonical Score（旧Benchmark兼容；当前应为空） | `{{ score }}` | `{{ score }}` | `{{ delta_points }}` | `{{ delta_percent }}` | `{{ conclusion }}` |
| 请求场景加权Scenario Score | `{{ score }}` | `{{ score }}` | `{{ delta_points }}` | `{{ delta_percent }}` | `{{ conclusion }}` |

一句话结论：新模型总分 `{{ 提升/下降/持平 }}` `{{ delta_points }}` 分（`{{ delta_percent }}`），主要由 `{{ top_drivers }}` 驱动。

#### 明显改进项

只列达到当前Score Schema显著改进条件的维度/场景；未配置显著性规则时写明“仅观察到数值上升，尚不能判定明显改进”。

| 排名 | 场景/维度 | 基线 | 新版 | 变化 | 影响权重 | 证据与典型样本 | 可信度 |
| ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | `{{ item }}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{ evidence_ids }}` | `{{...}}` |

改进解释：`{{ improvement_analysis }}`

### P1 — 新模型持平项

只列变化落入当前Score Schema持平区间，且没有新增Hard Gate失败的项目。

| 场景/维度 | 基线 | 新版 | 变化 | 波动/区间 | 证据 | 结论 |
| --- | ---: | ---: | ---: | --- | --- | --- |
| `{{ item }}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{ evidence_ids }}` | 持平 |

持平总结：`{{ tie_analysis }}`

### P2 — 新模型劣化项

任何新增Hard Gate失败必须列入本节，即使总分仍然上升。按业务影响、下降幅度和覆盖样本数排序，不得因整体提升而省略。

| 严重度 | 场景/维度 | 基线 | 新版 | 变化 | 受影响样本 | Hard Gate | 证据 | 建议动作 |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| `blocker/high/medium/low` | `{{ item }}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{ n }}` | `{{ gate_id/none }}` | `{{ evidence_ids }}` | `{{ action }}` |

劣化解释：`{{ regression_analysis }}`

## 3. 分场景详细结果

> 所有面向读者的得分均以百分比显示。每个Feed × Prompt重复组的主分数为包含生成失败0%的算术平均；单次Case仍独立列出，聚合统计不回写Case数据表。

重复组详细统计至少包括：样本数、逐Case百分比、平均值、中位数、最小值、最大值、标准差、P25、P75、生成成功率和Hard Gate失败数。

| 版本 | 样本数 | 逐Case分数（%） | 平均 | 中位数 | 最小 | 最大 | 标准差 | P25 | P75 | 成功率 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 基线 | `{{ baseline_n }}` | `{{ baseline_case_scores_percent }}` | `{{ baseline_mean }}` | `{{ baseline_median }}` | `{{ baseline_min }}` | `{{ baseline_max }}` | `{{ baseline_stdev }}` | `{{ baseline_p25 }}` | `{{ baseline_p75 }}` | `{{ baseline_success_rate }}` |
| 新版 | `{{ candidate_n }}` | `{{ candidate_case_scores_percent }}` | `{{ candidate_mean }}` | `{{ candidate_median }}` | `{{ candidate_min }}` | `{{ candidate_max }}` | `{{ candidate_stdev }}` | `{{ candidate_p25 }}` | `{{ candidate_p75 }}` | `{{ candidate_success_rate }}` |

> 按用户本次要求的场景逐个复制本节；不能只给全局平均值。

### 3.x `{{ scenario_id }} — {{ scenario_name }}`

场景目标：`{{ scenario_goal }}`  
模式：`offline / realtime`  
Weight Profile及命中规则：`{{ profile_and_rules }}`

| 指标/维度 | 基线均值 | 新版均值 | 变化 | 有效样本 | 分类（P0/P1/P2） |
| --- | ---: | ---: | ---: | ---: | --- |
| `{{ dimension }}` | `{{...}}` | `{{...}}` | `{{...}}` | `{{ n }}` | `{{ class }}` |

- 改进：`{{ scene_improvements }}`
- 持平：`{{ scene_ties }}`
- 劣化：`{{ scene_regressions }}`
- 典型证据：`{{ run_ids / evaluation_ids / artifact_uris }}`
- 场景结论：`{{ scene_conclusion }}`

## 4. 细则级变化

> 维度变化必须能回溯到细则。新口径报告对每条两版均可评的Benchmark细则输出均值和差值；只有旧维度分时标记需Replay，不反推。

| 维度 | 细则ID | 细则名称 | 基线 | 新版 | 变化 | 基线可评数 | 新版可评数 | 分类 | 证据 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `{{ dimension }}` | `{{ criterion_id }}` | `{{ criterion_name }}` | `{{ baseline_criterion_score }}` | `{{ candidate_criterion_score }}` | `{{ criterion_delta }}` | `{{ baseline_criterion_n }}` | `{{ candidate_criterion_n }}` | `{{ criterion_class }}` | `{{ evidence_ids }}` |

## 5. 稳定性、运行与失败事实

离线和实时事实分开报告，不能用视觉联系图推断延迟、FPS或网络指标。

| 指标 | 基线 | 新版 | 变化 | 数据来源 |
| --- | ---: | ---: | ---: | --- |
| 生成成功率 | `{{...}}` | `{{...}}` | `{{...}}` | GenerationRun |
| 重复生成稳定性 | `{{...}}` | `{{...}}` | `{{...}}` | Repeat aggregate |
| 首帧/响应延迟（实时） | `{{...}}` | `{{...}}` | `{{...}}` | RTC/Harness timestamps |
| 有效FPS/冻结（实时） | `{{...}}` | `{{...}}` | `{{...}}` | Per-frame facts |
| 费用/积分 | `{{...}}` | `{{...}}` | `{{...}}` | API billing facts |

失败与跳过：`{{ failures_and_skips }}`

## 6. 人工信号与AI结论修订

| 项目 | 数量/结论 |
| --- | --- |
| 独立人工评测样本 | `{{ n }}` |
| AI结果反馈样本 | `{{ n }}` |
| Human Override | `{{ n }}` |
| 因人工信号改变的P0/P1/P2结论 | `{{ details }}` |
| 未处理或低置信度信号 | `{{ details }}` |

人工结论必须标注`blind`或`ai_assisted`，原AI结果和修订后结果同时保留。

## 7. 发布建议

- 建议：`promote / shadow / block / 补测后决定`
- 阻断原因：`{{ blockers_or_none }}`
- 建议补测场景：`{{ follow_up_scenes }}`
- 建议修复重点：`{{ priorities }}`
- 可接受风险：`{{ accepted_risks_or_none }}`

## 8. 审计附录

- Plan、Run、Evaluation、Comparison产物路径：`{{ artifact_uris }}`
- Benchmark、Scenario Pack和Judge内容哈希：`{{ hashes }}`
- 权重Profile、命中规则和Hard Gate轨迹：`{{ weight_and_gate_audit }}`
- 原始自动评测和人工信号索引：`{{ raw_index }}`
- 报告生成器版本及模板哈希：`{{ reporter_version }}` / `{{ template_hash }}`
