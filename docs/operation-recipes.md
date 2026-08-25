# 玩法与输入操作配方

本文是玩法名、默认Prompt、生成模式、Feed/Prompt API角色、音轨基准和实时操作之间的唯一映射说明。机器配置位于`config/operation-recipes.json`，Schema为`schemas/operation-recipes.schema.json`。

## 1. 为什么必须使用配方

“Feed”和“Prompt素材”是素材管理角色，不等于API里的源视频和参考图。不同玩法的被编辑视频可能相反：

- 图片参考类编辑Feed视频。
- 视频参考类编辑Prompt视频，Feed只提供主体截图。

因此Runner只能使用TestPlan已经冻结的绑定，不能根据扩展名、文件名或历史脚本重新猜测。

同样的合同也必须提供给评测Judge。每条Recipe包含`evaluation_contract`，明确：操作摘要、结果预期、各输入角色的生成语义、必须保留内容和必须改变内容。新建TestPlan时该合同冻结到Case；历史Case没有冻结字段时，Evaluator只允许按相同Recipe ID回读版本化合同，不根据Prompt自由推断。

## 2. 离线图片参考类

适用：换装、换身材、换人、换舞姿、换角色。

```text
refVideoPath = Feed视频
refImagePath = Prompt图片
被编辑视频 = Feed视频
预期音轨来源 = Feed视频
默认模式 = offline
```

实时换装/换角色由用户或Run Request显式选择后，可以使用`connectMedia(Feed)`和`refImageUrl=Prompt图片`；未指定时仍默认离线。

## 3. 离线视频参考类

适用：运镜、对口型、手势舞、舞蹈、换动作、搞怪。

```text
refVideoPath = Prompt视频
refImagePath = Feed视频截图
被编辑视频 = Prompt视频
预期音轨来源 = Prompt视频
默认模式 = offline
```

Feed截图是正式输入Asset，必须记录抽帧时间、哈希和来源Feed。Case上传时`feed文件`同时附原Feed和实际截图。

评测输入必须同时包含原Feed、生成时实际使用的`feed_capture`、Prompt视频和Result，并明确告诉Judge：Prompt视频是被编辑的时间线，Feed截图提供要替换进去的主体身份或外观。评价目标是“Feed截图主体是否正确替换到Prompt视频并跟随其动作/舞蹈/口型/节奏/运镜”，不是“Feed原视频是否学会了Prompt舞蹈”。

## 4. 实时轨迹互动

适用：触控、滑动、拖动、MoX。

```text
默认模式 = realtime
输入 = Feed视频按Case稳定随机截图→connectMedia
操作 = SDK内置drag或sendTracks
```

标准触控配方`realtime-track-interaction@0.3.0`不直接播放原Feed视频。执行器以`case_id + Feed SHA-256`为种子，在视频时长的10%–90%安全窗内随机抽取一帧JPEG；重复Case因ID不同抽到不同帧，同一Case续跑使用同一张。Harness将JPEG封装为无音轨静态H.264输入后交给SDK，并在Run中保存截图URI、哈希、时间戳、原Feed和策略版本。静帧触控没有源音轨，音频保留细则标记为不适用。

自定义轨迹以约30 FPS发送，单指格式为`[[x,y]]`，多指格式为`[[x1,y1],[x2,y2],...]`。坐标基于`session.media.streamSetting`内容分辨率；执行器负责从测试脚本坐标映射到`[0,width-1] × [0,height-1]`。

默认轨迹Profile为`pointer-track-30fps-v2`：每个Case用自身`case_id`作为稳定种子，生成4–6条方向、距离、时长、间隔、曲率和轻微抖动均不同的单指滑动。同一Case中断续跑可复现，重复Case因ID不同而获得不同轨迹；旧`v1`仅保留给已冻结历史Case。

## 5. 实时场景互动

适用：召唤、DimX、以摄像头动作或手势驱动角色互动的Prompt。

```text
默认模式 = realtime
常规固定Feed回归 = connectMedia（显式真机补充测试才可覆盖为connectCamera/connect）
参考图 = Prompt图片（如玩法需要）
操作 = Feed/摄像头中的受控手势事件 + 必要的轨迹脚本
```

人物手势属于输入流事件；网页指针属于`sendTracks()`事件。两者必须使用不同事件类型，不能混为一个“触控”标签。

## 6. 模式解析和覆盖

```text
TestCase显式模式
→ Run Request按玩法覆盖
→ Recipe默认模式
```

用户或Agent可以显式指定模式，但Agent只能引用配方或用户要求，不能仅凭自由文本临场猜测。显式模式不在`allowed_generation_modes`时，计划失败并列出原因；系统不得静默切换。

## 7. 音频合同

配方必须声明`expected_audio_source_role`。评测只检查：

- 输入原音轨存在时，输出是否保留对应内容。
- 起始音画偏移是否合理。
- 中段和结尾是否出现持续漂移。

不评价音乐审美或音色质量。实时Harness显式配置`audio.publish=true`和`audio.subscribe=true`，否则不能声称完成音轨评测。

## 8. 新玩法接入

新增玩法只允许通过新版本Recipe接入：

1. 填稳定`recipe_id`和版本。
2. 填玩法别名与可执行Prompt模板。
3. 指定默认/允许模式。
4. 指定被编辑视频与音轨来源。
5. 指定离线API或实时SDK绑定。
6. 若有互动，引用版本化Interaction Profile。
7. 使用一条fake和一条真实smoke验证后再批量运行。

8. 填写`evaluation_contract`，使无上下文Judge能够区分被编辑视频、参考素材、实际触发事件以及结果应保留/改变的内容。

修改已有语义时创建新Recipe版本，不原地改变已冻结TestPlan的解释。
