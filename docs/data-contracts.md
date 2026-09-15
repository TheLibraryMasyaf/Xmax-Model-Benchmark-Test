# 数据合同

本文只定义跨模块使用的数据对象、ID、状态和版本。具体业务操作由其他文档说明。

## 1. ID规则

| ID | 含义 | 是否稳定 |
| --- | --- | --- |
| `asset_id` | 一个内容确定的素材版本 | 内容变化时新建 |
| `case_id` | Feed、Prompt、配方、模式、配置与重复序号的内部稳定ID | 组合不变则稳定 |
| `task_id` | 某冻结计划中一个Case的可租约执行单元 | Plan Hash和Case不变则稳定 |
| `case_number` | 飞书Case编号，如`feed002_prompt037_03` | TestPlan冻结后稳定 |
| `run_id` | 一次实际生成尝试 | 每次执行新建 |
| `evaluation_id` | 某组Judge对某次Run的评测 | 每次评测新建 |
| `signal_id` | 一条原始人工信号 | 永不复用 |
| `dimension_id` | 评测维度的稳定语义ID | 改名不变，重大拆分新建 |
| `judge_id` | Judge能力身份 | 实现升级不变 |
| `stage_run_id` | 一次阶段执行 | 每次Attempt新建 |
| `*_batch_id` | 某阶段产出的稳定批次 | 批次内容不变则稳定 |

推荐 ID由业务键规范化后计算哈希或使用 UUIDv7。禁止用飞书行号、文件名或数组下标作为唯一身份。

## 2. 核心对象

### 2.1 Asset

至少包含：`asset_id`、`kind`、`uri`、`sha256`、`bytes`、`mime_type`、`source`、`status`、`metadata`。

`kind`示例：`feed_video`、`prompt_text`、`prompt_image`、`prompt_video`、`mask_video`、`result_video`、`review_sheet`、`event_clip`。

### 2.2 TestPlan/TestCase

Schema：`schemas/test-plan.schema.json`。TestPlan是版本化集合；TestCase描述一个确定组合，不代表生成已经执行。

TestCase中的`scenario_id`引用Scenario Pack；`scene_tags`固定为`{tag_name: tag_value}`对象，不是自由文本数组。标签值必须来自该Pack的`tag_definitions`。

每个TestCase还必须保存：`feed_number`、`prompt_number`、`case_number`、Operation Recipe ID/版本、`edited_video_asset_id`、`expected_audio_source_asset_id`和最终API素材绑定。内部`case_id`用于稳定关联，外部`case_number`用于飞书和本地Case目录，两者不得互相替代。

### 2.2.1 TestTask

Schema：`schemas/test-task.schema.json`。Task内嵌冻结TestCase快照，并保存`task_batch_id`、分配顺序、租约、Attempt、状态和下游`result_refs`。Task是调度单元，Case是业务输入单元，Run是真实生成Attempt，三者不得混用。

```text
pending → leased → generating → preprocessing → evaluating → syncing → completed
                    └─ 任意基础设施异常 → error
                    └─ 付费闸门或批次级评测基础设施暂停 → evaluation_paused
```

模型正常返回生成失败Run时，Task仍标记`completed`且`outcome=generation_error`，并按0%同步；但实时网络准入失败是环境无效，不是模型失败，其Run为`cancelled`且不同步、不评分。`error`只表示流程基础设施未完成，可用`--resume`重试。`evaluation_paused`不是失败，也不持有租约；它保存已有Run/Preprocess引用。付费闸门暂停需人工重新授权；评测基础设施暂停在修复网络、认证或错误分类后显式续跑。

### 2.3 GenerationRun

Schema：`schemas/generation-run.schema.json`。每次尝试均追加Run；重试不能覆盖原Run。每个Run绑定一个`run_batch_id`和Case编号；失败Run同样保留并投影为0% Case记录。

`origin`必须是`xmax_offline`、`decart_offline`、`xmax_realtime`、`feishu_import`、`local_import`或`stage_manifest_import`。TestCase的`generation_provider`和`model_id`是生成身份与`generation_signature`的一部分，XMAX与Lucy结果不得互相续跑复用。所有Run都要保存`provenance`；导入Run不伪造Provider task/session/延迟/FPS事实，缺失指标显式标为不可用。

共同状态：

```text
planned → running → completed
                  ↘ error
                  ↘ cancelled
```

适配器可以保存更细的内部状态，但对外必须映射到共同状态。

### 2.4 Judgment

