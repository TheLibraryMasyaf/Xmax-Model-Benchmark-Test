# CV Judge

> `.venv-cv/`和`var/models/`是本机可重建的可选运行缓存，不是合同、Champion声明或仓库交付物。只有`config/judges.json`中`enabled=true`且`context-check`实际导入成功的Judge才属于当前评测链路；目录中存在权重文件不代表已经启用。

本文只说明专项视觉模型的职责、插件规范、候选能力和学习方式；不决定最终评测维度。

## 1. Judge插件

Python插件实现 `xmax_test.judges.base.JudgePlugin`，manifest符合 `schemas/judge-manifest.schema.json`，输出符合 `schemas/judgment.schema.json`。

一个Judge可以支持多个维度，一个维度也可以绑定多个Judge。插件不得直接读取飞书、修改Benchmark或写最终总分。

仓库内置三个可执行基线：`video-quality`用ffmpeg灰度帧覆盖P.1空画面、G1基础画质和G2时序信号；`audio-integrity`用PCM能量包络覆盖G3.1来源/完整性和G3.2可测时间轴对齐；`run-metrics`只覆盖P.1结果登记、实时G2.1帧更新和R1响应速度。实时SDK已请求订阅但远端输出流没有音频轨时，`audio-integrity`将G3标为不适用；离线结果缺少应有音轨，或远端已有音轨但录制文件丢失音轨，仍按0分处理。P.2/P.3/RP.1/RP.2由冻结批次报告器直接统计，不生成Judgment。无法解码或缺少实验时返回`assessable=false`，不返回中性占位分。

当前`var/models/`中的DINOv2和MUSIQ权重属于候选资产，尚未注册成启用Judge，当前得分不会假装使用这些模型。开放语义、结构和跨帧现象由Qwen3-VL Shadow Judge补充；后续专项CV Challenger通过同一插件协议替换相应路由。

## 2. 候选能力组件

这些是候选技术，不代表已安装或已绑定正式维度：

| 能力 | 候选模型/方法 | 适合输出 |
| --- | --- | --- |
| 感知画质 | DOVER、FAST-VQA | 技术/审美质量特征、时间窗口趋势 |
| 全参考局部差异 | LPIPS、VMAF（严格对齐时） | 非目标区域保持、编码损失 |
| 人脸身份 | ArcFace/InsightFace | 人脸相似度、时间漂移 |
| 通用主体外观 | DINOv2/CLIP区域特征 | 非真人或风格化主体相似度 |
| 人体姿态 | ViTPose/RTMPose | 关键点、骨长、姿态置信度 |
| 点/实例跟踪 | CoTracker、SAM 2 | 轨迹断裂、实例增减、遮挡恢复 |
| 光流 | RAFT或同类 | 运动连续性、对齐和局部跳变 |
| 音画/口型 | SyncNet或同类 | 音画偏移、口型同步特征 |

模型选择、权重和阈值必须通过Benchmark版本和Judge Manifest发布。

## 3. 标准输出

CV Judge不能只返回一个维度裸分，必须在`criterion_results`中只列出它真正测量的Benchmark细则。至少返回：

- `criterion_id`。
- `assessable`。
- 细则级`score`和原始指标。
- `confidence`。
- 时间段。
- ROI、轨迹或关键点引用。
- `raw_metrics`。
- 失败原因或不适用原因。

例如音频Judge只能提交G3.1/G3.2；基础画质Judge只能提交P.1、G1.1/G1.2或G2.1/G2.2。任何Judge都不能用整体分替代未测细则；Fusion先合并同一细则的多Judge结果，再派生标准分。

## 4. 运行隔离

不同CV模型依赖容易冲突，建议按插件或服务隔离：

```text
Judge Registry
→ Worker Router
→ Python env / Container / Local service
→ normalized Judgment
```

Manifest声明CPU/GPU、显存、批大小、超时和输入要求。主项目基础包不强制安装所有深度学习依赖。

## 5. 学习通道

人工信号进入CV的顺序：

1. 校准现有阈值。
2. 冻结底层特征，训练轻量分类/回归头。
3. 有足够时间段、bbox、mask或关键点标签后微调专项模型。

“看起来很怪”不能直接训练手部或姿态模型；LLM必须先判断反馈能否转成该模型需要的监督信号。

`human partition --output <file.jsonl>`导出的1.0学习包保留原文、规范化标签、时间段/ROI等`annotations`和Feed/Prompt/Result/预处理证据引用。CV训练器只读`route_kind=cv`且`training_eligible=true`的包；Calibration只调阈值，Holdout只验证。

## 6. 新维度处理

新维度暂时没有CV Judge时：

- 使用Codex MLLM进行Shadow判断；或
- 只收集人工标签并标记无自动覆盖。

积累数据后注册新CV Judge。新Judge先进入Shadow，不能立即替换Champion。

## 7. 版本发布

Judge状态：

```text
registered → shadow → champion → deprecated
```

升级Judge必须在冻结回归集和人工Holdout上比较严重问题漏判、误判和分场景退化。模型文件、阈值和预处理配置共同构成版本。
