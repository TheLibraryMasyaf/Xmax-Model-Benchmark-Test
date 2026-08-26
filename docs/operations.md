# 运行、安全与故障处理

本文只说明运行门槛、密钥、续跑、审计和故障分类。

建成后的标准命令只以根目录 [RUNBOOK](../RUNBOOK.md) 为准。每轮真实操作先运行 `xmax-test context-check --request config/run-request.json`；该检查必须一次性列出全部缺项，不允许操作者靠逐阶段报错猜流程。

## 1. 运行阶段

```text
resolve selectors → freeze stage inputs → dry-run/smoke → approved if billed
→ execute requested stages → write manifests → optional sync/reconcile → complete
```

- Dry-run不上传、不生成、不写飞书。
- Smoke只跑最小可验证组合。
- Approved明确记录计划哈希、任务数和预计成本。
- Reconcile核对生成、评测、附件和飞书记录。
- Run Request只授权`stages`列出的阶段；不得因依赖缺失静默增加阶段。

## 2. 付费操作

批量执行前必须展示：组合数、离线/实时任务数、重复次数、质量/FPS、预计积分范围、并发、预计耗时、不可用素材和跳过原因。计划改变后原批准失效。

任务执行过程中报告：已完成/总数、成功/失败、已消耗积分、当前阶段和剩余项。旧测试结果不能充当本轮交付。

## 3. 密钥

- XMAX Key只从环境变量或本机秘密文件读取。
- 浏览器端使用临时API Key，不暴露长期Key。
- 日志不得保存Key、完整鉴权Header或敏感参考URL。
- 飞书凭据不进入示例配置和产物目录。
- `.env`、真实config和Key文件已加入`.gitignore`。

## 4. 续跑

所有长任务基于数据库状态和不可变事件续跑：

- 下载按素材哈希。
- 生成按Run和外部task/session ID。
- 评测按evaluation + judge version。
- 飞书按Sync Ledger。
- 重试追加Attempt，不覆盖失败记录。
- 每次Attempt使用独立Case编号后缀；生成失败仍同步Case表并写0%，后续重试使用新后缀。
- 阶段按`input_hash + config_hash + producer_version`续跑；哈希变化创建新Stage Run。
- 新Benchmark/Judge对旧Run的评测创建新Evaluation Batch，不重新生成也不覆盖旧评测。

## 5. 超时与重试

每个外部边界独立配置超时：下载、上传、Session创建、RTC进房、start、heartbeat、模型完成、结果下载、Codex、飞书。

只有幂等操作自动重试；可能重复计费的提交必须先查询外部任务状态。重试使用退避和最大次数。

## 6. 监控

至少输出：

- 各阶段吞吐和积压。
- 外部API错误码分布。
- 生成成功率与费用。
- Codex Schema失败和重试。
- Judge耗时、GPU失败和覆盖率。
- 飞书同步错误与对账差异。
- Challenger与Champion差异。

## 7. 完成判定

完成条件只适用于Run Request显式授权的阶段。不得因为本次未列`evaluate`、`report`或`sync`，就把“只生成”任务判为未完成。

所有请求共同满足：

1. Selector已冻结，每个已执行阶段都有通过Schema的Stage Manifest和Batch Manifest。
2. 输入哈希、配置哈希、生产者版本、费用和错误已汇总。
3. 请求范围外的Adapter没有外部副作用。

按已授权阶段增加条件：

- `ingest`：来源快照完整，素材/导入Run已校验，失败项有明确原因；导入没有写远端。
- `plan`：TestPlan冻结且有内容哈希。
- `generate`：所有Case有终态Run或明确跳过原因，Completed Run媒体通过验证。
- `preprocess`：所有选中completed Run有匹配版本/哈希的预处理产物或明确失败。
- `evaluate`：所有适用Active维度有Judgment或明确不可评估状态，Scenario Score保存命中规则和版本轨迹；未命中核心场景时明确无法出分。
- `feedback`：原文、归一化版本、分区和学习许可已保存。
- `report`：P0/P1/P2 Markdown和JSON已生成并通过Schema。
- `sync`：实际写入不超出`sync_policy`，写入和回读对账通过；Case百分比转换正确。
- `reconcile`：所有差异被标记为已修复、冲突或需人工处理。

## 8. 删除与回滚

默认不物理删除Run、Judgment、人工原文和已使用维度。清理大文件时先验证有副本和引用关系。Judge、Benchmark和Score Schema发布均保留上一个Champion并支持回滚。
