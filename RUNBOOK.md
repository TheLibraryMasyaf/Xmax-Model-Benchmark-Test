# XMAX Test Runbook

本文定义项目全部实现完成后的唯一操作流程和CLI合同。目前实现状态以 `IMPLEMENTATION.md` 为准；未完成命令不得被声称可用。

## 1. 操作者只需提供

```text
BENCHMARK.md
config/scenarios.json
config/operation-recipes.json
config/asset-sources.json
config/project.json
config/judges.json
config/feishu.json（需要飞书时）
config/existing-results.json（导入已有结果时）
config/run-request.json
.env中的密钥（真实外部运行时）
```

各文件从同名 `.example.json`复制。系统不得要求操作者再解释模块顺序或字段含义。

单一用途可直接复制`config/run-generate-only.example.json`、`config/run-evaluate-only.example.json`、`config/run-import-evaluate-only.example.json`或`config/run-sync-scores-only.example.json`。

## 2. 一次性安装

```bash
cd xmax-test
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/xmax-test project-check
.venv/bin/xmax-test db migrate
.venv/bin/xmax-test db check
```

真实离线REST/COS运行另执行 `.venv/bin/pip install -e '.[production]'`。实时Harness在`realtime-harness/`执行`npm ci`、`npm run check`。`ffmpeg/ffprobe`、CV插件和其他额外依赖由 `judges check` / `context-check`一次性列出。

## 3. 上下文检查

目标CLI：

```bash
.venv/bin/xmax-test context-check --request config/run-request.json
```

必须一次性输出：缺失文件、空Benchmark、无效场景标签、未覆盖维度、无效Operation Recipe/模式/互动Profile、缺失Judge、缺失Key、飞书映射、预计需要的外部权限。检查不产生外部副作用。`run`命令内部会再强制执行同一检查，Agent不能通过少跑一条命令绕过。

## 4. 标准运行

```bash
# 只预览，不下载、不生成、不写飞书
.venv/bin/xmax-test run --request config/run-request.json --dry-run

# 最小冒烟；若涉及计费，仍需批准预算
.venv/bin/xmax-test run --request config/run-request.json --smoke-limit 1 --budget-approved

# 正式执行；系统应先打印冻结计划和预算并请求放行
.venv/bin/xmax-test run --request config/run-request.json --budget-approved

# 中断后原命令续跑
.venv/bin/xmax-test run --request config/run-request.json --resume --budget-approved
```

CLI不会在无人值守运行中弹出交互问答；只有在看过`plan preview`/统一Run dry-run的任务数和积分范围后，操作者才能传`--budget-approved`。计划哈希改变后必须重新预览。

统一Run命令依次执行请求中显式列出的stages；已完成且输入/配置/生产者哈希未变的阶段自动跳过。依赖缺失时报错，不静默补跑未列出阶段。

当Run Request同时列出`generate + preprocess + evaluate`且`execution_mode=streaming`（默认）时，统一Run采用有界流水线：每条completed Run立即进入预处理，每条completed Preprocess立即进入评测。若请求还显式列出`sync`且`sync_policy != none`，每条完整EvaluationResult会立即写入飞书，最后的`sync/reconcile`阶段再做幂等补偿和回读对账。`pipeline_queue_size`默认4，队列满后对上游反压。所有条目结束后才封口批次Manifest和总结报告。

生成前会实际导入COS SDK的`CosConfig/CosS3Client`并校验STS响应，检查失败时不创建GenerationRun。统一Run、独立离线生成和TaskWorker遵循同一规则；批量Worker必须在领取第一条任务前完成共享预检。流水线对不可重试错误立即熔断，对完全相同的生成异常默认连续3次后熔断；可用`circuit_breaker_threshold`调整，不得为了“跑完”而关闭。

显式设`execution_mode=batch`可恢复“整批生成完再预处理/评测”。只列出单阶段时，无论该字段为何都不会暗中执行下游。

小批次可在`filters`中使用`feed_limit`、`prompt_limit`、`feed_asset_ids`或`prompt_record_numbers`。Feed和Prompt先按飞书业务编号、再按稳定ID排序后截取；过滤条件属于计划哈希，不能复用到全量计划。仓库提供`config/run-smoke-5x5.example.json`作为前5个Feed × 前5个Prompt、每组合1次的安全模板。它默认`dry_run=true`；复制为本轮请求、完成`context-check`和预算确认后才能改为false。

