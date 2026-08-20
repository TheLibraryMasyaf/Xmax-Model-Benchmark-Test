# 测试计划

本文只说明如何把素材展开成可复现、可预算的测试组合；不执行生成和评测。

## 1. 基本组合

```text
Feed × Prompt包 × Operation Recipe × generation_mode × model_id × repeat_index × 环境配置
```

Prompt包可以包含文字、参考图、参考视频和mask。`repeat_index`属于TestCase身份的一部分，用于离线成功率、重复稳定性和实时重复任务测试。

Operation Recipe来自`config/operation-recipes.json`，负责声明玩法默认Prompt、被编辑视频、预期音轨来源和API素材绑定。计划生成器不得仅根据文件扩展名猜`refVideoPath`或把Feed永远当作被编辑视频。

## 2. 计划生成步骤

1. 从Asset Registry只选择 `ready` 素材。
2. 加载通过 Schema 校验的 Scenario Pack，根据玩法/场景规则组成 Feed与Prompt，并产出合法场景标签。
3. 解析Operation Recipe并固化`edited_video_asset_id`、`expected_audio_source_asset_id`和API绑定。
4. 分配 `offline`、`realtime` 或两者。
5. 展开重复次数；默认5次，但Run Request、项目配置和单Case均可覆盖。
6. 从飞书现有最大后缀继续分配`case_number`，固定随机种子并生成稳定 `case_id`。
7. 验证输出文件名、业务键和组合无重复。
8. 生成预算预览，等待计费任务批准。
9. 先生成最小 smoke 子计划，通过后再执行全量。

`filters.feed_limit`和`filters.prompt_limit`表示按业务编号稳定排序后的前N项；也可用`feed_asset_ids`和`prompt_record_numbers`固定具体集合。所有过滤条件都写入Plan Hash和随机种子输入，避免小批次与全量计划互相误复用。

## 3. 生成模式解析

模式优先级固定为：

```text
Case显式指定
→ Run Request按玩法覆盖
→ Operation Recipe默认模式
```

互动要求的玩法（触控、滑动、轨迹、实时场景互动）默认`realtime`；其他玩法默认`offline`。场景包和项目能力只做支持性校验，不静默改写选择。用户明确要求两种都测时展开两个Case；显式选择不受支持的模式时报合同错误并进入计划跳过清单。

## 4. 预算预览

任何付费批量任务执行前输出：

```text
计划组合数
按模式的任务数
重复次数
预计素材上传数
预计积分范围
预计最长运行时间
跳过素材及原因
已知不支持的玩法
```

计划阶段不读取真实密钥、不提交任务、不上传素材。

## 5. Case编号、重复与可复现性

TestPlan保存：

- 计划版本和创建时间。
- Benchmark版本。
- Scenario Pack版本。
- 随机种子。
- 素材 `asset_id` 与内容哈希。
- 模型ID和生成参数。
- 组合规则版本。
- 重复序号。
- Operation Recipe ID和版本。
- 被编辑视频、预期音轨来源和API素材绑定。
- 飞书`case_number`及编号分配时读取的远端revision。

同一计划重跑时创建新的GenerationRun，但TestCase ID保持不变。

一个Case只代表一次Run。重复次数大于1时，每次Run在飞书Case表中独立写一行，编号为`feedXXX_promptYYY_01..._NN`。后续同版本、同组合重测从远端最大后缀继续；生成失败仍占用编号并以0%写入，重试不得覆盖。

## 6. 模式特有配置

离线示例：质量档位、FPS、采样方式、mask、并发与批大小。

实时示例：输入方式、流宽高、FPS、码率、contentHint、音频、会话时长、事件脚本、网络和设备配置。

模式特有字段放在 `generation_config`，不能扩散为顶层固定列。

## 7. 场景与Benchmark关系

TestPlan必须保存 Scenario Pack 版本和场景标签，但适用评测维度与权重必须由当前 Benchmark 解析。计划生成器不能根据旧标准硬编码“某玩法固定评哪些维度”，也不能自行写入数值权重。场景标签只作为融合层匹配 `scene_weight_rules` 的确定性输入。
