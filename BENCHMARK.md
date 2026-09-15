# XMAX Benchmark

> 状态：**Shadow / 暂定版**。本版本从 [XMAX场景化评测标准](../XMAX场景化评测标准.md) 录入，用于校准新的 P/G/E/R 体系。历史结果继续绑定旧 Benchmark；旧 Run 需要复评时使用 Replay，不改写历史结果。

<!-- XMAX-BENCHMARK-CONTRACT:BEGIN -->
```json
{
  "$schema": "./schemas/benchmark.schema.json",
  "schema_version": "1.2",
  "benchmark_version": "0.4.0-draft",
  "status": "shadow",
  "provisional": true,
  "review_after": "first_round_pger_calibration",
  "source_document": "../XMAX场景化评测标准.md",
  "source_sha256": "e8938a41f62d35fb336d1456c948a2d008c5d8f2b44d368b97399dce92e18319",
  "scoring_method": {
    "criterion_scale": [0, 1, 2],
    "dimension_raw_score": "mean(applicable criterion scores) / 2",
    "weighted_score": "sum(dimension_raw_score * effective_scene_weight)",
    "final_score_scale": "0_to_100",
    "not_applicable_policy": "remove_from_numerator_and_denominator_then_renormalize",
    "invalid_result_policy": "P.1=0 blocks the single-video score at 0",
    "batch_metric_policy": "P.2, P.3, P.4, RP.1, RP.2 and RP.3 are report-only"
  },
  "dimensions": [
    {
      "dimension_id": "P", "version": "0.4.0-draft", "name": "模型性能与基础可用性", "parent_dimension": null, "status": "shadow",
      "definition": "P.1只判断单次结果是否存在、可解码、可播放且包含有意义的生成画面；其他P指标按冻结批次统计。", "applicable_modes": ["both"], "score_type": "gate_only",
      "required_evidence": ["generation_run", "result_media", "result_evidence"],
      "judge_routing": {"primary_kinds": ["metric", "cv"], "secondary_kinds": ["mlmm"], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "P.1", "name": "单次结果有效性", "definition": "结果必须存在、可解码、可播放并包含有意义的生成画面。Prompt未生效、Feed直出或修改对象错误不属于媒体无效。", "score_type": "gate_0_or_2", "anchors": {"0": {"description": "一票否决：没有结果、文件损坏、全程黑屏/空白/错误页面，或实时会话始终没有有效生成画面。"}, "2": {"description": "结果存在、可解码、可播放并包含有意义的生成画面；生成要求是否正确由E类另评。"}}}
      ]
    },
    {
      "dimension_id": "G1", "version": "0.3.0-draft", "name": "视频基础质量", "parent_dimension": "G", "status": "shadow",
      "definition": "只评价可客观检测的清晰度、曝光、编码、画幅与画面完整性；自然度、协调性和AI味由E4评价。", "applicable_modes": ["both"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["result_video", "decoded_frames", "stream_metadata"],
      "judge_routing": {"primary_kinds": ["cv"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "G1.1", "name": "清晰度、曝光与可辨识细节", "definition": "主体及目标效果的关键细节应持续清楚可辨。", "score_type": "integer_0_2", "anchors": {"0": {"description": "主体或关键区域长期严重模糊、过曝或欠曝，无法辨认人物、动作或目标效果。"}, "1": {"description": "核心内容可辨，仅有短暂模糊、轻微噪声或曝光偏差。"}, "2": {"description": "全程清晰度和曝光稳定，运动中关键细节仍可辨。"}}},
        {"criterion_id": "G1.2", "name": "编码、画幅与画面完整性", "definition": "编码、尺寸、画幅和画面边界应持续正确。", "score_type": "integer_0_2", "anchors": {"0": {"description": "大面积压缩块、色带、画幅错误、异常裁切或尺寸跳变明显影响观看。"}, "1": {"description": "编码和画幅总体正确，仅有轻微压缩痕迹或一次非关键裁切。"}, "2": {"description": "编码稳定，画幅、尺寸和边界全程正确，无明显压缩伪影。"}}}
      ]
    },
    {
      "dimension_id": "G2", "version": "0.3.0-draft", "name": "视频时序质量", "parent_dimension": "G", "status": "shadow",
      "definition": "评价逐帧更新与客观时序伪影；实时结果还结合帧到达时间和RTC事实。", "applicable_modes": ["both"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["result_video", "decoded_frames", "realtime_frame_timestamps"],
      "judge_routing": {"primary_kinds": ["cv"], "secondary_kinds": ["metric"], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "G2.1", "name": "有效帧更新、掉帧与冻结", "definition": "画面应持续更新，不出现明显重复、跳帧或冻结。", "score_type": "integer_0_2", "anchors": {"0": {"description": "频繁冻结、重复帧或大段跳帧，动作呈幻灯片或明显断裂。"}, "1": {"description": "整体连续可看，仅有少量掉帧、一次短暂冻结或少量重复帧。"}, "2": {"description": "全程持续平滑更新，无可感知冻结、重复或异常跳帧。"}}},
        {"criterion_id": "G2.2", "name": "闪烁、抖动、拖影与重影", "definition": "主体、背景和边缘在运动中应保持时序稳定。", "score_type": "integer_0_2", "anchors": {"0": {"description": "持续闪烁、抖动，或快速运动出现大面积拖影和多重轮廓。"}, "1": {"description": "整体稳定，仅有局部短暂闪烁、轻微抖动或少量运动拖影。"}, "2": {"description": "快速运动和镜头移动中也无明显闪烁、抖动、拖影或重影。"}}}
      ]
    },
    {
      "dimension_id": "G3", "version": "0.3.0-draft", "name": "音频完整性与同步", "parent_dimension": "G", "status": "shadow",
      "definition": "主要Prompt视频带有效音轨时默认遵循Prompt视频，否则遵循Feed；Operation Recipe可覆盖。两者均无音轨，或实时SDK已请求订阅但远端输出流没有音频轨时不适用。", "applicable_modes": ["both"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["result_audio", "expected_audio_source", "audio_timeline"],
      "judge_routing": {"primary_kinds": ["metric"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "G3.1", "name": "音频有无、完整性与来源", "definition": "应有音频时必须保留完整、正确来源的音轨。", "score_type": "integer_0_2", "not_applicable_when": "Feed和Prompt视频均无应保留音轨，或实时SDK远端输出流没有音频轨", "anchors": {"0": {"description": "应有音频却无音轨、长期静音、严重截断，或音频来自错误输入。"}, "1": {"description": "来源正确且主体内容完整，但有短暂静音、轻微截断或非关键噪声。"}, "2": {"description": "音轨完整且来源正确，无异常静音、重复、截断或明显污染。"}}},
        {"criterion_id": "G3.2", "name": "音画同步与时间漂移", "definition": "声音应与口型、动作、明确节拍或声明的源时间轴保持同步。", "score_type": "integer_0_2", "not_applicable_when": "没有可判断同步关系的音轨或视觉事件", "anchors": {"0": {"description": "声音与可见事件或源时间轴持续明显错位，或偏移不断扩大。"}, "1": {"description": "大部分时间同步，仅有短暂或轻微偏差且不累积。"}, "2": {"description": "从开始到结束持续同步，无可感知偏移或累计漂移。"}}}
      ]
    },
    {
      "dimension_id": "E1", "version": "0.4.0-draft", "name": "Prompt文字遵循", "parent_dimension": "E", "status": "shadow",
      "definition": "判断Prompt文字要求做什么以及是否做对，包括目标动作、表情和动态变化；不以参考相似度或画面美观代替。", "applicable_modes": ["both"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["prompt_text", "feed_evidence", "result_evidence"],
      "judge_routing": {"primary_kinds": ["mlmm"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "E1.1", "name": "目标对象与操作类型", "definition": "应对正确人物、物体或区域执行指定操作。", "score_type": "integer_0_2", "anchors": {"0": {"description": "没有执行核心操作，或修改了错误对象、区域或操作类型。"}, "1": {"description": "核心对象和操作正确，但存在局部漏改或短暂作用错误。"}, "2": {"description": "全程对正确对象完整执行指定操作。"}}},
        {"criterion_id": "E1.2", "name": "属性、数量与关系", "definition": "颜色、身份、数量、位置、归属和对象关系应正确。", "score_type": "integer_0_2", "anchors": {"0": {"description": "关键属性、数量、位置、归属或对象关系大面积错误。"}, "1": {"description": "主要属性和关系正确，仅有一个次要偏差。"}, "2": {"description": "对象属性、数量、位置、归属和关系均正确。"}}},
        {"criterion_id": "E1.3", "name": "多阶段内容与顺序", "definition": "所有要求阶段应完整出现并保持正确顺序。", "score_type": "integer_0_2", "not_applicable_when": "Prompt不包含多阶段要求", "anchors": {"0": {"description": "关键阶段缺失、顺序颠倒，或只执行开始部分。"}, "1": {"description": "主要阶段完整且顺序正确，但一个次要阶段不完整。"}, "2": {"description": "所有阶段完整出现，顺序、触发时机和持续时间均正确。"}}},
        {"criterion_id": "E1.4", "name": "目标动作、表情与动态变化", "definition": "Prompt明确要求的新动作、表情、口型、姿态或动态变化应按内容和时机完整出现；未要求修改的Feed原动作由E2.2评价。", "score_type": "integer_0_2", "not_applicable_when": "Prompt不包含动作、表情、口型、姿态或其他动态变化要求", "anchors": {"0": {"description": "目标动态没有出现、类型明显错误，或主要动作/表情与指令相反。"}, "1": {"description": "主要动态正确，但幅度、细节、时机或持续时间存在局部偏差。"}, "2": {"description": "目标动作、表情、口型、姿态或动态变化在内容、方向、时机、幅度和持续时间上均符合指令。"}}}
      ]
    },
    {
      "dimension_id": "E2", "version": "0.4.0-draft", "name": "Feed遵循与编辑边界", "parent_dimension": "E", "status": "shadow",
      "definition": "分别评价目标区域完成度与边界、Feed动态与时序结构、非目标内容与视觉属性保持，三项不重复扣分。", "keywords": ["编辑边界", "漏改", "误改", "非目标保持"], "applicable_modes": ["both"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["feed_evidence", "prompt_text", "result_evidence"],
      "judge_routing": {"primary_kinds": ["mlmm"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "E2.1", "name": "目标区域完成度与空间编辑边界", "definition": "只评价目标人物、部位或区域是否改全、原内容是否残留，以及修改是否越过空间边界；不评价原动作和镜头时序。", "score_type": "integer_0_2", "anchors": {"0": {"description": "目标区域大面积未修改或残留，或修改明显扩散到非目标区域。"}, "1": {"description": "目标主体修改完整，只有少量边缘残留、局部漏改或轻微越界。"}, "2": {"description": "目标修改完整且无明显残留，空间边界准确，非目标区域保持原样。"}}},
        {"criterion_id": "E2.2", "name": "Feed动态与时序结构保持", "definition": "只评价未要求修改的动作轨迹、主体位置、场景空间关系、镜头、构图、剪辑和事件顺序是否延续；不评价局部空间边缘。", "score_type": "integer_0_2", "anchors": {"0": {"description": "本应保留的动态、空间关系、镜头、剪辑或事件顺序被持续改写或丢失。"}, "1": {"description": "核心动态与时间结构保留，但局部节奏、位置、构图或镜头连续性轻微偏差。"}, "2": {"description": "要求保留的动态、空间关系、镜头、构图、剪辑和事件顺序均准确延续。"}}},
        {"criterion_id": "E2.3", "name": "非目标内容与视觉属性保持", "definition": "未被要求修改的人物、物体、背景、文字、标识及其颜色、纹理、身份和局部细节应保持不变。", "score_type": "integer_0_2", "anchors": {"0": {"description": "非目标主体或背景被大面积重绘、身份改变、消失，或关键文字与标识被篡改。"}, "1": {"description": "非目标内容总体保持，仅有短暂或非关键的颜色、纹理、文字或局部细节漂移。"}, "2": {"description": "所有非目标内容和视觉属性全程保持，且不随目标替换发生连带变化。"}}}
      ]
    },
    {
      "dimension_id": "E3", "version": "0.3.0-draft", "name": "Prompt素材遵循", "parent_dimension": "E", "status": "shadow",
      "definition": "当前每次只评价一份Prompt图片或视频；同一素材可含一个或多个参考角色，并按指令绑定到一个或多个Feed目标角色。", "applicable_modes": ["both"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["prompt_reference", "feed_evidence", "prompt_text", "result_evidence"],
      "judge_routing": {"primary_kinds": ["mlmm"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "E3.1", "name": "单一素材中的角色与Feed目标绑定", "definition": "同一Prompt素材中的一个或多个参考角色应分别绑定到Feed中指定的一个或多个目标角色，不串人、不漏换、不影响未指定角色。", "score_type": "integer_0_2", "not_applicable_when": "没有Prompt图片或视频素材", "anchors": {"0": {"description": "素材基本未采用，或参考角色大面积错绑、漏绑、串人，未指定角色也被替换。"}, "1": {"description": "主要绑定正确，但一个目标角色短暂错绑、漏换或遮挡后串人。"}, "2": {"description": "所有参考角色准确绑定对应目标，多角色运动、换位和遮挡时也不漏换、不串人。"}}},
        {"criterion_id": "E3.2", "name": "参考特征还原", "definition": "人物、服装、动作、场景、风格或特效的标志性特征应准确呈现。", "score_type": "integer_0_2", "not_applicable_when": "没有Prompt图片或视频素材", "anchors": {"0": {"description": "参考关键特征大面积缺失，结果难以辨认目标参考。"}, "1": {"description": "主要参考特征可辨，但局部还原不足。"}, "2": {"description": "参考中的标志性人物、服装、动作、场景、风格或特效均准确还原。"}}}
      ]
    },
    {
      "dimension_id": "E4", "version": "0.4.0-draft", "name": "自然度、结构与时序完成度", "parent_dimension": "E", "status": "shadow",
      "definition": "集中评价主观自然度、形体结构、视觉融合、时序完成度和跨视角几何一致性；物理接触和环境反馈由E5评价。", "applicable_modes": ["both"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["feed_evidence", "prompt", "result_evidence"],
      "judge_routing": {"primary_kinds": ["mlmm"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "E4.1", "name": "人体、物体与实例结构", "definition": "形体结构、部件数量和实例数量应合理稳定；接触、支撑和穿透关系由E5.1评价。", "score_type": "integer_0_2", "anchors": {"0": {"description": "持续出现多肢、关节反折、融化或实例无故复制/消失。"}, "1": {"description": "整体结构合理，仅在快速运动或遮挡时出现一次局部瑕疵。"}, "2": {"description": "人体、物体和实例数量全程结构合理，在运动和遮挡中也稳定。"}}},
        {"criterion_id": "E4.2", "name": "视觉融合、材质与主观自然度", "definition": "边缘、光影、阴影、反射和材质应与现场协调。", "score_type": "integer_0_2", "anchors": {"0": {"description": "边缘像贴图，光影、反射或材质持续冲突，AI生成感强。"}, "1": {"description": "整体融合自然，仅有局部边缘、光色、阴影或材质短暂不协调。"}, "2": {"description": "边缘、光影、材质及现场关系自然协调，无明显贴图感或AI味。"}}},
        {"criterion_id": "E4.3", "name": "时序连续、效果持续与恢复", "definition": "身份、动作和效果应持续，遮挡或重新出现后应保持或恢复。", "score_type": "integer_0_2", "anchors": {"0": {"description": "身份、动作或效果频繁跳变、丢失；遮挡或重新入镜后无法恢复。"}, "1": {"description": "整体连续，存在一次短暂跳变、减弱或恢复延迟，随后恢复。"}, "2": {"description": "身份、动作和效果全程连续，遮挡、转身或出入镜后保持或立即恢复。"}}},
        {"criterion_id": "E4.4", "name": "跨视角几何、尺度与透视一致性", "definition": "镜头移动、景别变化或主体转向时，生成内容的三维结构、相对尺度、朝向、遮挡层级和透视关系应连续可信。", "score_type": "integer_0_2", "anchors": {"0": {"description": "视角变化后尺度、朝向、形体或空间位置持续矛盾，出现明显二维贴片或几何跳变。"}, "1": {"description": "主要几何和透视关系正确，仅在一次快速转向、边缘视角或遮挡恢复时有短暂偏差。"}, "2": {"description": "镜头移动、景别变化和主体转向中，结构、尺度、朝向、遮挡层级和透视全程一致。"}}}
      ]
    },
    {
      "dimension_id": "E5", "version": "0.4.0-draft", "name": "物理合理性与环境互动", "parent_dimension": "E", "status": "shadow",
      "definition": "评价生成内容与人物、物体和环境之间是否满足可见的接触、支撑、碰撞、运动、材质与环境反馈规律；属于实时与离线通用标准。", "applicable_modes": ["both"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["feed_evidence", "prompt", "result_evidence"],
      "judge_routing": {"primary_kinds": ["mlmm"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "E5.1", "name": "接触、支撑、碰撞与不可穿透", "definition": "生成主体或效果与人物、物体和地面发生关系时，应满足接触位置、承托、碰撞、不可穿透和遮挡层级。", "score_type": "integer_0_2", "not_applicable_when": "画面中不存在且任务不要求接触、支撑或碰撞关系", "anchors": {"0": {"description": "持续漂浮、下陷、穿透，接触位置明显错误，或碰撞后互相无视。"}, "1": {"description": "主要物理关系正确，但快速运动或边缘接触时有一次轻微悬空、穿插或反馈不足。"}, "2": {"description": "接触点、支撑、碰撞、不可穿透和遮挡层级在静止与运动中均持续正确。"}}},
        {"criterion_id": "E5.2", "name": "运动规律、作用结果与状态演化", "definition": "生成内容的速度、惯性、重力、形变及事件前中后状态应与可见作用和前序状态相符。", "score_type": "integer_0_2", "not_applicable_when": "任务和画面均不包含可判断的运动、作用或状态变化", "anchors": {"0": {"description": "运动无视重力或惯性，作用没有合理结果，或状态无因跳变、倒退和复原。"}, "1": {"description": "主要运动方向和事件结果合理，但速度、轨迹、形变或状态持续时间存在局部偏差。"}, "2": {"description": "运动方向、速度变化、惯性、重力、形变和事件状态均与可见作用及前序状态连续一致。"}}},
        {"criterion_id": "E5.3", "name": "材质响应、自然现象与环境反馈", "definition": "火焰、烟雾、液体、布料、光照、阴影、反射及受影响环境应产生符合场景条件的变化。", "score_type": "integer_0_2", "not_applicable_when": "任务和画面均不包含可判断的材质、自然现象或环境反馈", "anchors": {"0": {"description": "材质或自然现象行为明显错误，且环境对强光、火焰、碰撞或液体等作用没有应有反馈。"}, "1": {"description": "主要材质与环境反馈存在，但强度、方向、范围、延迟或衰减不完全匹配。"}, "2": {"description": "材质形变、自然现象及环境光影、遮挡和受力反馈均随主体、镜头和场景条件合理变化。"}}}
      ]
    },
    {
      "dimension_id": "R1", "version": "0.3.0-draft", "name": "交互响应速度", "parent_dimension": "R", "status": "shadow",
      "definition": "只评价操作后的画面变化快不快，不因对象或效果错误扣分；保留原始毫秒事实。", "applicable_modes": ["realtime"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["interaction_events", "output_change_timestamps"],
      "judge_routing": {"primary_kinds": ["metric"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "R1.1", "name": "单次操作首次响应", "definition": "操作发出后应尽快出现对应画面变化。", "score_type": "integer_0_2", "anchors": {"0": {"description": "操作后长期没有反馈，或延迟已使互动失去意义。"}, "1": {"description": "能得到反馈，但存在可感知等待，主要互动仍可完成。"}, "2": {"description": "操作后快速出现画面变化，无明显等待。"}}},
        {"criterion_id": "R1.2", "name": "连续操作与状态切换响应", "definition": "连续输入时延迟不应堆积或持续增加。", "score_type": "integer_0_2", "not_applicable_when": "未执行连续交互实验", "anchors": {"0": {"description": "响应大量堆积、顺序错乱，或延迟持续累积。"}, "1": {"description": "连续操作基本可用，偶有一次切换偏慢或短暂堆积。"}, "2": {"description": "连续操作和多次切换均及时响应，长时间交互也不累积延迟。"}}}
      ]
    },
    {
      "dimension_id": "R2", "version": "0.3.0-draft", "name": "交互准确性", "parent_dimension": "R", "status": "shadow",
      "definition": "只评价交互是否正确触发、绑定和跟随，不因响应较慢扣分。", "applicable_modes": ["realtime"], "score_type": "mean_applicable_criteria_0_2",
      "required_evidence": ["interaction_events", "feed_evidence", "result_evidence"],
      "judge_routing": {"primary_kinds": ["mlmm"], "secondary_kinds": [], "fallback_policy": "no_automated_judge"},
      "criteria": [
        {"criterion_id": "R2.1", "name": "触发与作用对象", "definition": "操作应被正确识别并作用于正确人物、物体或区域。", "score_type": "integer_0_2", "anchors": {"0": {"description": "大量漏触发、误触发、串人，或持续作用于错误对象。"}, "1": {"description": "主要触发和对象正确，偶有一次漏触发、误触发或短暂绑定错误。"}, "2": {"description": "所有关键操作均正确触发并持续作用于正确对象。"}}},
        {"criterion_id": "R2.2", "name": "位置、方向与幅度跟随", "definition": "生成内容应准确跟随输入的位置、方向、速度和幅度。", "score_type": "integer_0_2", "anchors": {"0": {"description": "位置、方向或运动幅度持续与输入相反或无关。"}, "1": {"description": "主要方向和位置正确，但有局部偏移、幅度不足或短暂跟丢。"}, "2": {"description": "位置、方向、速度和幅度持续准确跟随输入。"}}},
        {"criterion_id": "R2.3", "name": "连续状态控制", "definition": "状态应按操作准确切换、保持和结束。", "score_type": "integer_0_2", "not_applicable_when": "未执行连续状态控制实验", "anchors": {"0": {"description": "状态切换错误、丢失或自行跳转，连续控制无法完成。"}, "1": {"description": "主要状态正确，偶有一次切换遗漏、短暂回退或保持不足。"}, "2": {"description": "状态按操作准确切换并保持，不丢失、不回退、不自行跳转。"}}}
      ]
    }
  ],
  "reporting_metrics": [
    {"metric_id": "P.2", "version": "0.3.0-draft", "name": "批次生成成功与失败", "scope": "frozen_run_batch", "applicable_modes": ["both"], "definition": "统计计划Run数、结果返回、有效视频、无效输出、首次成功、重试、耗时和失败原因；不赋0/1/2，不回灌单视频。"},
    {"metric_id": "P.3", "version": "0.3.0-draft", "name": "同输入重复稳定性", "scope": "repeat_group", "applicable_modes": ["both"], "definition": "在同一模型版本、模式、Feed、Prompt文字、Prompt素材和参数下，按冻结Run Request的repeat_count统计核心任务成立率、质量离散度和失败模式；默认5次但可配置，不写死。"},
    {"metric_id": "P.4", "version": "0.4.0-draft", "name": "离线生成时效性", "scope": "frozen_offline_batch", "applicable_modes": ["offline"], "definition": "分别统计排队、模型生成、结果下载/传输和端到端交付时间，并计算生成耗时与输出成片时长之比RTF；不赋0/1/2，不回灌单视频。"},
    {"metric_id": "RP.1", "version": "0.4.0-draft", "name": "启动与画面交付", "scope": "frozen_realtime_batch", "applicable_modes": ["realtime"], "definition": "统计会话建立、远程首帧、首个有意义效果、首个稳定结果、有效FPS、掉帧、重复帧和冻结的覆盖率与分布。"},
    {"metric_id": "RP.2", "version": "0.3.0-draft", "name": "稳定与恢复", "scope": "frozen_realtime_batch", "applicable_modes": ["realtime"], "definition": "统计异常断开、自动恢复、恢复耗时、长会话存活及开始/中段/末段累计退化。"},
    {"metric_id": "RP.3", "version": "0.4.0-draft", "name": "持续端到端时延与抖动", "scope": "frozen_realtime_batch", "applicable_modes": ["realtime"], "definition": "在冻结网络配置下统计输入采集至生成画面呈现的持续端到端时延P50/P95/P99、抖动、超阈值占比和随会话时长的漂移。"}
  ],
  "weight_profiles": [
    {"profile_id": "scene-base-offline-0.4", "version": "0.4.0-draft", "status": "shadow", "applicable_modes": ["offline"], "description": "仅供场景规则覆盖的实现基底，不构成通用权重。", "implementation_base_only": true, "weights": {"G1": 1, "G2": 1, "G3": 1, "E1": 1, "E2": 1, "E3": 1, "E4": 1, "E5": 1}, "maximum_rule_multiplier": 100},
    {"profile_id": "scene-base-realtime-0.4", "version": "0.4.0-draft", "status": "shadow", "applicable_modes": ["realtime"], "description": "仅供场景规则覆盖的实现基底，不构成通用权重。", "implementation_base_only": true, "weights": {"G1": 1, "G2": 1, "G3": 1, "E1": 1, "E2": 1, "E3": 1, "E4": 1, "E5": 1, "R1": 1, "R2": 1}, "maximum_rule_multiplier": 100}
  ],
  "scene_weight_rules": [
    {"rule_id": "core-indoor-selfie-person-replacement-offline", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-offline-0.4"], "when": {"all": [{"scenario_id": "core-indoor-selfie-person-replacement"}, {"mode": "offline"}]}, "weight_overrides": {"G1": 12, "G2": 8, "G3": 4, "E1": 15, "E2": 15, "E3": 19, "E4": 22, "E5": 5}},
    {"rule_id": "core-indoor-selfie-person-replacement-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-indoor-selfie-person-replacement"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 10, "G2": 7, "G3": 3, "E1": 14, "E2": 11, "E3": 17, "E4": 18, "E5": 4, "R1": 6, "R2": 10}},
    {"rule_id": "core-outdoor-complex-person-replacement-offline", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-offline-0.4"], "when": {"all": [{"scenario_id": "core-outdoor-complex-person-replacement"}, {"mode": "offline"}]}, "weight_overrides": {"G1": 12, "G2": 10, "G3": 3, "E1": 12, "E2": 13, "E3": 16, "E4": 26, "E5": 8}},
    {"rule_id": "core-outdoor-complex-person-replacement-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-outdoor-complex-person-replacement"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 10, "G2": 8, "G3": 2, "E1": 12, "E2": 11, "E3": 13, "E4": 20, "E5": 7, "R1": 5, "R2": 12}},
    {"rule_id": "core-pet-realtime-interaction-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-pet-realtime-interaction"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 7, "G2": 9, "G3": 3, "E1": 10, "E2": 6, "E3": 7, "E4": 17, "E5": 14, "R1": 10, "R2": 17}},
    {"rule_id": "core-pet-replacement-offline", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-offline-0.4"], "when": {"all": [{"scenario_id": "core-pet-replacement"}, {"mode": "offline"}]}, "weight_overrides": {"G1": 11, "G2": 9, "G3": 3, "E1": 13, "E2": 12, "E3": 19, "E4": 23, "E5": 10}},
    {"rule_id": "core-pet-replacement-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-pet-replacement"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 9, "G2": 7, "G3": 2, "E1": 12, "E2": 9, "E3": 18, "E4": 18, "E5": 8, "R1": 5, "R2": 12}},
    {"rule_id": "core-high-speed-subject-edit-offline", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-offline-0.4"], "when": {"all": [{"scenario_id": "core-high-speed-subject-edit"}, {"mode": "offline"}]}, "weight_overrides": {"G1": 10, "G2": 15, "G3": 5, "E1": 11, "E2": 10, "E3": 15, "E4": 24, "E5": 10}},
    {"rule_id": "core-high-speed-subject-edit-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-high-speed-subject-edit"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 8, "G2": 12, "G3": 4, "E1": 10, "E2": 9, "E3": 12, "E4": 19, "E5": 8, "R1": 6, "R2": 12}},
    {"rule_id": "core-travel-vlog-scene-style-offline", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-offline-0.4"], "when": {"all": [{"scenario_id": "core-travel-vlog-scene-style"}, {"mode": "offline"}]}, "weight_overrides": {"G1": 10, "G2": 13, "G3": 5, "E1": 12, "E2": 15, "E3": 16, "E4": 20, "E5": 9}},
    {"rule_id": "core-multishot-film-replacement-offline", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-offline-0.4"], "when": {"all": [{"scenario_id": "core-multishot-film-replacement"}, {"mode": "offline"}]}, "weight_overrides": {"G1": 9, "G2": 14, "G3": 5, "E1": 16, "E2": 14, "E3": 16, "E4": 18, "E5": 8}},
    {"rule_id": "core-fixed-camera-multi-subject-replacement-offline", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-offline-0.4"], "when": {"all": [{"scenario_id": "core-fixed-camera-multi-subject-replacement"}, {"mode": "offline"}]}, "weight_overrides": {"G1": 9, "G2": 9, "G3": 3, "E1": 18, "E2": 15, "E3": 18, "E4": 21, "E5": 7}},
    {"rule_id": "core-fixed-camera-multi-subject-replacement-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-fixed-camera-multi-subject-replacement"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 8, "G2": 7, "G3": 2, "E1": 16, "E2": 12, "E3": 16, "E4": 18, "E5": 6, "R1": 4, "R2": 11}},
    {"rule_id": "core-moving-camera-added-subject-interaction-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-moving-camera-added-subject-interaction"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 8, "G2": 8, "G3": 2, "E1": 10, "E2": 6, "E3": 10, "E4": 18, "E5": 16, "R1": 8, "R2": 14}},
    {"rule_id": "core-moving-camera-effects-offline", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-offline-0.4"], "when": {"all": [{"scenario_id": "core-moving-camera-effects"}, {"mode": "offline"}]}, "weight_overrides": {"G1": 10, "G2": 10, "G3": 3, "E1": 12, "E2": 10, "E3": 12, "E4": 25, "E5": 18}},
    {"rule_id": "core-moving-camera-effects-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-moving-camera-effects"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 8, "G2": 8, "G3": 2, "E1": 12, "E2": 9, "E3": 10, "E4": 19, "E5": 14, "R1": 7, "R2": 11}},
    {"rule_id": "core-long-single-host-live-replacement-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-long-single-host-live-replacement"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 8, "G2": 11, "G3": 3, "E1": 12, "E2": 12, "E3": 14, "E4": 18, "E5": 5, "R1": 7, "R2": 10}},
    {"rule_id": "core-long-multi-host-live-replacement-realtime", "version": "0.4.0-draft", "status": "shadow", "priority": 100, "applicable_profile_ids": ["scene-base-realtime-0.4"], "when": {"all": [{"scenario_id": "core-long-multi-host-live-replacement"}, {"mode": "realtime"}]}, "weight_overrides": {"G1": 7, "G2": 11, "G3": 3, "E1": 13, "E2": 14, "E3": 13, "E4": 17, "E5": 5, "R1": 6, "R2": 11}}
  ],
  "hard_gates": [
    {"gate_id": "invalid-result-block-score", "version": "0.3.0-draft", "status": "shadow", "condition": {"dimension_id": "P", "criterion_id": "P.1", "score_equals": 0}, "action": {"type": "block_score", "final_verdict": "invalid_result"}}
  ],
  "score_schemas": [
    {"score_schema_id": "xmax-scene-score-0.4", "version": "0.4.0-draft", "status": "shadow",
      "dimensions": [{"dimension_id": "G1", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "G2", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "G3", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "E1", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "E2", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "E3", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "E4", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "E5", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "R1", "aggregation": "mean_applicable_criteria"}, {"dimension_id": "R2", "aggregation": "mean_applicable_criteria"}],
      "weight_profile_by_mode": {"offline": "scene-base-offline-0.4", "realtime": "scene-base-realtime-0.4"}, "require_scene_weight_rule": true, "canonical_score_enabled": false, "outputs": ["scenario_score"], "case_score_output": "scenario_score", "comparison_policy_status": "pending_first_round"}
  ],
  "change_log": [
    {"version": "0.4.0-draft", "date": "2026-09-07", "summary": "Added E1.4 dynamic-instruction adherence, E2.3 non-target preservation, E4.4 cross-view geometry and the E5 physical/environment dimension; added P.4 and RP.3 reporting metrics; reweighted all core scenes and added long single-host and multi-host live replacement scenarios."},
    {"version": "0.3.0-draft", "date": "2026-08-25", "summary": "Replaced C/O/R scoring with P gate, objective G quality, E generation effect and concise realtime R standards; moved P/RP batch performance out of single-video scoring; added 10 core scenes and 16 exact mode-specific weight rules."},
    {"version": "0.2.1-draft", "date": "2026-08-24", "summary": "Historical criterion-level C/O/R Shadow contract retained in persisted results and Git history."}
  ]
}
```
<!-- XMAX-BENCHMARK-CONTRACT:END -->

