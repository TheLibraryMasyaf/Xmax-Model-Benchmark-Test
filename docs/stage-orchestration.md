# 可拆分阶段与产物交接

本文只定义流水线阶段、标准输入输出、独立运行、历史结果导入和依赖解析。各阶段内部算法由对应组件文档负责。

## 1. 核心原则

阶段解耦指“只依赖已版本化的输入合同”，不是“没有输入依赖”。下一阶段不读取上一进程的内存对象，只通过本地事实库、Artifact Store和Stage Manifest中的ID/哈希/URI交接。

飞书是可选来源和投影终点，不是阶段间唯一传输通道。生成、评测和同步不得彼此隐式触发。

## 2. 标准阶段

| 阶段 | 必要输入 | 标准输出 |
| --- | --- | --- |
| `ingest` | Sheet/Base/Wiki/本地/HTTP素材，或已有结果 | Asset Batch；导入结果时还产生TestPlan/Run Batch |
| `plan` | Asset Batch、Benchmark、Scenario Pack、Recipe Pack | 冻结TestPlan + Task Batch |
| `generate` | TestPlan | Generation Run Batch |
| `preprocess` | completed Generation Run Batch | Preprocess Batch |
| `evaluate` | TestCase、completed Run、Preprocess Batch、Benchmark、Judge Registry | Evaluation Batch |
| `feedback` | 人工原文或结构化评价 | Human Signal Batch |
| `report` | Evaluation Batch、人工修订、比较配置 | Report Bundle |
| `sync` | 本次生成或显式选中的Run Batch Manifest | Sync Batch/Receipt |
| `reconcile` | Sync Batch/Receipt和本地事实 | Reconcile Manifest |

默认完整流程的依赖顺序是`ingest → plan → generate → preprocess → evaluate → report → sync → reconcile`。`feedback`是可独立并行的阶段，不强制阻塞自动评测。

依赖顺序不等于整批屏障。完整Run默认`execution_mode=streaming`：生成一条completed Run后就以已持久化的Run合同交给预处理，预处理完一条就交给评测。三个Service仍相互独立，由`pipeline/streaming.py`通过有界队列调度。

`stages`在合同上是授权集合，Orchestrator按上述DAG进行稳定拓扑排序，不依赖JSON数组的书写顺序。`feedback`可与自动线并行；其他阶段只在存在真实依赖边时排序。

## 3. Stage Manifest

每次阶段执行都必须写`schemas/stage-manifest.schema.json`，至少保存：阶段Run ID、输入引用、输出引用、输入哈希、配置哈希、生产者版本、状态、Attempt和时间。每个输出批次另写`schemas/batch-manifest.schema.json`，列出冻结成员、上游批次和内容哈希。

标准输出引用实体是：`asset_batch`、`test_plan`、`task_batch`、`run_batch`、`preprocess_batch`、`evaluation_batch`、`human_signal_batch`、`report_bundle`和`sync_batch`。`ingest`的外部输入快照使用`source_snapshot`。Stage Manifest只保存引用和哈希，不把大文件嵌入JSON。

`sync`必须从Run Batch Manifest的`item_ids`读取精确Run集合，不能仅按可复用的`run_batch_id`查询整库，否则会把同一计划的旧失败Attempt混入当前同步。`reconcile`只对Sync Batch列出的Run做回读核对；共享Case表中与本批无关的历史行不计为当前批次的orphan。

流水线运行时，单条Run、Preprocess和Evaluation结果在完成时立即持久化；对应Batch Manifest在整个计划排空后封口，用于最终报告、同步和隔日续跑。Stage Manifest的metadata保存`execution_mode`、队列大小、各阶段完成数以及是否实际观测到与生成重叠。

## 4. 独立运行与依赖解析

Run Request的`stages`只表示本次授权执行的阶段。系统不得为了补输入而静默增加阶段，尤其不得静默调用付费生成。

