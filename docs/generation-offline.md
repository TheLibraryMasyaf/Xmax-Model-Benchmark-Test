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

当前架构允许三个后端实现，共同产出GenerationRun：

1. `offline-task` REST：通过XMAX官方文件上传协议上传素材、提交异步任务、轮询、下载结果。
2. Session + RTC：创建Session、加入RTC、发送start、heartbeat、等待生命周期事件、关闭Session。
3. Decart Queue：将本地视频/参考图以multipart提交到`POST /v1/jobs/lucy-2.5`，轮询`GET /v1/jobs/{job_id}`，并从`GET /v1/jobs/{job_id}/content`下载结果。

选择哪一个由TestPlan中冻结的`generation_provider`决定，业务层不依赖具体协议。同一TestPlan不允许混合Provider。

## 2. 玩法输入配方

所有新生成在素材角色绑定之前执行 `drop-first-decoded-frame-v1`：Feed 视频无论有无封面，都删除第一个解码帧，以第二帧实际 PTS 作为裁剪点，音频按相同时间点裁剪并重置时间轴。保留剩余帧的时间间隔、画幅和音轨；少于两帧、解码失败或帧数/时间戳校验失败时不提交生成。原文件不变，派生视频及 SHA-256、裁剪命令、源/结果帧数保存在 `artifact://feed-preprocessing/`。输出哈希回执使同一 Artifact Store 内重新登记的裁剪视频不会再少一帧；跨库搬运须同时保留这些回执，不能仅凭文件名判断已处理。

XMAX、Lucy 2.5 和 Session/RTC 共用该处理。图片参考类上传裁剪后的 Feed；视频参考类从裁剪后的 Feed 重新截图，Prompt 视频保持原样。新计划将策略版本写入 `generation_config.feed_input_policy`，参与生成签名。已完成历史结果不自动重跑；有外部任务 ID 的在途任务仍只续轮询。实际裁剪证据追加到 Run 的 `feed_preprocessed` 事件。自定义调用方只可在测试中注入 `FakeFeedPreprocessor`，生产默认使用真实 FFmpeg 处理。

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

若进程在`task_submitted`之后退出，Run保留`external_task_id`。同一Run Batch续跑相同Case时，Adapter必须先轮询该外部任务并下载终态结果，禁止重新上传或POST第二个付费任务；只有没有同批非终态Run时才创建新Attempt。

### 3.1 Decart Lucy 2.5

Lucy只接入离线Queue API；`generation_provider=decart`必须与`generation_modes=["offline"]`一起冻结。实时输入、RTC回调、首帧/帧间延迟和网络准入继续是XMAX专有项目，不给Lucy伪造对应指标。

实现依据 [Decart Lucy 2.5 API Reference](https://docs.platform.decart.ai/api-reference/lucy-25) 和 [Decart Pricing](https://docs.platform.decart.ai/getting-started/pricing)。计价页可能随模型版本调整，所以`decart_cost_usd_per_second`是项目配置而非写死常量；每次真实批次前需核对当时账户价格，并用1条smoke账单校准。

Operation Recipe到Lucy的映射为：`refVideoPath`解析后的视频作为`data`，`refImagePath`解析后的图片作为可选`reference_image`，文字作为`prompt`。视频上传前按`decart-720p-h264-pad-v1`生成内容哈希缓存：保留横/竖屏方向，等比缩放并黑边pad到1280×720或720×1280，H.264/yuv420p/AAC封装为MP4。长视频可显式选择独立的`decart-720p-h264-size-capped-v2` profile；该profile尽量保持源帧率、时长、方向和音轨，按180,000,000字节目标控制视频码率，并在200,000,000字节硬上限处失败关闭。批量长视频推荐使用`decart-720p-h264-crf18-capped-v3`：保持CRF 18画质，以`maxrate/bufsize`做VBV上限，短片视频maxrate最高8 Mbps，长片按180,000,000字节目标随时长收紧，音频128 kbps；同样校验时长、方向、音轨和200,000,000字节硬上限。三种profile均在提交前执行大小校验。

`enhance_prompt=false`作为评测默认值，避免Provider重写Prompt破坏同批可比性；`self_anchor=true`、`resolution=720p`和`seed`与输入转码Profile一起冻结。创建任务的POST不自动重试；如果上传后丢失响应，Run保持`running`/`ambiguous_submission`，必须先人工对账，不能盲目再付费。GET状态/结果请求才允许有上限重试。

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

P.2/P.3/P.4的统计口径和G1-G3的评分方式由Benchmark定义；生成模块只提供Run终态、重试、分离后的排队/模型生成/结果传输/端到端耗时、媒体和音轨事实，不生成批次分或单视频分。只有新鲜提交且观察到`processing`状态的REST任务才记录`queue_wait_s`与`model_generation_elapsed_s`；中断续跑不得用续跑片段冒充全程耗时。

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

这些分类用于P.2批次失败原因统计和运维，不应被MLLM推测。
