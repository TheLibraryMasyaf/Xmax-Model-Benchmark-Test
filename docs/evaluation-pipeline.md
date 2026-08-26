# 自动评测编排

本文只说明自动评测的执行顺序、Judge路由、证据与融合；不定义具体维度内容和实际权重值。

## 1. 输入

每次评测至少读取：

- TestCase：Feed、Prompt包、模式和场景标签。
- GenerationRun：结果视频、事件、运行指标和版本。
- Benchmark合同：维度、适用模式、证据要求和Judge路由。
- Scenario Pack：合法场景标签、取值和版本。
- Judge Registry：可用Judge版本和能力。

GenerationRun可由XMAX生成，也可由飞书Case/本地结果/Stage Manifest导入。评测只接受`status=completed`、结果媒体已校验且TestCase上下文完整的Run；它不调用生成器、不从飞书下载视频、不写远端。导入Run没有的延迟/FPS/RTC指标为不可评，不从画面反推。

Benchmark为空或状态不是可执行版本时，系统允许做合同检查和素材/生成测试，但不产出正式Benchmark分数。

## 2. 执行顺序

```text
验证Run与Benchmark
→ 解析适用维度
→ 读取显式preprocess阶段已生成的共享抽帧/ROI/事件窗口，并解析原始角色媒体
→ 并行执行CV Judge、Codex Judge和纯指标Judge
→ 验证每条Judgment Schema
→ 置信度校准与同维度Judge融合
→ 应用硬门槛并确定可评维度
→ 确定性解析场景权重并计算Scenario Score
→ 输出单视频分项、Scenario Score、覆盖率和审计记录
→ 在冻结批次收口P.2/P.3/RP.1/RP.2报告指标
```

预处理是独立阶段：统一Run通过`preprocess_batch`显式交接；单独`evaluate --run-batch-id`只会查找该Run已持久化的完成预处理记录，缺失时报错，绝不隐式抽帧。预处理器将Feed、Prompt参考素材和Result按角色分组，同一结果只解码一次，供CV、审计和不支持视频的MLLM共享。支持原生视频的MLLM从同一TestCase/Run合同解析原始媒体，按`generation_operation → feed → 实际feed_capture（如有）→ prompt_text → prompt_reference_N → result_video`分项传入，不读取扁平化截图列表。

`generation_operation`来自Case冻结的Operation Recipe评测合同，至少说明`edited_video_role`、`api_asset_bindings`、`expected_audio_source_role`、`role_semantics`、`must_preserve`、`must_change`和`result_expectation`。Judge不得根据“Feed/Prompt”字面名称自行假设谁是源视频。

## 3. 路由

每个维度可声明：

```json
{
  "primary": ["judge-a@1.0.0"],
  "secondary": ["judge-b@2.1.0"],
  "fallback": ["codex-mlmm@3.0.0"],
  "minimum_confidence": 0.7,
  "fusion": "configured"
}
```

若没有兼容Judge：

- Shadow维度：记录 `no_automated_judge`。
- Active且允许MLLM fallback：调用配置的Codex Judge。
- Active且不允许fallback：维度不可评分并降低覆盖率，不能按满分或零分填充。

`context-check`在执行前按模式核对每个适用维度是否至少存在一个路由允许的已启用Judge。当前Shadow包中，运行事实Judge覆盖P.1、实时帧更新和R1，ffmpeg CV覆盖P.1/G1/G2，音频Judge覆盖G3，Qwen3-VL覆盖P.1/E1–E4/R2。声明“有路由”不等于每条样本必然可评：没有连续交互实验时R1.2不适用；音频基准不存在，或实时SDK请求订阅后远端输出流仍无音频轨时G3不适用；证据不足时必须返回不可评，不能伪造分数。

## 4. 自动Judge分工原则

- CV：可测量的身份、姿态、轨迹、分割、实例、画质和局部差异。
- Codex MLLM：Prompt完成、复杂语义、物理互动、错误解释和整体可懂性。
- 运行指标：任务状态、费用、延迟、FPS、丢包、重试和设备数据。
- Fusion：只合并已经版本化的结果，不重新看视频。

具体维度到Judge的映射由Benchmark给出，不在本模块写死。Judge实际评分单元是细则而不是维度：每个可评细则必须输出`criterion_id`、0/1/2分、置信度和证据。CV/Metric只输出它真正覆盖的细则；MLLM必须对本次合同的所有细则逐条返回可评或不可评。维度级`score`只是审计便利字段，Fusion不信任它。

## 5. 证据

Judgment证据优先包含：