1. 用`stage_inputs`中的Selector解析已有输入。
2. Selector可按稳定ID、已冻结Manifest或过滤条件选择。
3. 过滤条件在执行前必须解析为`resolved_entity_ids + snapshot_hash`，之后不随远程数据漂移。
4. 输入已存在且合同、哈希和版本有效时直接使用。
5. 输入缺失时按`missing_input_policy=error`停止，返回缺少的实体和建议命令，不自行扩大范围。

同一Run Request内，已显式列出的所有前置阶段产物进入本次请求的已冻结实体池，下游按Stage Manifest引用；`stage_inputs`只需声明来自历史批次或外部Manifest的输入。`explicit_only`限制的是“能执行哪些阶段”，不是禁止同一请求内的标准数据交接。

例如只运行`evaluate`时，必须已选到completed Run Batch及与输入/预处理版本哈希匹配的Preprocess Batch。缺少预处理时先显式运行`preprocess`。若只有飞书Case，先显式运行`ingest results`导入；评测命令本身不下载飞书附件、不创建Run、不调用XMAX。

## 5. 已有结果导入

`config/existing-results.json`遵循`schemas/existing-results.schema.json`。导入阶段必须：

- 冻结飞书record ID/本地文件列表、附件token和快照哈希。
- 下载并校验结果视频、Feed和Prompt输入。
- 建立Asset、TestCase和`status=completed`的GenerationRun。
- 将Run的`origin`设为`feishu_import`、`local_import`或`stage_manifest_import`，保存来源定位和哈希。
- 解析不出生成模式、配方、被编辑视频或音轨来源时报错，不猜测。
- 导入本身永不写远端。

导入的Run与本项目生成的Run使用相同评测合同；缺失的运行指标标记为不可用，不从视频猜测延迟/FPS/网络事实。

## 6. 同步策略

Run Request必须显式声明`sync_policy`：

- `none`：不产生任何远端写入。
- `score_only`：只更新已有Case的评分和评测说明，禁止创建新Case和上传附件。
- `metadata_only`：只写允许的文本/数值字段。
- `attachments_only`：只补已定位记录的附件。
- `full`：按飞书投影合同执行完整upsert和附件同步。

`sync_policy != none`不代表授权执行`sync`阶段；只有`stages`显式包含`sync`时才写远端。反之，包含`sync`时不允许`sync_policy=none`。

## 7. 标准场景

```text
只生成：plan + generate，输出Run Batch，sync_policy=none
隔日只评测：preprocess + evaluate + 已有run_batch Selector，sync_policy=none；已有匹配Preprocess Batch时可只列evaluate
历史Case评测：ingest results → preprocess → evaluate，不调用XMAX
只回写分数：sync + evaluation_batch Selector，sync_policy=score_only
完整流程：按默认全部阶段显式列出，sync_policy=full
```

`--resume`只续跑同一阶段中输入哈希和配置哈希未变的尝试。生成阶段还使用稳定的`generation_signature`识别已经完成的Feed、Prompt、配方、模式与生成配置组合；计划ID、Case显示后缀、素材批次血缘和无关素材变化不属于生成身份，不能触发重复付费。用新Benchmark/Judge重评旧Run必须创建新Evaluation Batch，不覆盖旧评测。

流水线中断后，已持久化的completed Run和Preprocess会被幂等复用；同一计划、Benchmark、Judge Pack和Preprocessor版本生成稳定的`evaluation_batch_id`，已存在的Run评测不会重复调用MLLM。生成失败的Run不进入下游，但仍保留在Run Batch并按失败Run规则记录0%。

需要“任务分配器反复调用单条完整流程”时，使用Task Batch和Worker，详见[Task Worker](task-execution.md)。原有`generate --plan-id`和统一Run入口仍保留整批兼容性。

可直接复制的请求模板：`config/run-generate-only.example.json`、`config/run-evaluate-only.example.json`、`config/run-import-evaluate-only.example.json`和`config/run-sync-scores-only.example.json`。
