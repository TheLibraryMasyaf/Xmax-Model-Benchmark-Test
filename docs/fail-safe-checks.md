# 执行防呆与熔断合同

本文只定义“不允许继续”的机器级条件。Agent不得用口头判断、手工跳过或临时脚本取代这些检查。

## 1. 真实Run的强制前置检查

`xmax-test run` 在解析Run Request后、创建任何业务Run前强制执行`ContextChecker`。必须同时通过：

- Run Request和Judge Registry JSON Schema，未知字段或拼写错误直接失败。
- Benchmark、Scenario、Operation Recipe的版本和引用完整性。
- 当前模式的所有适用维度至少有一个允许且启用的Judge。
- XMAX Key、Qwen凭据、飞书映射与`lark-cli`、`ffmpeg/ffprobe`、实时Harness依赖。
- `sync`与`sync_policy`相互一致，付费生成有本轮批准。

`context-check`用于提前展示全部缺项；它不是可选的安全开关，因为`run`内部会再执行一次。

## 2. 离线生成前的真实传输检查

在第一条GenerationRun入库前，真实REST/COS Adapter必须：

1. 实际执行`from qcloud_cos import CosConfig, CosS3Client`，不以`pip show`或包目录存在代替。
2. 请求XMAX `/cos/sts`，校验`bucket/region/prefix`和三个临时凭据字段。
3. 任一检查失败就退出，不创建`submit_failure` Run。

统一Run、独立`generate offline`、单条`task run`和批量`worker run`都必须经过该检查。TaskWorker在领取第一条任务前执行共享预检；预检失败时整批任务保持原状态，不能逐条改成`error`。

流水线还有第二道熔断：不可重试异常立即停批；完全相同的异常连续达到`circuit_breaker_threshold`（默认3）时停批。熔断后必须修复根因并显式续跑，不得将阈值设成批次总数。

## 3. 单条Case原子评测

- 一次MLLM批Judge必须返回该Case分配给它的全部维度。
- 免费额度错误以结构化码`AllocationQuota.FreeTierOnly`判断，不依赖固定HTTP状态。
- 换模型时重发整条Case的Prompt、Feed、Prompt素材、操作合同和结果视频，不从上一模型的半成品继续。
- MLLM批Judge失败时不保存EvaluationResult，不用CV/Metric子集重新归一化，不写飞书分数。
- 新Benchmark、Scenario或Judge Registry内容会改变阶段缓存哈希，不得复用旧Evaluation Batch。

## 4. 飞书写入边界

- 只有Run Request显式列出`sync`且`sync_policy != none`才授权远程写入。
- 流水线在单条完整评测后立即upsert；批末`sync`重试失败项，`reconcile`回读核对。
- 唯一业务键是`case编号 + Xmax模型版本`。同一次同步选中多个Run命中同一键时直接报错，不选“最新”或“最高分”。
- 飞书`case评分`单独保存百分比；`case说明`不重复分数，只写1–2个最低分且有可复查证据的维度。

## 5. 中断与续跑

- 已completed且输入、配置、生产者哈希未变的Stage直接复用；只重跑缺失或未完成阶段。
- 生成、评测和飞书同步各自使用不可变ID/内容哈希续跑；不用文件时间或行号猜测。
- 正式已付费视频不因中断自动删除。删除正式Run、飞书记录或仍被引用的Artifact必须再次获得用户确认。

## 6. 执行后必须验证

1. 本轮选中Case数 = Run终态数，不允许出现批量相同基础设施错误。
2. 完整流程中，每个成功评测的Run有且仅有一个本轮EvaluationResult和一个飞书业务键。
3. 所有同步条目回读后分数、说明、Feed/Prompt/Prompt素材/结果附件一致。
4. 有任何评测或同步失败时，整体返回partial/非0退出码，不得以局部成功宣称全批完成。