Schema：`schemas/judgment.schema.json`。每条只对应一个维度、一个Judge版本和一个Run，并必须保存非空`criterion_results`。每条细则结果含`criterion_id`、0/1/2或`null`、`assessable`、置信度和证据。Judge的顶层维度分只用于审计，不进入最终融合。

### 2.5 EvaluationResult

Schema：`schemas/evaluation-result.schema.json`。必须保存融合后的`criterion_results`与由它们确定性计算的`dimension_results`；维度分不得反向填充细则分。对同一细则的多Judge结果保存Judge列表、版本、证据和`judge_score_count`。当前Benchmark只按预设核心场景规则计算`scenario_score`，`canonical_score`保留为`null`兼容字段，并记录实现基底、命中规则、有评测能力的维度/细则集合、最终有效权重和硬门槛结果。未命中核心场景权重规则时`score_readiness=missing_scene_weight_rule`且不出总分。`case_score_percent`取Score Schema声明的`case_score_output`，范围0–100，用于单次Case的飞书展示。投影到飞书百分比字段时转为0–1（写入除100，读回乘100），内部对象始终保持0–100。

Evaluation Batch Manifest的`metadata.aggregate`保存逐细则、逐维度、总分统计和P.2/P.3/P.4/RP.1/RP.2/RP.3报告指标。六项批次指标不属于Judgment，不回填单视频。历史Evaluation没有`criterion_results`时只能标记为旧口径/缺失，必须用新Benchmark重评后才能进入细则汇总。

生成失败、结果无效或阻断型Hard Gate产生`case_score_percent=0`；尚未使用新Benchmark重评的历史Case使用`null`。空值与0分不可互换。

### 2.6 HumanSignal

Schema：`schemas/human-signal.schema.json`。同时保存 `raw_text` 与 `normalized_labels`；后者可以重跑生成，前者不可修改。

### 2.7 JudgeManifest

Schema：`schemas/judge-manifest.schema.json`。声明能力，不包含密钥或可执行任意代码的用户输入。

### 2.8 StageManifest与Selector

`schemas/stage-manifest.schema.json`是阶段执行的Manifest合同；`schemas/batch-manifest.schema.json`定义每个输出批次的成员、内容哈希、生产Stage和上游批次；`schemas/pipeline-selector.schema.json`是独立运行时选取已有批次的合同。

Selector在配置中为`state=request`；动态过滤执行前解析为`state=frozen`的稳定ID集、快照时间和快照哈希。StageManifest引用输入/输出批次，同时保存输入哈希、配置哈希和生产者版本。任何Agent可用Stage + Batch Manifest在新进程里继续下游，不需要上游会话上下文。

### 2.9 ExistingResultImport

`schemas/existing-results.schema.json`定义飞书Case、本地目录或旧Manifest的导入。导入必须产生正常Asset、TestCase和completed GenerationRun，因此下游评测不需要来源特判。

## 3. 统一产物目录

```text
var/artifacts/
├── assets/<asset_id>/
├── plans/<plan_id>/
├── runs/<run_id>/
│   ├── run.json
│   ├── events.jsonl
│   ├── input/
│   ├── output/
│   └── logs/
├── preprocessing/<preprocess_id>/
├── evaluations/<evaluation_id>/
│   ├── judgments.jsonl
│   └── raw/
├── human-signals/<signal_id>/
└── releases/<judge_id>/<version>/

var/manifests/
├── stages/<stage_run_id>.json
├── selectors/<selector_id>.json
└── batches/<entity_type>/<batch_id>.json
```

数据库只保存URI、哈希、状态和索引；大文件不放入数据库字段。

飞书只投影三张业务表：Feed数据、Prompt数据、Case数据。每个GenerationRun对应Case数据的一行；重复组平均、中位数和其他统计只存在于版本报告产物中。

## 4. 版本字段

每条正式结果至少绑定：

```text
benchmark_version
scenario_pack_version
dimension_version
score_schema_version
weight_profile_id + weight_profile_version
scene_weight_rule_versions
judge_id + judge_version
preprocessor_version
model_id
generation_config_hash
```

缺少其中任一关键版本时，该结果只能作为实验数据，不能进入跨版本排行榜。

## 5. 原始与派生数据

不可变原始数据：

- 下载到的原素材及哈希。
- 实时触控实际使用的Feed截图、抽帧时间戳、原Feed哈希和抽帧策略版本。
- XMAX请求、响应和RTC事件。
- Codex原始stdout/stderr。
- CV原始指标。
- 人工原文。

可重建派生数据：

- 联系图、ROI、抽帧。
- 标准化Judgment。
- 总分、排行和飞书展示字段。
- LLM转写后的人工标签。

派生数据发生错误时应重新生成，不修改原始数据解释历史。
