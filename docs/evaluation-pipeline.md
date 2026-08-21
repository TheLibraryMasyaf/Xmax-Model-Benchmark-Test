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
→ 计算Canonical Score
→ 确定性解析场景权重并计算Scenario Score
→ 输出分项结果、双总分、覆盖率和审计记录
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

`context-check`在执行前按模式核对每个适用维度是否至少存在一个路由允许的已启用Judge。当前Shadow包中，运行事实Judge覆盖成功、成本及实时运行指标，ffmpeg CV覆盖基础画质，Qwen3-VL覆盖开放视觉语义。声明“有路由”不等于每条样本必然可评：O4需要完整重复组，R5需要版本化异常脚本，R6需要达到长会话最低时长；条件不足时必须返回不可评并从当条总分分母剔除。

## 4. 自动Judge分工原则

- CV：可测量的身份、姿态、轨迹、分割、实例、画质和局部差异。
- Codex MLLM：Prompt完成、复杂语义、物理互动、错误解释和整体可懂性。
- 运行指标：任务状态、费用、延迟、FPS、丢包、重试和设备数据。
- Fusion：只合并已经版本化的结果，不重新看视频。

具体维度到Judge的映射由Benchmark给出，不在本模块写死。

## 5. 证据

Judgment证据优先包含：

- 时间段或事件窗口。
- 主体/区域/轨迹ID。
- 可复查描述。
- 原始指标或帧索引。
- 预处理产物URI。

MLLM只能描述输入中可见内容；不得把Prompt、API错误或工程猜测当作视觉证据。原生视频允许判断可见画面连续性和时序，但Qwen3-VL不能据此判断音轨、API延迟或其他运行事实。

MLLM批量Judge对一条Case的全部所属维度是原子操作。遇到`AllocationQuota.FreeTierOnly`时，Provider用下一个配置模型重发该Case的完整请求；所有候选都失败时，该Case评测失败且不保存、不上传残缺分数。禁止丢弃MLLM维度后用剩余CV/Metric结果重新归一化总分。

## 6. 融合

融合顺序：

1. 先执行媒体有效性等阻断型硬门槛；失败时停止产生视觉总分，但保留运行事实。
2. Judge输出合法性和可评估性。
3. 每个Judge置信度校准。
4. 同维度多Judge融合。
5. 使用发布的基础Profile计算Canonical Score。
6. 使用场景标签和发布规则计算Scenario Score。
7. 应用其余不可被权重抵消的Fail/Cap/Block Gate并生成最终结论。

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

正式结果遵循 `schemas/evaluation-result.schema.json`，保存Benchmark、Scenario Pack、Weight Profile、命中规则、Score Schema、Judge、预处理器和模型版本，以支持历史回放。
