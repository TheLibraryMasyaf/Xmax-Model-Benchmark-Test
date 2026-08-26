# Architecture Decisions

本文记录已确定的“为什么”，防止后续Agent重复设计或引入不兼容捷径。

## D1 Benchmark外置且版本化

原因：维度仍会新增、修改、停用。代码硬编码会迫使每次改标准都改数据库和执行器。结论：维度ID始终是字符串，从 `BENCHMARK.md`加载。

## D2 场景只选择预设权重，不由MLLM自由定权

原因：自由权重不可复现、容易结果导向。结论：场景标签来自Scenario Pack/TestPlan；Fusion按已发布规则解析，Codex最多补充带置信度的标签。

## D3 当前Benchmark只发布Scenario Score

原因：当前十个核心业务场景的关注点不同，脱离场景的通用权重会制造不可解释的跨场景总分。结论：每条Run必须命中显式核心场景规则后才输出Scenario Score；Canonical停用并保留空兼容字段，跨场景只比较分项与批次事实。

## D4 硬失败独立于权重

原因：核心目标失败、黑屏或严重崩坏不能靠其他高分抵消。结论：Hard Gate先于权重总分或对总分设置上限。

## D5 原始数据追加写

原因：Prompt、模型、Judge和人工转写会变化，需要重新解释历史。结论：API事件、Codex输出、CV指标和人工原文不可覆盖，派生结果可重建。

## D6 人工是并行监督线

原因：人工不仅纠错，也可提供独立评测集和新维度。结论：Human Signal Hub独立于自动争议队列；单条override与模型学习分离。

## D7 飞书不是事实源

原因：附件、字段、行号和表结构会变化。结论：本地元数据与产物可重建飞书；同步按稳定业务ID和Ledger幂等进行。

## D8 实时测试必须运行在浏览器Harness

原因：官方实时SDK依赖浏览器和WebRTC。结论：TypeScript浏览器Harness负责SDK、录流和RTC采集，Python负责编排与持久化。

## D9 联系图不替代运行事实

原因：静态帧看不到真实FPS、响应、网络和设备状态。结论：运行指标来自API/RTC/系统时间戳，联系图只做视觉证据。

## D10 外部能力必须可Fake

原因：建设Agent不一定有Key、额度、GPU或飞书权限。结论：每个外部Adapter必须有离线fake，真实执行只替换插件配置。

## D11 玩法通过版本化Operation Recipe绑定输入

原因：图片参考和视频参考玩法中Feed/Prompt的API角色相反，文件类型不足以判断哪个视频被编辑。结论：TestPlan必须冻结配方ID、被编辑视频、预期音轨来源和API字段绑定；Runner不得再次猜测。

## D12 模式显式覆盖优先，互动默认实时

原因：同一场景标签不足以决定生成后端。结论：Case/Run Request显式模式优先；否则互动玩法默认实时，其他默认离线；能力不支持时失败而非静默切换。

## D13 Case表一Run一行

原因：重复生成、失败和重试都是独立事实。结论：每个Run使用连续`_XX`后缀独立写入飞书Case表；失败为0%，历史未重评为空；平均和分布只在报告中计算。

## D14 音频基准跟随被编辑视频

原因：图片参考类编辑Feed，视频参考类编辑Prompt视频。结论：音轨保留和音画同步以Operation Recipe声明的`expected_audio_source_asset_id`为基准，不固定看Feed或Prompt。

## D15 阶段通过Manifest解耦

原因：生成和评测可能隔天或由不同Agent执行，也需要评测已有视频。结论：阶段只通过不可变批次、Selector和Stage Manifest交接；Run Request显式列出授权阶段，缺少输入时报错，禁止静默补跑。飞书Case/本地视频通过`ingest results`归一为completed GenerationRun；远程写入由独立`sync_policy`控制。

## D16 MLLM原始媒体按角色分项传入

原因：把Feed、Prompt参考和Result抽帧扁平化会产生角色串位，也会损失完整时序。结论：支持原生视频的Provider默认接收`generation_operation`、`feed`、可选`feed_capture`、`prompt_text`、`prompt_reference_N`和`result_video`等带标签输入；通用截图只服务CV、审计和不支持视频的Provider。原生视频超出Provider本地Base64限制时必须提供可访问URL或明确失败，不能静默压缩或改变证据模式。

## D17 生成操作语义必须进入评测合同

原因：素材管理角色不等于生成API角色；视频参考玩法实际编辑Prompt视频，并使用Feed截图替换其主体。结论：Operation Recipe必须声明并冻结`evaluation_contract`，评测时把它作为独立`generation_operation`文本输入；如生成使用`feed_capture`，还必须把实际截图作为单独视觉输入。Judge先按合同理解各素材职责，再判断Result，不得根据通常的Feed/Prompt含义自行推断。