## 1. 适用范围与当前状态

适用于XMAX基于原视频、摄像头流、Prompt文字及单份Prompt图片或视频素材的离线与实时生成，不包含纯文本直接生成视频。当前`benchmark_version = 0.4.0-draft`，全部合同仍为Shadow。

## 2. 评分方法

每条适用细则按0（差）、1（合格）、2（好）评分。每个标准先取适用细则平均档位，再乘场景权重；不适用项从分子、分母同时剔除，其余权重归一化至100%。P.1为0时Run直接记0。P.2/P.3/P.4和RP.1/RP.2/RP.3只进入批次报告。

本版本不设置脱离场景的通用权重。`scene-base-*`只是解析器的实现基底；只有命中核心场景规则才允许产出场景分，未映射场景应显示`missing_scene_weight_rule`。

## 3. 当前标准清单

| ID | 名称 | 模式 | 细则数 | 单视频权重 |
| --- | --- | --- | ---: | --- |
| P | 模型性能与基础可用性 | both | 1 | 否，仅P.1一票否决 |
| G1 | 视频基础质量 | both | 2 | 是 |
| G2 | 视频时序质量 | both | 2 | 是 |
| G3 | 音频完整性与同步 | both | 2 | 条件适用 |
| E1 | Prompt文字遵循 | both | 4 | 是 |
| E2 | Feed遵循与编辑边界 | both | 3 | 是 |
| E3 | Prompt素材遵循 | both | 2 | 有Prompt素材时适用 |
| E4 | 自然度、结构与时序完成度 | both | 4 | 是 |
| E5 | 物理合理性与环境互动 | both | 3 | 条件适用 |
| R1 | 交互响应速度 | realtime | 2 | 是 |
| R2 | 交互准确性 | realtime | 3 | 是 |

