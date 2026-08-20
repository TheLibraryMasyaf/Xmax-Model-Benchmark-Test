# Judge责任矩阵

本文只定义不同类型评测标准由哪个组件主判、谁提供辅助证据；不定义维度名称、编号、权重或通过线。

## 1. 责任类型

Benchmark中的每个维度应声明：

```text
primary_judge_kind
secondary_judge_kinds
required_facts
fallback_policy
human_signal_policy
```

可用Judge种类：`metric`、`cv`、`mlmm`和`fusion`。`mlmm`是Provider无关的能力类型，可由本地Codex CLI、OpenAI兼容API或自定义Python Provider提供。人工信号是独立监督来源，不作为常规必经Judge。

## 2. 主判矩阵

| 评测标准关注点 | 主判 | 辅助 | 原因 |
| --- | --- | --- | --- |
| Prompt核心目标、对象关系、多阶段完成 | Codex MLLM | CV检测和运行事实 | 需要理解语言和视频语义 |
| 参考人物/角色/服装/场景还原 | CV | Codex MLLM | 相似度可测，标志性语义需补充 |
| 身份、外观和实例随时间稳定 | CV | Codex MLLM | 需要密集时间特征和跟踪 |
| 姿态、节奏、幅度和动作对齐 | CV | Codex MLLM | 关键点和轨迹可测，动作完整语义需补充 |
| 场景、构图、镜头和空间连续 | CV | Codex MLLM | 光流、特征和轨迹可测，可信度需语义判断 |
| 人体/物体结构、复制、消失、融合 | CV + Codex MLLM | — | 固定检测器和开放错误审计互补 |
| 物理、接触、互动和因果 | Codex MLLM | CV轨迹和遮挡事实 | 纯轨迹不能覆盖复杂常识 |
| 清晰、闪烁、噪声、拖影、编码质量 | CV | 媒体指标 | 可用专用质量模型和逐帧统计 |
| 视觉融合、贴图感、可懂性和整体观感 | Codex MLLM | CV质量特征 | 语义与感知综合判断 |
| 源视频非目标区域保持和编辑边界 | CV | Codex MLLM | 对齐后的区域差异更可靠 |
| 生成成功率、重复稳定性、耗时和费用 | Metric/Fusion | CV/MLLM判定每次是否可用 | 来自多次Run聚合，不靠看图猜测 |
| 实时启动、响应、FPS、掉帧和网络 | Metric | CV检测视觉生效时刻 | 时间戳和RTC事实优先 |
| 实时跟手、空间锚定和异常恢复 | CV + Metric | Codex MLLM | 输入输出轨迹、事件和语义共同需要 |
| 设备资源与网络适应性 | Metric | — | 来自浏览器、RTC和系统采集 |

## 3. 数据生产者不等于Judge

- Generation Runner负责产生任务、Session、费用、时间和错误事实，不直接评分。
- Media Preprocessor负责证据，不决定通过失败。
- Feishu Adapter负责同步，不参与评分。
- Human Signal Hub负责监督数据，不直接修改Judge配置。
- Fusion只按已发布Benchmark合并，不能创造新的视觉事实。
- Fusion按 [场景动态权重](scene-weighting.md) 确定性计算，CV、Codex和人工文本Normalizer都不能直接改写单样本权重。

## 4. 新标准接入

新增维度时先选择关注点，再确定主判种类：

```text
可由确定数值直接计算
→ Metric

需要密集帧、身份、姿态、跟踪、分割或画质
→ CV

需要Prompt、开放语义、物理常识或整体理解
→ Codex MLLM

同时需要事实与语义
→ CV/Metric主证据 + Codex复核 + Fusion
```

如果当前没有合适Judge，维度可以保持Shadow或 `no_automated_judge`，不得为了覆盖率把它强行路由到不合适的模型。

## 5. 人工信号作用

人工评测集与结果反馈可以：纠正单条结果、校准阈值、提供CV训练标签、提供Codex正反例、提出新维度。它不能未经验证直接改变Champion Judge或正式总分。
