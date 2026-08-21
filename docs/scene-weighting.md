# 场景动态权重

本文只说明场景标签如何确定性地改变总分权重；不定义任何具体维度或实际权重值。

## 1. 分离原则

Judge只输出分项Judgment，不感知最终权重。Fusion读取Benchmark和Scenario Pack，输出：

- Canonical Score：固定Profile，跨场景对比。
- Scenario Score：按场景规则调整，评估具体使用场景。

细则层当前等权：每个维度先计算`sum(可评细则分) / (2 × 可评细则数)`，再应用维度场景权重。场景权重不会改变维度内部细则分。未来如需细则权重，必须发布新Benchmark/Score Schema并Replay，不得在报告器或Judge里临时加权。

## 2. 合同

Benchmark合同包含：

- `weight_profiles`：离线/实时基础权重。
- `scene_weight_rules`：适用Profile、标签匹配、优先级、乘数和覆盖值。
- `hard_gates`：不可被权重抵消的失败条件。
- `score_schemas`：指定Canonical/Scenario计算和发布状态。

具体结构由 `schemas/benchmark.schema.json` 校验。

## 3. 标签来源

优先级：人工整理的Scenario Pack/TestPlan标签 → CV可确定事实 → Codex带置信度补充。低置信度推断不得触发高影响规则。

场景规则可以直接匹配Scenario Pack中的稳定`scenario_id`，也可以引用已声明的标签和值，避免拼写差异静默改变分数。

## 4. 解析算法

实现：`src/xmax_test/evaluation/weights.py`。

```text
选择支持当前mode的基础Profile
→ 复制基础raw weights
→ 过滤Active规则
→ 过滤不适用于当前Profile的规则
→ 按priority、rule_id稳定排序
→ 匹配mode和scene tags
→ 依次应用multiplier/override
→ 移除规则明确标记为不适用的维度
→ 按profile最大倍率封顶
→ 移除不可评维度
→ 对剩余权重归一化
→ 保存命中规则和排除维度
```

规则引用基础Profile不存在的维度必须报错，不能静默忽略。

## 5. 规则示例

以下仅展示格式，不代表正式标准：

```json
{
  "rule_id": "example-fast-motion",
  "version": "1.0.0",
  "status": "shadow",
  "priority": 100,
  "applicable_profile_ids": ["example-offline"],
  "when": {
    "all": [
      {"mode": "offline"},
      {"tag": "motion", "equals": "fast"}
    ]
  },
  "weight_multipliers": {
    "D_ACTION": 1.5,
    "D_STRUCTURE": 1.3
  }
}
```

正式ID和值只在未来Benchmark和Scenario Pack中填写。

## 6. 不可评维度

维度为 `assessable=false` 时从场景权重分母移除，剩余维度重新归一化。缺失Judge覆盖与“场景中不适用”必须分别记录，不能都当作不可评估掩盖。

## 7. 硬门槛

Hard Gate可设置Fail、总分上限或阻止发布。执行顺序：原始有效性Gate → 维度Gate → 权重计算 → 总分上限。权重规则不能修改或关闭Gate。

## 8. 结果审计

`schemas/evaluation-result.schema.json`要求保存基础Profile、版本、命中规则、场景标签哈希、有效权重、排除维度和应用Gate。相同输入、Benchmark和Scenario Pack必须得到相同权重。

## 9. 发布

新规则先Shadow，只输出影子Scenario Score。验证没有非预期排序翻转且人工/业务目标改善后，随新Score Schema版本激活。历史结果通过Replay生成新Evaluation，不覆盖旧分。
