# Task Batch 与单条执行器

本文只说明测试任务交接、租约和单条完整流程；组合如何被选中见`docs/test-planning.md`，各阶段算法见对应组件文档。

## 1. 责任分界

```text
任务分配（Plan Builder + StrategyRegistry）
  → 冻结 TestPlan
  → 按Case产生 Task Batch
  → Worker原子领取一条Task
  → 生成 → 预处理 → CV/MLLM评测 → 飞书同步 → 回读对账
  → 领取下一条，直到批次排空或达到max_tasks
```

分配器只决定“测什么”；Worker只决定“如何执行这一条冻结任务”。新增分配形式无需修改Worker。

## 2. 交接合同

Plan Builder一次写入：

- `TestPlan.task_batch_id` 和 `task_ids`。
- `task_batch` Batch Manifest，成员为精确`task_id`集合。
- SQLite `test_tasks`中每个Task的内嵌Case快照。

Worker通过`BEGIN IMMEDIATE`原子租约一条`pending` Task；过期租约回收为`pending`。任务每完成一段就持久化Run、Preprocess、Evaluation或Sync引用，进程中断后不从头重做已有成果。

## 3. CLI

```bash
# 查看批次数量和状态
.venv/bin/xmax-test worker status --task-batch-id tasks-...

# 单独执行一条；默认完整同步飞书并回读
.venv/bin/xmax-test task run --task-id task-... --lease-owner agent-a --budget-approved

# 消费整批；多个Worker使用不同lease-owner可并行运行
.venv/bin/xmax-test worker run --task-batch-id tasks-... --lease-owner agent-a --budget-approved

# 只在本地生成和评测，不写飞书
.venv/bin/xmax-test worker run --task-batch-id tasks-... --lease-owner agent-a \
  --sync-policy none --no-reconcile --budget-approved

# 限制本进程最多处理10条
.venv/bin/xmax-test worker run --task-batch-id tasks-... --lease-owner agent-a \
  --max-tasks 10 --budget-approved
```

付费生成仍必须显式`--budget-approved`。`task claim`只用于外部调度器领取冻结任务；领取后必须在租约期内执行，否则会被其他Worker回收。

## 4. 失败与续跑

- 已`completed`的Task再次调用会直接返回，不重复付费。
- XMAX返回生成失败Run仍是已执行Task；写飞书0%，不无限重试。
- 网络、Judge、飞书等基础设施异常进入`error`；使用`task run ... --resume`只续跑该任务。
- 付费评测闸门关闭时进入`evaluation_paused`而不是`error`，保留`run_id/preprocess_id`且释放租约；Worker继续生成后续pending任务，但所有Judge和飞书评分同步都暂停。充值并显式开放预算后，下一次`worker run`自动把本批这些任务重新排队，只续评测和后续步骤。
- `result_refs`保存已有`run_id/preprocess_id/evaluation_id`，续跑优先复用。