统一Run的`--dry-run`会为每个阶段产生仅供本次验证传递的占位引用，不创建业务Run、不调用Judge、不写飞书；因此可以完整验证阶段合同而不会在`preprocess/evaluate`处因缺少真实批次中断。

## 5. 分阶段命令

```bash
.venv/bin/xmax-test ingest assets discover --config config/asset-sources.json
.venv/bin/xmax-test ingest assets sync --config config/asset-sources.json
.venv/bin/xmax-test ingest assets verify --batch-id <asset_batch_id>
.venv/bin/xmax-test ingest results --config config/existing-results.json

.venv/bin/xmax-test plan preview --request config/run-request.json
.venv/bin/xmax-test plan build --request config/run-request.json
.venv/bin/xmax-test plan show --plan-id <plan_id>

.venv/bin/xmax-test worker status --task-batch-id <task_batch_id>
.venv/bin/xmax-test task show --task-id <task_id>
.venv/bin/xmax-test task run --task-id <task_id> --lease-owner <agent-id> --budget-approved
.venv/bin/xmax-test worker run --task-batch-id <task_batch_id> --lease-owner <agent-id> --budget-approved

.venv/bin/xmax-test generate offline --plan-id <plan_id> --resume --budget-approved
.venv/bin/xmax-test generate realtime --plan-id <plan_id> --resume --budget-approved

.venv/bin/xmax-test preprocess --run-batch-id <run_batch_id> --resume
.venv/bin/xmax-test evaluate --run-batch-id <run_batch_id> --resume

.venv/bin/xmax-test human import --input <human-file.json>
.venv/bin/xmax-test human feedback --input <feedback-file.json>
.venv/bin/xmax-test human normalize --pending
.venv/bin/xmax-test human partition --output var/feedback/learning.jsonl

.venv/bin/xmax-test report model-update --request config/run-request.json

.venv/bin/xmax-test sync dry-run --selector <selector.json> --policy <policy>
.venv/bin/xmax-test sync run --evaluation-batch-id <evaluation_batch_id> --policy <policy> --resume
.venv/bin/xmax-test reconcile --sync-batch-id <sync_batch_id>

.venv/bin/xmax-test judges list
.venv/bin/xmax-test judges check
.venv/bin/xmax-test judges run --judge-id <id> --version <version> --context <context.json>
.venv/bin/xmax-test replay run --run-batch-id <run_batch_id>
.venv/bin/xmax-test release validate --holdout <holdout.json> --threshold 0.5
.venv/bin/xmax-test release promote --validation <validation.json> --operator <name>
.venv/bin/xmax-test release rollback --previous-version <version> --operator <name>

.venv/bin/xmax-test stage show --stage-run-id <stage_run_id>
.venv/bin/xmax-test batch show --batch-id <batch_id>
```

分阶段命令用于独立交付、隔日续流和排障；完整流程优先使用统一Run命令。

Plan Build会返回`task_batch_id`。需要边生成边评测并让调度器反复消费单条任务时，优先使用`worker run`。默认每条任务运行到飞书同步和对账；仅本地执行时显式传`--sync-policy none --no-reconcile`。详细合同见`docs/task-execution.md`。

## 6. 独立阶段范式

只生成视频：Run Request列出`ingest, plan, generate`或直接对已有`plan_id`运行`generate`，并设`sync_policy=none`。命令输出`run_batch_id`和Stage Manifest，不评测、不写飞书。

隔日只评测：

```bash
.venv/bin/xmax-test preprocess --run-batch-id <yesterday_run_batch_id> --resume
.venv/bin/xmax-test evaluate --run-batch-id <yesterday_run_batch_id> --resume
```

评测组合只读取completed Run、匹配的Preprocess Batch和已版本化评测配置，不调用XMAX、不写飞书。如果已存在同输入/预处理版本哈希的Preprocess Batch，可省略第一条命令。

只评测飞书已有Case：

```bash
.venv/bin/xmax-test ingest results --config config/existing-results.json
.venv/bin/xmax-test preprocess --run-batch-id <imported_run_batch_id>
.venv/bin/xmax-test evaluate --run-batch-id <imported_run_batch_id>
```

