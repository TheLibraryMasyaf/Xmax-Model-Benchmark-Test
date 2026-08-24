# MLLM Provider 与 Qwen3-VL

本文只说明多模态Judge和人工评价归一化器的Provider边界；不定义具体评分标准。当前真实配置使用阿里云百炼Qwen3-VL，本地Codex CLI仍是可替换Provider，不是业务层硬依赖。

## 1. 两种职责

### 视频评测 Judge

读取按角色分离的Feed原素材、Prompt文字、Prompt参考素材和Result原视频，对Benchmark分配的每条细则输出0/1/2或不可评的结构化Judgment。若Provider不支持原视频，再使用预处理抽帧作为兼容输入。

### 人工反馈 Normalizer

把人工非结构化评价映射为现有维度标签、部分匹配或新维度提案。原文始终保留，MLLM输出是派生数据。

两种职责使用不同Prompt、输出Schema和Judge ID，不共用对话上下文。

## 2. 视频输入

Qwen3-VL默认使用原生视频输入，不再把通用抽帧截图作为它的默认视觉输入。一次请求中的业务输入顺序和标签固定为：

1. `generation_operation`：版本化操作合同，说明哪个视频被编辑、素材如何绑定、结果必须保留和改变什么。
2. `feed`：Feed原图片或原视频；它是否是被编辑视频由操作合同决定。
3. `feed_capture`：仅视频参考玩法提供，必须是生成时实际作为`refImagePath`使用的Feed截图。
4. `prompt_text`：原始文字Prompt，作为独立文本块。
5. `prompt_reference_N`：零个或多个Prompt图片/视频素材，每个单独标记；它可能是参考，也可能是被编辑视频。
6. `result_video`：当前Run生成的结果视频。

OpenAI兼容Provider把视频编码为`video_url`、图片编码为`image_url`，并在每项前插入`[INPUT_ROLE:<role>]`文本标签；不得把所有图片或视频扁平化后只依靠附件顺序猜角色。Judge Prompt中也包含同一份操作合同，保证图片型Provider仍能理解生成语义。项目中的`.bin`只是Artifact Store的内部保存名，Provider会按文件签名识别真实PNG/JPEG/MP4/WebM MIME，传给模型的仍是原始媒体格式。

当前Qwen配置使用`fps=2.0`、视频帧`min_pixels=65536`、`max_pixels=655360`和跨候选都可用的`total_pixels=50000000`。评测由XMAX离线生成的Run时，系统优先复用Run事件中按素材SHA匹配的Feed/Prompt上传URL和结果URL，不重复上传；没有可复用URL时才用Base64 Data URL。本地Base64单项编码后不得超过`max_base64_bytes=10000000`；超限必须由输入包提供模型可访问的URL，不能静默压缩原视频或退回截图并冒充原生视频评测。

预处理截图仍然保留，但职责改为：CV Judge输入、ROI/异常窗口、审计复核，以及Codex CLI等不支持原生视频Provider的兼容兜底。它不再进入启用`direct_media`的Qwen请求。联系图不能代替FPS、实时延迟、音频和设备数据；Qwen3-VL只能读取视频视觉内容，音轨仍由音频Metric Judge评测。

## 3. Provider适配器

`config/judges.json` 中的 `provider.type` 可为 `openai_compatible`、`codex_cli`或`python_plugin`。Judge层调用 `complete_json(prompt, image_paths, output_schema, media_inputs)`；`media_inputs`是可选的带角色原始媒体合同，`image_paths`是兼容兜底。评分维度、人工校准和融合不依赖供应商。

### 百炼视频MLLM真实配置

当前 `config/judges.json` 从项目上层 `QWEN_API.csv` 读取 `apiKey` 和 `openAiCompatible`，不复制密钥、不写日志。CSV是两列键值格式；工作空间端点会自动补上 `/chat/completions`。当前凭据文件位于Git仓库之外，不可能被本仓库跟踪；`.gitignore`也额外忽略`QWEN_API.csv`，防止后续被复制进仓库时误提交。

他人接入时只需在他们的项目上层放置同格式CSV，并至少提供`apiKey`与`openAiCompatible`两行；不需要改Python代码。若改成环境变量，可在Judge配置中使用`api_key_env`并显式配置对应地域/业务空间的`endpoint`。

模型顺序以`config/judges.json`为唯一真源。2026-08-21通过百炼控制台登录账户逐项回读后，删除了无免费额度的`qwen3.8-max`，当前共26个候选：前25个只使用免费额度，唯一末位兜底是泛化别名`qwen3-vl-flash`。前25个模型必须保持“免费额度用完即停”开启；只有末位`qwen3-vl-flash`在人工充值并准备启用付费时关闭该开关。

以下项不进入当前调用链：控制台明确显示“无免费额度”的模型；剩余额度显示为`-`的泛化别名；当前请求的非思考结构化输出协议不匹配的Thinking专用规格；以及仅WebSocket实时协议的Omni模型。全模态HTTP模型虽然当前显示100万Token，但账户的“免费额度用完即停”尚未开启，因此未注册为回退候选。

