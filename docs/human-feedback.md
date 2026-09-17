# 并行人工评测与反馈

本文只说明人工数据如何独立进入系统、如何被结构化以及如何路由学习；人工不是常规自动流程的必经节点。

## 1. 两条人工入口

### 独立人工评测集

人工可批量提供Feed、Prompt、Result及非结构化或结构化评价。样本不需要先由自动评测产生；素材进入自动评测线，人工结果进入Human Signal Hub，两边用 `sample_id`对齐。

### 自动结果反馈

人工可以确认、修改、补充或推翻任何自动结果，不要求系统先判定为争议样本。

## 2. 非结构化评价流程

```text
人工原文
→ 不可变保存
→ 可配置MLLM Normalizer
→ 映射现有维度或产生维度提案
→ 完整性/矛盾检查
→ 人工评测池
→ Train / Calibration / Holdout分区
```

LLM转写进入人工评测池，但只能产生 `normalization_review.status = pending` 的候选映射，不能自动成为人工真值。审核通过后才赋予 `learning_permission`，再进入学习池。

多表或多跳关联由来源预处理器解析，通用导入器只读取不可变 `HumanEvaluationImportPack`。相对评价在包中保留一条规范化pairwise记录，导入时再对称投影为两条单视频信号；缺失的单侧评语不会被伪造。

可执行命令：

```bash
xmax-test human import --input <independent-dataset.json>
xmax-test human feedback --input <ai-result-feedback.json>
xmax-test human prepare-feishu --config <source.json> --output <import-pack.json>
xmax-test human import-pack --input <import-pack.json>
xmax-test human normalize --pending
xmax-test human export-normalization-review --output var/feedback/normalization-review.json
xmax-test human repair-normalizations --signal-id <signal-id>
xmax-test human review-normalizations --input <review-decisions.json>
xmax-test human partition --output var/feedback/learning.jsonl
xmax-test human build-challenger --judge-id <id> --version <version> --route mlmm --train var/feedback/learning.train.jsonl
xmax-test judges promote --judge-id <id> --version <version> --validation <holdout-validation.json>
```

导入保留时间段、ROI、bbox/mask/关键点、人工分数和Feed/Prompt/Result Asset ID等扩展监督字段，Normalizer不能删除或改写它们。

Normalizer将有明确细则与0/1/2提示分的标签标为可评分锚点；只能确定缺陷方向的相对评语保留为定性锚点，不得把空分值补猜为绝对分。相对记录的 `better_than` / `worse_than` 关系会随学习包保留；评分Judge只使用Train分区中已审批的匿名锚点。

`export-normalization-review` 同时输出原评语、视频/抽帧路径、测试组合、成对关系、结构审计统计和可填写的 `decisions` 数组。审核人补入 `decision=approved|rejected`、`reviewer` 及可选 `note` 后，同一文件可直接交给 `review-normalizations`。若审核只覆盖部分飞书评分字段，决定中应提供 `approved_labels=[{criterion_id, score_hint}]`；系统只把这些明确展示并确认的细则写入 `reviewed_normalized_labels`，同一评语中未展示的定性、空分或其他细则不会连带获得学习权限。相对关系也必须通过 `approved_comparison` 单独确认，否则字段级审核不会把原始pairwise关系带入学习包。省略 `approved_labels` 仅用于兼容明确执行整条评价审核的旧流程。`repair-normalizations` 只在本地修复供应商输出的枚举或ID漂移，不会再次上传素材，也不会自动授予 `learning_permission`。

## 3. 统一HumanSignal

Schema：`schemas/human-signal.schema.json`。来源包括：`human_dataset`、`independent_human_eval`、`evaluation_feedback`和`human_confirmation`。

必须用 `review_context = blind | ai_assisted | not_applicable | unknown` 记录人工是否看过AI结果，区分独立判断与事后纠错造成的偏差；缺失时只能记为`unknown`，不能猜测。

## 4. 单条覆盖与模型学习分离

人工反馈可以立即把当前样本标记为 `human_override`，但：

- 原AI输出保留。
- 历史结果不被覆盖。
- 模型和阈值不立即改变。
- 学习在异步批次中创建Challenger。

## 5. 维度提案

当评价无法映射现有维度时，Normalizer输出 `unmapped`；只能部分覆盖时输出 `partial`。建议提案包含名称、父维度、定义、适用模式、所需证据、正反例和候选Judge。

提案进入：

```text
proposed → draft → shadow → active
```

LLM可以草拟，只有标准负责人可以激活。

## 6. 数据分区

- Train：Judge学习。
- Calibration：阈值、置信度和融合校准。
- Holdout：发布验证，禁止训练、Few-shot检索和Prompt调试。
- Unassigned：尚未确认用途。

分区按Feed、人物、Prompt家族和场景分组，避免相似生成泄漏到训练与Holdout。

分区会持久回HumanSignal，并导出主JSONL及`.train/.calibration/.holdout.jsonl`。每条学习包声明`route_kind`和`training_eligible`；Holdout包固定为false，`LearningRouter.route()`对Holdout会直接报错。`build-challenger`对MLLM生成Judge可直接读取的Train-only few-shot校准锚点，对CV调用`TrainerPlugin.train`，对Fusion生成标定集；所有路线先登记为Shadow，不会热更新Champion。

## 7. 路由

- 可定位身份、姿态、实例、画质问题：CV学习通道。
- 目标完成、物理、语义、整体观感：MLLM学习通道。
- 延迟、FPS、费用、成功率阈值：Fusion/规则通道。
- 模糊主观评价：先进入MLLM案例候选，不直接训练专项CV。

## 8. 审计

每次归一化、修订、分区、训练使用和发布都记录操作者、时间、版本和来源signal ID。删除人工原文需要单独的数据治理流程，不能由普通评测操作触发。