第一步冻结远端记录、下载并校验视频，创建`origin=feishu_import`的completed Run；导入本身不写飞书。

只把既有评测分数回写原Case：

```bash
.venv/bin/xmax-test sync run --evaluation-batch-id <evaluation_batch_id> --policy score_only
```

`score_only`禁止新建Case、上传附件或改其他业务字段。完全不需要远端写入时，Run Request不列`sync`并设`sync_policy=none`。

`--evaluation-batch-id`是评分写回的强制选择边界：同步器只写该批次明确包含的EvaluationResult，不会从同一Run的其他历史评测中自动取“最新”或“最高”分。若只用`--run-batch-id`做`full`同步，则按“生成完成、尚未评测”处理，完成项评分和说明保持空值。

人工信号可以与自动线并行导入。`human partition`除了主JSONL，还会生成`.train.jsonl`、`.calibration.jsonl`和`.holdout.jsonl`；Holdout包的`training_eligible=false`，训练路由代码禁止读取。外部CV/MLLM/Fusion Challenger只消费这些版本化包，不直接改当前Champion。

## 7. 成功输出

统一Run只对本次`stages`中的相应阶段必须生成下列输出；未授权阶段不得被当作缺失交付：

```text
计划组合数与实际Run数
离线/实时成功失败数
总积分、耗时和重试
Benchmark与Judge版本
Canonical与Scenario结果
每次Case百分比评分；失败Run为0%，未重评为空
重复组平均百分比与中位数/最小/最大/标准差/P25/P75/成功率
未覆盖/不可评维度
人工override与未处理信号
飞书新增、更新、跳过、冲突数
模型版本更新报告的Markdown与JSON绝对路径
全部本地产物绝对路径
每个已执行阶段的stage_run_id、输入/输出批次ID和Manifest绝对路径
```

只有 `docs/operations.md` 的完成条件全部满足，退出码才为0。

## 8. 不需要操作者判断的内容

系统在已授权阶段内自动完成：DAG顺序、适用维度、Judge路由、场景权重、不可评维度归一化、重试、续跑、对账和版本记录。只有已授权`sync`时才执行飞书upsert。

模式自动规则：Case/Run Request显式选择优先；否则互动玩法默认实时，其他默认离线。每个Feed × Prompt默认重复5次，但项目、本轮请求或单Case均可覆盖。执行`sync_policy=full`时每个Run独立写入飞书Case表，报告聚合不回填单行。

系统只能要求操作者决定：付费预算批准、提供缺失外部凭据、确认删除/迁移正式数据、提供仍未确定的Benchmark或场景包。

系统不会自动决定扩大阶段范围。单阶段输入缺失时，必须返回缺失的批次类型和建议命令，由操作者或上层Agent发起新请求。

组合分配可用`cartesian`、`random_pairs`、`random_runs`或`explicit_pairs`，示例见`config/run-task-allocation.example.json`。`repeat_count`默认5但可修改；`random_runs.target_run_count`表示最终任务总数，不再额外乘`repeat_count`。

## 9. 模型版本更新报告

当Run Request包含`report` stage时，系统必须使用 `report-templates/model-version-update-report.md`，针对`comparison.requested_scene_ids`逐场景生成报告。报告必须包含：P0总分变化与明显改进、P1持平项、P2劣化项，以及可比性、覆盖率、证据、人工修订和发布建议。

目标输出：

```text
var/reports/model-version-updates/<comparison_id>.md
var/reports/model-version-updates/<comparison_id>.json
```

## 10. 单版本/单批次评测报告

当用户只要求报告一个模型版本或某次指定测试批次时，读取`docs/single-version-reporting.md`，使用`report-templates/single-version-evaluation-report.md`。报告必须包含总分分布、全量适用维度、细则级得分或缺失说明、强项、短板、Good/Bad Case和P0/P1/P2改进建议。

当前没有独立的单版本报告CLI；执行Agent从已有产物填写模板，写入：

```text
var/reports/single-version/<report_id>.md
```

不得为了填写细则表而从维度分反推细则分。新批次必须从EvaluationResult.`criterion_results`和Evaluation Batch Manifest.`metadata.aggregate.criterion_summary`生成细则表；当前批次是旧口径且未产出细则级Judgment时，必须明确写“旧口径，需Replay”。
