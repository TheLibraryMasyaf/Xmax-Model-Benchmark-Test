# 实时生成

本文只说明实时 SDK测试Harness、录制、事件和运行指标采集；不定义R1/R2的具体阈值。

## 1. 运行形态

XMAX实时能力通过浏览器JavaScript SDK和WebRTC运行。`realtime-harness/src/harness.js`由Playwright运行，Python `BrowserRealtimeHarness`负责计划、进程控制、产物登记和后续评测。

三种输入方式：

- `connectMedia`：固定Feed文件；标准轨迹触控配方传入从Feed视频随机抽取的静帧，其他实时配方可传完整视频。
- `connectCamera`：真实摄像头，设备和真实链路测试。
- `connect`：自定义MediaStream，受控帧率、合成流和故障注入。

不能默认把 SDK直接载入纯Node CLI；浏览器能力是设计前提。Harness优先使用当前官方`connectMedia(Blob|URL, {context, render, audio})`和`stopGeneration()`，同时兼容仓库锁定的0.1.x `connect(MediaStream, {initialState})`/`stop()`形态。SDK升级后先运行`npm run check`和单Case smoke。

模式由Operation Recipe决定：互动Prompt默认实时，非互动玩法默认离线；用户或Agent可在Run Request中显式覆盖，但不支持的组合必须在计划阶段报错。

对`realtime-track-interaction@0.3.0`，Python Harness在启动浏览器前按`seeded_random_safe_window_v1`抽取Feed静帧：时间戳位于时长10%–90%之间，种子由Case ID和Feed内容哈希决定。JPEG产物按内容哈希缓存，再转为无音轨的静态H.264流输入SDK。Run原始事件和metrics必须保存实际截图的Artifact URI、SHA-256、抽帧时间戳、原Feed ID和策略版本，供评测与飞书回读。

## 2. 会话流程

```text
创建client并注册回调
→ 记录connect调用时刻
→ connectMedia/connectCamera/connect
→ 记录Promise完成
→ 读取session uid与media设置
→ onRemoteStream取得远程流
→ 记录首个解码帧
→ start/set/sendTracks执行测试事件
→ stopGeneration
→ 等待video_completed（若有）
→ disconnect
```

`set()`更新当前上下文；`start(context?)`创建新任务。重复测试不得把多次`set()`误当成独立任务。

## 3. 必须注册的回调

- `onRemoteStream`：输出流可用或重新绑定。
- `onStateChange`：`idle`、`running`、`disconnected`。
- `onError`：连接或会话错误。
- `onDisconnect`：稳定断开原因。
- `onRoomEvent`：如 `video_completed`。

所有回调写入单调时钟时间戳和墙钟时间，不只打印控制台。Harness也保存浏览器日志，并对能观测的`RTCPeerConnection`定期调用`getStats()`；若供应商SDK将PeerConnection隔离在不可见上下文，`rtc_log`可为空，相关R维度必须标为不可评，不用固定RTT填充。

## 4. 逐帧采集

对输入和远程输出分别保存：

- 每帧媒体时间和到达时间。
- 帧哈希或低成本相似度特征。
- 实际解码宽高。
- 录制结果视频。
- 轨迹、Prompt更新和状态事件。

RTC日志约两秒一次，只适合趋势；冻结、重复帧和短时响应需要逐帧数据。

浏览器对输出帧计算8×8感知哈希。每个离散交互起点保存发送时刻和发送前基准哈希，并把首个达到差异阈值的后续输出帧记为`firstOutputChangeMs`。Python据此派生首次变化、P95、延迟趋势、事件堆积，以及重复帧率和最长冻结时段。该事实只测“何时出现可见变化”，不证明变化是否正确；语义跟手准确性由R2结合轨迹与画面证据评分。

## 5. R指标所需原始事实

