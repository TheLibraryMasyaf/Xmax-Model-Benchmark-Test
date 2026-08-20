# 离线生成

本文只说明离线生成适配器、状态机、运行数据和失败处理；不定义视觉评测标准。

## 1. 统一接口

```python
class OfflineGenerationAdapter:
    def prepare(self, case) -> PreparedRun: ...
    def submit(self, prepared) -> ExternalTask: ...
    def poll(self, task) -> GenerationEvent: ...
    def cancel(self, task) -> None: ...
    def collect(self, task) -> GenerationRun: ...
```

当前架构允许两个后端实现，共同产出GenerationRun：

1. `offline-task` REST：通过XMAX官方文件上传协议上传素材、提交异步任务、轮询、下载结果。
2. Session + RTC：创建Session、加入RTC、发送start、heartbeat、等待生命周期事件、关闭Session。

选择哪一个由配置决定，业务层不依赖具体协议。

## 2. 玩法输入配方

Runner只消费TestCase中已经冻结的Operation Recipe绑定，不重新解释玩法：

| 配方 | 被编辑视频 / `refVideoPath` | `refImagePath` | 预期保留音轨 |
| --- | --- | --- | --- |
| 图片参考类 | Feed视频 | Prompt图片 | Feed视频音轨 |
| 视频参考类 | Prompt视频 | Feed视频截图 | Prompt视频音轨 |

视频参考类包括运镜、对口型、手势舞、舞蹈、换动作、搞怪等。Feed截图必须作为独立Asset保存并上传。旧示例Runner把`refVideoPath`固定为Feed的做法不得复用。

## 3. REST适配器

流程：

```text
验证本地素材
→ 获取COS临时凭证
→ 按uploadImage/uploadVideo的MIME和文件名合同上传
→ 优先使用COS响应Location，其次使用STS endpoint生成XMAX认可URL
→ POST /offline-task
→ 记录task uid
→ 轮询submitted/processing/completed/error
→ 下载result_url
→ 校验结果媒体
→ 保存费用和计费时长
```

每次状态变化追加到 `events.jsonl`。并发、批大小和重试策略属于Run配置，不能写死在领域对象中。

当前真实适配器按官方合同使用`X-Api-Key`、`/cos/sts`和`/offline-task[/<taskUid>]`，并解包`success/code/message/data`响应。这里需要的不是给本地文件做“公网伪装”，而是得到XMAX官方上传链路认可的媒体URL：`refImagePath`与`refVideoPath`不得使用自建公网URL、本地路径、`blob:` URL，也不得忽略COS响应后自行猜测对象地址。Python适配器实现与当前`@xmaxai/sdk` `uploadImage/uploadVideo`等价的STS + COS契约，包括扩展名缺失时的媒体类型识别、正确`Content-Type`、安全对象名，以及`Location → endpoint → 标准COS域名`的URL解析优先级。真实运行需`XMAX_API_KEY`和`cos-python-sdk-v5`；CLI只有收到`--budget-approved`才提交计费任务。

## 4. Session + RTC适配器

根据项目根目录的参考时序，拆成三层：

本仓库包含真实Session HTTP Client、状态机和RTC Protocol/Fake，但不内置一个特定RTC供应商的生产实现。常规离线生成使用上节官方REST；只有显式提供RTC插件时才选`session_rtc`。

### Session API Client

- `POST /session`。
- `PUT /session/{sessionUid}/heartbeat`。
- `DELETE /session/{sessionUid}`。

### RTC Adapter

- 根据Session返回加入房间。
- 等待ready。
- 发送带本地任务UID的 `start` 消息。
- 采集并按UID过滤 `video_started`、`video_completed`、`video_stopped`、`error`。

### Task State Machine

```text
create_session
→ join_room
→ start_sent
→ video_started
→ completed | stopped | error | timeout
→ close_session
```

heartbeat与事件等待并行；失败也必须尝试关闭Session。原始请求、响应和RTC事件完整保留。

## 5. 离线运行指标

生成模块负责记录事实，不直接判定好坏：

- 提交、排队、开始、完成、下载时间。
- 任务是否成功、错误码和重试次数。
- 计费积分、计费秒数、质量档位和FPS。
- 结果文件大小、时长、帧率和音轨。
- 每个Feed × Prompt重复生成的Run集合。

O5/O6的阈值和评分方式由Benchmark定义；生成模块只提供计算所需数据。

音频事实必须以`expected_audio_source_asset_id`为基准：记录源音轨是否存在、输出音轨是否存在、内容对应性、起始偏移和全程漂移。只评价原音轨保留与音画同步，不扩展为音乐审美或音质偏好。

## 6. 续跑与幂等

- 每次真实提交必须有新的 `run_id`。
- `external_task_id`避免重复轮询和重复下载，但不能作为本地唯一ID。
- 已完成结果经哈希和媒体校验后可跳过下载。
- 失败重试追加新Run，并通过 `retry_of_run_id`关联。
- 上传缓存按`媒体角色 + 本地内容哈希`索引，不按绝对路径单独判断。
- 提交前失败也必须把GenerationRun置为`error`并记录`submit_failure`，不得遗留伪`running`记录。
- 跨计划续跑按不含`plan_id`、Case后缀和无关素材批次血缘的`generation_signature`匹配同一生成组合；已完成组合不得因为重新下载了相同素材或重建计划而再次提交付费任务。

## 7. 失败分类

至少区分：输入无效、上传失败、认证失败、额度不足、并发限制、Session失败、RTC进房失败、start发送失败、heartbeat失败、模型error、生命周期超时、下载失败、媒体校验失败。

这些分类用于O5/O6统计和运维，不应被Codex推测。
