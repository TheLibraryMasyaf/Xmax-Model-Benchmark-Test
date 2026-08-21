# 本地存储与迁移

本文只定义SQLite元数据、追加事件和Artifact Store的实现边界；不负责外部同步或业务判断。

## 1. 事实源

数据库路径始终以`config/project.json`的`database_path`为唯一真源：当前正式工作区使用`var/xmax-production.sqlite3`；`config/project.example.json`中的`var/xmax-sandbox.sqlite3`仅用于复制后创建隔离的演练环境，不能据此推断正式库路径。大文件位于`var/artifacts/`。数据库保存业务ID、状态、版本、哈希和URI，不保存视频Blob。飞书可由两者重建。

## 2. 目标实现

```text
src/xmax_test/storage/sqlite.py
src/xmax_test/storage/artifacts.py
src/xmax_test/storage/migrations.py
src/xmax_test/storage/migrations/0001_initial.sql
tests/storage/
```

SQLite启动时启用外键、WAL和busy timeout。迁移只前进，记录`schema_migrations(version, checksum, applied_at)`；已执行迁移内容改变必须报错。

## 3. 逻辑表

| 表 | 主键/唯一键 | 写入规则 |
| --- | --- | --- |
| `assets` | `asset_id`，内容哈希唯一 | 内容版本不可覆盖，只更新校验状态和可重建索引 |
| `test_plans` | `plan_id + plan_version`，`plan_hash`唯一 | 冻结后不可修改 |
| `test_cases` | `case_id` | 同一规范化组合幂等 |
| `test_tasks` | `task_id`，`task_batch_id + case_id`唯一 | 冻结输入不覆盖；状态、租约和结果引用只前进更新 |
| `generation_runs` | `run_id` | 每次尝试新建，终态不可回到运行态 |
| `run_events` | `run_id + sequence`，外部事件去重键可选 | 仅追加 |
| `preprocess_runs` | `preprocess_id`，输入+配置+版本哈希唯一 | 可重建但历史记录保留 |
| `judgments` | `evaluation_id + dimension_id + judge_id + judge_version` | 仅追加，新评测使用新evaluation |
| `evaluation_results` | `evaluation_id` | 保存完整版本和权重轨迹 |
| `human_signals` | `signal_id` | 原文仅追加，归一化结果另存版本 |
| `dimension_proposals` | `proposal_id` | 显式状态机 |
| `judge_releases` | `judge_id + version` | Champion切换记录事件，不覆盖验证报告 |
| `approvals` | `approval_id`，批准哈希唯一 | 保存操作者、范围和时间 |
| `sync_ledger` | `entity_type + entity_id + destination` | 幂等upsert和Attempt历史 |
| `stage_runs` | `stage_run_id` | 每次阶段Attempt追加，保存输入/配置哈希 |
| `batch_manifests` | `entity_type + batch_id` | 批次内容冻结，不原地修改 |
| `selector_snapshots` | `selector_id + snapshot_hash` | 保存解析后稳定ID集 |
| `result_imports` | `import_request_id + source_hash` | 远端/本地已有结果幂等导入 |
| `evaluation_budgets` | `budget_id` | 保存本地付费上限、已用/预留金额和人工授权周期；暂停跨进程持久化 |
| `evaluation_budget_reservations` | `reservation_id` | 付费请求前预留，成功结算、明确拒绝释放、超时保守核销 |
| `evaluation_budget_events` | `event_id` | 授权、预留、结算、释放、暂停仅追加审计 |

JSON负载可以作为版本化列保存，但常用关联键、状态、时间和哈希必须单独建列与索引。Schema演进不能要求物理新增每个Benchmark维度列。

## 4. 事务边界

- 外部调用不放在长SQLite事务中。
- 提交前先创建Attempt/Run和`planned`事件，外部调用后追加响应事件，再短事务更新派生当前状态。
- Artifact先写同目录临时文件、校验哈希后原子rename，最后事务写URI；失败临时文件进入可清理清单。
- 数据库提交成功但外部调用结果未知时标记`reconcile_required`，不得盲目重试提交。

## 5. Repository合同

Repository公开显式业务方法，不让Service拼SQL。所有列表方法必须稳定排序并支持游标；写方法接收`expected_version`防止并发覆盖；不存在、冲突和重复分别返回稳定错误码。

读取历史事件可重建当前状态；缓存的当前状态只用于查询性能。测试必须证明删除可重建投影后可以从事件恢复。

Stage Repository必须支持按`stage_run_id`、`batch_id`、输入哈希和配置哈希定位产物。单阶段执行不得依赖上一进程内存状态或临时目录名。

## 6. Artifact URI

本地URI统一为`artifact://<namespace>/<relative-path>`，通过ArtifactStore解析。禁止把本机绝对路径写入跨机器合同；CLI最终输出可以额外显示解析后的绝对路径。

目录和文件名只使用稳定ID与受控后缀，原始上传文件名仅作为metadata。写入前检查目标仍在Artifact Root内，拒绝路径穿越。

## 7. 备份、清理与验收

正式运行前记录数据库和Artifact Root位置。清理器默认只报告未引用临时文件；物理删除必须显式批准并生成清单。

完成验收：迁移可在空库和已有库幂等执行；并发写不丢事件；中断后可续跑；Artifact损坏可由哈希发现；从本地事实可重建飞书投影；Fake端到端测试不依赖外部服务。