- 时间段或事件窗口。
- 主体/区域/轨迹ID。
- 可复查描述。
- 原始指标或帧索引。
- 预处理产物URI。

MLLM只能描述输入中可见内容；不得把Prompt、API错误或工程猜测当作视觉证据。原生视频允许判断可见画面连续性和时序，但Qwen3-VL不能据此判断音轨、API延迟或其他运行事实。

MediaRecorder产生的WebM如果容器未写入duration，预处理仅在常规ffprobe结果缺失时读取视频packet时间戳推导时长和帧率，并把`duration_source=packet_timeline`保存为派生媒体元数据。原始WebM不转码、不覆盖。

直接视频MLLM输入不直接修改MediaRecorder原始WebM。预处理会另行产生`webm-h264-v2` H.264/yuv420p/24 FPS MP4 Provider副本，用于规避上游对无duration或异常帧率WebM的`Invalid video file`和Base64体积边界；Manifest同时保留原Asset ID、SHA256与转换版本。

MLLM批量Judge对一条Case的全部所属维度是原子操作。遇到已验证的免费额度信号（`AllocationQuota.FreeTierOnly`或已收录的`insufficient_quota`网关变体）时，Provider用下一个配置模型重发该Case的完整请求。未知quota信号、同模型网络重试耗尽或所有免费候选均耗尽时，整个评测批次安全暂停，不由语义Agent直接决定切换或付费。当前Case不保存、不上传残缺分数。禁止丢弃MLLM维度后用剩余CV/Metric结果重新归一化总分。

唯一付费候选必须是模型列表最后一项。首次进入付费候选、99元本地上限无法预留下一次请求，或末位模型仍返回`FreeTierOnly`时，预算闸门抛出`xmax.evaluation_budget_paused`。当前Case不保存CV/Metric子集；后续Case在进入任何Judge前被拦截。流式生成和预处理继续排空，评测、同步和报告在此边界暂停，待人工充值确认与显式授权后续跑。

## 6. 融合

融合顺序：

1. 校验Judge是否只提交Benchmark声明的细则；MLLM必须完整返回分配给它的全部细则。
2. 同一`criterion_id`有多个Judge时合并分数、置信度和带Judge身份的证据；当前合并是算术平均，保留`judge_score_count`以便后续改版。
3. 按Benchmark的细则尺度确定性计算维度分：`sum(可评细则分) / sum(可评细则满分)`；当前每条细则等权。
4. 用细则分执行精确到`criterion_id`的Hard Gate；高维度平均不能掩盖阻断细则。
5. 使用显式`scenario_id`命中发布规则并计算Scenario Score；当前Benchmark不计算Canonical Score。
6. 保存逐细则、派生维度、有效权重、Gate和覆盖缺口。

不可评与未覆盖分开记录：有Judge提交但证据不足是`unassessable`，没有任何Judge提交该细则是`uncovered`。两者都不得填默认1分。

权重只允许来自版本化Benchmark，算法见 [场景动态权重](scene-weighting.md)。Codex、CV Judge和操作者均不能为单条样本临场发明权重。若数据不足，先展示分项和覆盖率，不生成具有误导性的总分。

## 7. 冲突

CV与Codex相反时仍可自动出结果，但必须保存冲突状态。人工线可以处理任意结果，不只处理冲突；冲突只是推荐抽查来源之一。

## 8. 输出

```text
evaluation.json
judgments.jsonl
coverage.json
conflicts.jsonl
weight-resolution.json
raw/<judge_id>/
```

`evaluation.json`的`criterion_results`是单条视频评分事实；`dimension_results`必须从其派生。`evaluate_runs`同时返回并在Evaluation Batch Manifest中保存`criterion_summary`、`dimension_summary`和`case_score_summary`，包含覆盖率、均值、中位数、最小/最大、标准差、P25/P75和分布。

所有0/1/2细则均属于单条Run；P.2、P.3、RP.1、RP.2是冻结批次的报告指标，不生成Judgment、不回填单视频分数。P.3按相同模型、模式、Feed、Prompt文字、Prompt素材、配方、生成参数与场景分组，重复次数来自冻结Run Request的实际`repeat_count`，默认值可配置且不得写死。`not_applicable`表示本Case没有配置对应实验并从分母排除；`unassessable`表示实验已要求或已执行但证据不足；`uncovered`表示没有Judge提交，三者不得互换。

正式结果遵循 `schemas/evaluation-result.schema.json`，保存Benchmark、Scenario Pack、Weight Profile、命中规则、Score Schema、Judge、预处理器和模型版本，以支持历史回放。