## 4. 批次报告指标

| ID | 名称 | 范围 | 0/1/2 |
| --- | --- | --- | --- |
| P.2 | 批次生成成功与失败 | 冻结Run Batch | 否 |
| P.3 | 同输入重复稳定性 | 冻结重复组；次数由`repeat_count`决定 | 否 |
| P.4 | 离线生成时效性 | 冻结离线批次；分离排队/生成/传输并报告RTF | 否 |
| RP.1 | 启动与画面交付 | 冻结实时批次 | 否 |
| RP.2 | 稳定与恢复 | 冻结实时批次 | 否 |
| RP.3 | 持续端到端时延与抖动 | 冻结实时批次；网络配置必须一致 | 否 |

## 5. 核心场景权重规则

| Scenario ID | 模式 | G合计 | E合计 | R合计 |
| --- | --- | ---: | ---: | ---: |
| core-indoor-selfie-person-replacement | offline / realtime | 24 / 20 | 76 / 64 | — / 16 |
| core-outdoor-complex-person-replacement | offline / realtime | 25 / 20 | 75 / 63 | — / 17 |
| core-pet-realtime-interaction | realtime | 19 | 54 | 27 |
| core-pet-replacement | offline / realtime | 23 / 18 | 77 / 65 | — / 17 |
| core-high-speed-subject-edit | offline / realtime | 30 / 24 | 70 / 58 | — / 18 |
| core-travel-vlog-scene-style | offline | 28 | 72 | — |
| core-multishot-film-replacement | offline | 28 | 72 | — |
| core-fixed-camera-multi-subject-replacement | offline / realtime | 21 / 17 | 79 / 68 | — / 15 |
| core-moving-camera-added-subject-interaction | realtime | 18 | 60 | 22 |
| core-moving-camera-effects | offline / realtime | 23 / 18 | 77 / 64 | — / 18 |
| core-long-single-host-live-replacement | realtime | 22 | 61 | 17 |
| core-long-multi-host-live-replacement | realtime | 21 | 62 | 17 |

