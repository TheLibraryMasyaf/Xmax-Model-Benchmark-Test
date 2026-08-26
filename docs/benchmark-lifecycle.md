# 评测标准生命周期

本文只说明 `BENCHMARK.md` 中维度、权重、硬门槛与总分方案如何新增、修改、停用和发布。

## 1. 单一来源

根目录 `BENCHMARK.md` 是唯一标准入口。机器读取标记之间的JSON合同，人阅读其余Markdown说明。代码、Prompt和飞书列不得形成另一个独立标准副本。

当前合同为`0.3.0-draft / shadow`，包含P.1 Gate、9个G/E/R计分标准、22条单视频细则、4个批次报告指标和10个核心场景的16组模式权重。它可以执行Shadow评测，但不作为稳定正式榜单；首轮后用新版本调整并Replay，不能原地覆盖。运行`xmax-test db check`会按Benchmark版本列出历史Evaluation；非当前版本不得与当前批次混用，需要新口径时对明确Run Batch执行`replay run`。

## 2. 维度生命周期

```text
proposed → draft → shadow → active → deprecated → archived
```

- Proposed：人工信号或分析提出。
- Draft：完成定义、锚点、证据和候选Judge。
- Shadow：执行但不影响正式分数。
- Active：正式使用。
- Deprecated：新测试停止使用，历史保留。
- Archived：日常界面隐藏，历史仍可回放。

只有从未被结果、人工标签、Judge或训练数据引用的Draft可以物理删除。

## 3. 修改规则

- Patch：文字澄清，不改变评分含义。
- Minor：新增问题类型、证据或兼容子维度。
- Major：改变定义、锚点、适用范围、拆分方式或总分含义。

已经使用的维度不能原地修改；创建新版本并保留旧版本。

## 4. 总分方案

新增维度不会自动进入总分：

```text
Shadow显示
→ Experimental总分并列
→ 发布新score_schema_version
→ Active
```

不同Score Schema的总分不直接横向比较，除非对同一数据集进行统一回放。

## 5. 权重与场景规则生命周期

`weight_profiles`、`scene_weight_rules`和`hard_gates`各自拥有稳定ID、版本和状态。修改任何数值、匹配条件、优先级、封顶或动作时都必须创建新版本，不能原地覆盖已用于正式结果的版本。

发布顺序：Draft合同检查 → Shadow并行出分 → 冻结集回放 → Holdout验证 → 随新Score Schema激活。规则只能引用当前Benchmark中的维度，以及已发布Scenario Pack中的标签和值。人工反馈可形成变更提案，但不能直接热更新正式权重。

删除规则仅限从未被Evaluation引用的Draft；其他规则使用Deprecated/Archived并保留回放能力。

## 6. 直接接入步骤

1. 编辑 `BENCHMARK.md`合同块和对应文字章节。
2. 执行 `xmax-test benchmark-check`。
3. 验证所有Active维度有兼容Judge或明确fallback。
4. 验证Profile只引用现有维度、Rule只引用合法标签/维度、Gate动作合法。
5. 生成Benchmark与Scenario Pack快照和内容哈希。
6. 对冻结回归集做Shadow回放，比较各核心场景的Scenario排序、Gate和P/G/E/R分项变化。
7. 在Holdout上验证。
8. 发布Benchmark、Weight Profile、Rule与Score Schema版本。
9. 同步飞书Judge/标准版本表。

## 7. 历史回放

维度或Judge变化后可创建Replay Run：读取旧GenerationRun和原始产物，用新Benchmark/Judge重新评测。回放结果是新的Evaluation ID，不能覆盖旧评测。
