# 跨模块实现合同

本文只规定所有工作包共同遵守的代码、CLI、配置、错误和测试约定；业务流程仍由各组件文档负责。

## 1. 分层与依赖注入

每个模块固定分为：Domain Model → Service → Repository/Adapter → CLI。Service只依赖Protocol，不在内部实例化XMAX、浏览器、Codex、飞书或SQLite客户端；CLI Composition Root负责根据配置注入真实Adapter或Fake。

禁止跨层捷径：Judge不写飞书，Runner不算分，Adapter不修改Benchmark，Repository不发外部请求。

每个可运行阶段实现统一`StageExecutor`边界：输入是已冻结EntityRef/Selector快照与配置快照，输出是Stage Manifest和批次引用。StageExecutor不得直接调用其他阶段的Service来补缺失输入。

## 2. 公共实现位置

```text
src/xmax_test/config.py          # 读取、合并、校验配置；不读取业务数据
src/xmax_test/context.py         # 一次性上下文检查和缺项报告
src/xmax_test/errors.py          # 稳定错误码和异常类型
src/xmax_test/hashing.py         # canonical JSON与SHA-256
src/xmax_test/time.py            # 可注入UTC Clock
src/xmax_test/cli.py             # 唯一CLI Composition Root
src/xmax_test/storage/           # SQLite与ArtifactStore实现
src/xmax_test/pipeline/          # Selector、依赖、Stage Manifest与统一编排
```

工作包需要这些能力时扩展上述公共模块，不得各自复制配置加载、哈希、时间或错误处理。

## 3. 时间、ID与哈希

- 时间统一为UTC、RFC 3339、带`Z`，保存原始外部时间的同时保存接收时间。
- `asset_id`由内容SHA-256确定；`case_id`由规范化组合JSON哈希确定；每次真实尝试创建新`run_id`；其他事件型对象使用UUIDv7。
- JSON哈希固定使用UTF-8、键排序、紧凑分隔符，禁止把本机绝对路径、密钥和创建时间放入可复现业务键。
- 所有派生文件都有`content_sha256`、`producer_version`和输入哈希。

## 4. 配置规则

配置优先级固定为：CLI显式参数 → Run Request → project config → `.env`密钥 → 示例默认值。非密钥配置必须通过JSON Schema；未知关键字段报错，不能静默忽略拼写错误。

路径相对其配置文件所在目录解析，写入运行快照时转成绝对路径。任何日志和`--json`输出均先做密钥脱敏。

## 5. CLI合同

所有最终命令同时支持人读输出和`--json`。成功退出码为0；输入/合同错误2；缺失外部依赖3；外部服务失败4；部分完成5；需要付费批准6；内部错误10。

JSON输出统一为：

```json
{
  "ok": true,
  "command": "plan.preview",
  "status": "complete",
  "data": {},
  "warnings": [],
  "errors": [],
  "artifact_paths": []
}
```

错误项至少包含`code`、`message`、`stage`、`retryable`和可选`entity_id`。`context-check`必须聚合全部错误后一次返回。

## 6. 幂等、重试与批准

- 读取、校验、预览和dry-run不得产生外部副作用。
- 每个外部提交先用业务幂等键查询已有状态；不能确认时停止，避免重复计费。
- 自动重试只用于已确认幂等的操作，使用有上限的指数退避并记录Attempt。
- 批量生成批准绑定`plan_hash + cost_estimate + generation_config_hash`；任一变化使批准失效。
- 付费MLLM使用独立的持久化预算授权周期；`xmax.evaluation_budget_paused`返回退出码6。每次充值后必须由操作者带`--recharge-confirmed`重开，进程重启、换Worker或`--resume`不得自动重置已花金额。
- `--resume`从Repository读取终态与事件，不依赖进程内缓存。
- `--resume`必须校验`input_hash + config_hash + producer_version`；任一变化时不续跑旧Stage Run。
- 单阶段命令只组装本阶段Adapter。例如`evaluate`的Composition Root不创建XMAX或飞书导入Adapter，以便用测试证明无隐式副作用。

## 7. 外部Adapter与Fake

每个外部边界必须实现同一Protocol的真实Adapter和确定性Fake。Fake至少可脚本化：成功、超时、可重试错误、永久错误、畸形响应和中断恢复；不得只返回永远成功的固定值。

真实Adapter必须保存去密钥后的原始请求/响应或事件URI、外部ID、超时、重试次数和版本。第三方返回值先转成Domain对象，再交给下游。

## 8. 测试层级

每个工作包至少包含：纯函数单测、失败路径、Fake Adapter集成测试、Schema/合同测试和一次幂等/续跑测试。全fake端到端测试固定放在`tests/e2e/test_fake_run.py`，不得联网、调用Codex或消耗XMAX积分。

流水线还必须有独立测试：只生成时不创建Evaluation；只评测时XMAX/Feishu写入Fake的调用次数为0；导入历史Case时不写远端；`score_only`不新建Case或上传附件。

涉及视频的测试夹具放`tests/fixtures/media/`，保持短小并记录生成方式与哈希；禁止依赖开发者Downloads目录。

## 9. 文档与状态同步

实现改变公共行为时，同一提交必须更新Schema、`.example.json`、对应组件文档、RUNBOOK命令和IMPLEMENTATION状态。文档中的“已实现”只能由自动测试或真实验证支持。