精确权重位于18条`scene_weight_rules`中，均与来源文档第3章一致。G3/E3或E5细则不适用时归一化，不以0分代替。

## 6. Judge职责

- P.1：运行事实验证文件存在，CV验证解码与黑白空帧，MLMM补充判断错误页面或始终无意义画面；任一可信Judge判0即可触发Gate。
- G1/G2：由确定性CV与实时帧事实评价；主观自然度只在E4评价。
- G3：由音频确定性指标评价来源、完整性及可测时间轴对齐；不可测的口型/动作同步标记不可评。实时 SDK 已请求订阅但远端输出流没有音频轨时，G3 标记不适用并归一化其余权重，不按录制缺失音轨扣分。
- E1-E5与R2：MLMM直接返回细则分，Fusion派生标准分。
- R1：由交互事件与可见变化时间戳评价；未执行连续交互实验时R1.2不适用。

## 7. 版本与校准要求

首轮至少检查P Gate误触发、G类CV阈值、G3可评覆盖率、E类Judge与人工盲评一致性、E5与E4重复扣分率、R1/R2证据覆盖、P.4/RP.3仪器覆盖、长时场景前中后段漂移、场景排序和权重敏感性。调整时创建新版本并Replay旧Run。

## 8. 来源与变更记录

来源为 [XMAX场景化评测标准](../XMAX场景化评测标准.md)，源文件SHA-256写入合同。详细来源依据、档位Case、核心场景业务拆解与权重侧重以该文档为准。