| 指标族 | Harness必须提供 |
| --- | --- |
| 启动 | connect调用、连接完成、远程流、首帧、首个有效结果时间 |
| 响应 | set/start/sendTracks时刻与输出首次对应变化 |
| 跟手 | 输入姿态/轨迹和输出姿态/轨迹时间序列 |
| 流畅 | 帧到达间隔、有效FPS、重复、冻结、RTC收发统计 |
| 长会话 | 分窗口帧、身份/质量特征、延迟、FPS和错误趋势 |
| 空间 | 输入输出的主体、相机、锚点、尺度和遮挡轨迹 |
| 恢复 | 异常注入、错误、断开、重连、新首帧和稳定恢复时间 |
| 设备网络 | 分辨率、码率、丢包、RTT、浏览器和系统采集指标 |

具体名称、门槛和计分均由Benchmark更新后决定。

## 6. 受控事件脚本与轨迹控制

事件脚本与Feed素材分离，至少支持：Prompt切换、参考图切换、轨迹开始/结束、动作开始/停止、遮挡、离场/入场、镜头移动、光照变化、多人进入、网络限速/断连、前后台切换。

每个事件保存 `event_id`、计划时刻、实际执行时刻、参数和执行结果。

轨迹实现遵循官方SDK合同：

- 有`remoteContainer`时可启用`render.drag.enabled`，用`onStart/onEnd`记录交互边界。
- 自动化或自定义区域调用`sendTracks()`；互动期间约以30 FPS发送当前全部触点。
- 默认`pointer-track-30fps-v2`按Case ID生成4–6条可复现的单指滑动；不同重复Case使用不同种子，同一Case续跑保持轨迹不变。
- 随机滑动覆盖不同方向、距离、时长、间隔、速度曲线、轻微弧线和手部微抖，并保留画面边缘安全区；它只模拟拖动，不混入点击或长按。
- 单指是`[[x,y]]`，多指是`[[x1,y1],[x2,y2],...]`。
- 坐标基于`session.media.streamSetting`的内容分辨率，不是DOM容器尺寸；范围从`[0,0]`到`[width-1,height-1]`。
- 轨迹不保证每次送达，且没有运行中的任务时会被忽略，所以必须保存发送事实并从输出逐帧数据测量实际响应。

事件格式至少包含`context_set`、`task_start`、`drag_start`、`tracks_frame`、`drag_end`和`task_stop`。每个随机滑动都有稳定`swipe_id`；每个`tracks_frame`保存轨迹阶段、映射后内容坐标、全部触点、发送结果以及输出首次对应变化时间。Harness会补发当前调度周期内全部到期帧，避免短滑动因单次循环只发一帧而被截断。

## 7. 音频

实时回归必须显式配置：

```js
audio: { publish: true, subscribe: true }
```

`connectMedia()`或`connect()`可以发布输入流中的有效音轨，Harness会请求订阅远程音轨，并将SDK返回的整个远端`MediaStream`交给`MediaRecorder`。当前SDK接口支持音频订阅，但实测实时服务返回的远端流没有音频轨，因此录制器无法凭空补录。每个Run必须同时记录`subscribe_requested`与远端实际音轨数：请求订阅后远端仍无音轨时G3不适用；未来远端提供音轨后恢复检查音轨完整性和音画同步。若远端已有音轨而录制结果丢失，则仍按录制/输出失败扣分。

## 8. 重复与清理

- 一个重复任务使用独立 `run_id`和task标识。
- Feed视频自动循环时，Harness必须限定单轮时间窗。
- 每轮结束调用`stopGeneration`，必要时等待收尾事件。
- 完成测试后调用`disconnect`并释放SDK管理的媒体。
- 非主动断开创建新Session，不复用断开的对象。

本项目的常规测试对象是固定`Feed × Prompt`结果，所以默认配方使用`connectMedia`保证可重复。`connectCamera`只用于显式授权的真机/摄像头补充测试，不得忽略Feed后冒充常规Case。