请求使用 `response_format={"type":"json_object"}` 和 `enable_thinking=false`，本地再用JSON Schema严格校验。免费额度耗尽按版本化规则识别：包括`AllocationQuota.FreeTierOnly`，以及百炼网关实际返回的`insufficient_quota + Free quota exhausted + free tier only`组合，不依赖固定HTTP状态。只有命中已验证规则才切换模型；未知quota类返回使整个评测批次安全暂停，不切模型、不进入付费。切换后完整重发当前Case，不跳过、不保存前一模型的半成品。末位`qwen3-vl-flash`第一次被选中时，如果项目预算尚未显式授权，则在发出请求前暂停。

付费兜底由`provider.paid_fallback`配置，默认本地硬上限99元。价格阶梯来自[阿里云百炼Qwen3-VL-Flash官方计费页](https://help.aliyun.com/zh/model-studio/qwen3-vl-flash)，作为版本化配置保存，价格变化时先更新配置和测试。系统在每次末位调用前保守预留0.36元，成功后按百炼返回的输入、缓存输入和输出Token结算；超时因计费结果不明确而按整笔预留计入，明确HTTP拒绝或连接前失败则释放。达到无法再预留下一次调用的边界时，预算持久化为`paused`，后续Case在任何CV/Metric/MLLM Judge开始前停止。这里统计的是本项目数据库中的保守估算，不是阿里云账户总账；其他程序的调用不会被计入。

超时设为180秒。429、408、5xx、DNS和连接错误只在同一免费模型上做有上限的指数退避，优先遵守`Retry-After`；网络错误不触发模型切换。默认参数为`transport_max_retries=2`、`retry_backoff_seconds=1`、`retry_backoff_max_seconds=8`、`retry_jitter_seconds=0.25`。重试耗尽、认证失败或未知quota会暂停后续评测，streaming中的生成和预处理仍可排空。Judge层不再对已经耗尽Provider重试的错误二次放大。付费请求不做自动网络重试，避免无法对账的重复计费。每次尝试写`request_started/request_succeeded/request_failed`时间、耗时、模型和错误码到`var/logs/mlmm/mlmm-events.jsonl`，不记录密钥。

2026-08-20已用一个真实离线Case做原生多输入烟测：同一请求传入Feed视频、Prompt文字、Prompt图片和Result视频，`qwen3-vl-plus`正确回传四个角色且`role_confusion=false`；调用消耗5089输入Token、273输出Token。该烟测只验证输入能力和角色隔离，不作为正式Benchmark评分。

进程重启后会从第一个模型重新检测；已耗尽模型返回结构化额度错误后会立即跳过，不假设固定HTTP状态。付费预算状态保存在SQLite，重启不会解除暂停。每次返回保存实际 `provider_model`、Token usage、`model_fallback_attempts`和`transport_attempts`，便于报告对账。

### Codex CLI可替换配置

参考调用形态：

```bash
codex exec \
  --skip-git-repo-check \
  --sandbox read-only \
  -i <evidence-image> \
  '<judge-prompt>'
```

适配器必须配置二进制路径、超时、重试、工作目录和输出Schema。不得在命令行中拼入API密钥或敏感URL。

当前实现还传`--ephemeral --ignore-rules --color never --output-schema <schema.json>`和每张证据图的`-i <absolute-path>`。运行目录是临时隔离目录，避免项目AGENTS/工作区文件影响盲评。模型输出Schema只包含`verdict/confidence/assessable/evidence/criterion_results`等模型负责的字段；顶层维度分不由模型提交。评测ID、Run ID、维度和Judge身份由本地系统注入后再用完整Judgment Schema校验。

## 4. Prompt构建

Prompt由以下部分组合：

1. 当前Benchmark版本、目标维度和全部细则ID。
2. 每条细则的定义、0/1/2锚点和必要证据。
3. 样本可见输入。
4. 抽帧盲区和不可推断项。
5. 严格JSON Schema。

视频Judge不应看到：产品名称、模型版本、历史人工结果、预期输赢和其他样本结论。任务完成Judge可以看到Prompt；纯观感Judge是否看到Prompt由Benchmark明确规定。

## 5. 输出与重试

- 保存完整stdout、stderr、退出码、耗时和Prompt哈希。
- 只接受可解析且通过Schema校验的JSON。
- 每个维度必须恰好返回合同中全部`criterion_id`；缺失、重复或额外ID都使整条Case失败并重试。
- 非JSON、缺字段或引用不存在帧时重试。
- 多次失败记录Judge error，不填默认分数。
- 对隐藏重复样本计算自一致性，偏差过大时降低Judge版本可信度。

## 6. 人工反馈转写

Normalizer输出：

```text
mapping_status = existing | partial | unmapped | needs_clarification
normalized_labels
dimension_proposal
ai_error_type
learning_targets
normalizer_confidence
```

人工没有提供时间段、ROI或具体错误时，字段保持空；不能由Codex补造。低置信度记录可进入人工评测池，但不进入学习池。

## 7. 学习方式

优先顺序：

1. 版本化Rubric和Prompt。
2. 可检索的正反例库。
3. 人工纠错样本驱动的Few-shot。
4. 数据充分后再考虑SFT、LoRA或偏好训练。

任何更新创建Challenger，不能热修改当前生产Prompt。当前Judge只会读取`var/feedback/learning.train.jsonl`中匿名的人工锚点；Calibration和Holdout在代码层禁止进入Judge Prompt。
