# Implementation Status

本文是“如何把当前架构补成完整项目”的唯一工作包清单。组件原理写在 `docs/`，这里只定义依赖、目标文件、公共接口和验收。

“完成整个项目”指实现本文件全部工作包，不是运行一次素材下载/视频生成/评测流程。建设Agent应自行按依赖推进，只有 `AGENTS.md` 明确列出的外部阻塞才向用户提问。

状态：`DONE`、`PARTIAL`、`TODO`、`BLOCKED`。只有满足 `AGENTS.md` Definition of Done才能改为DONE。

## 1. 当前完成度

| 包 | 状态 | 已实现 | 外部接入/后续维护 |
| --- | --- | --- | --- |
| P0 Foundation | DONE | Python包、核心合同、Schema、Benchmark Loader、CLI检查 | 后续随合同扩展维护 |
| P0.5 Persistence | DONE | SQLite迁移、Repository、Artifact Store、追加事件、持久化付费评测预算/预留/审计、Fake/测试 | 随数据合同做只增量迁移 |
| P0.7 Stage Orchestration | DONE | 依赖解析、Selector冻结、Stage/Batch Manifest、续跑、CLI | 新阶段需沿用显式依赖规则 |
| P1 Scene Weighting | DONE | 动态权重解析、Benchmark/Scenario校验、Fusion/Hard Gate集成 | 新权重只通过Benchmark版本发布 |
| P2 Assets | DONE | Sheet/Base/Wiki/本地/HTTP适配器、快照、去重、媒体校验、CLI | 真实来源映射由Asset Source Pack提供 |
| P2.5 Existing Result Ingestion | DONE | 飞书/本地/Manifest导入、Run归一、来源追溯、Fake和CLI | 真实Case字段由Existing Results Pack提供 |
| P3 Planning | DONE | 配方解析、模式优先级、Case后缀、成本预览、可插拔组合策略、可修改重复数、Task Batch冻结 | 新玩法通过Recipe/Profile包接入；新分配法通过StrategyRegistry接入 |
| P3.5 Task Execution | DONE | SQLite原子租约、过期回收、单任务生成→评测→飞书同步→对账、COS真实前检、`evaluation_paused`断点、断点续跑、CLI和全Fake E2E | 真实批量执行仍需预算批准和密钥 |
| P4 Offline Generation | DONE | 真实REST/COS、Session API边界、状态机、续跑、Fake | Session+RTC的真实RTC传输不内置，常规离线路径使用官方REST |
| P5 Realtime Generation | DONE | 新旧SDK兼容的浏览器Harness、录流、逐帧/事件/RTC快照、标准触控按Case稳定随机抽取Feed静帧、版本化互动Profile、4–6条随机用户滑动、Fake | 需Key的付费真实会话待运行时smoke |
| P6 Preprocessing | DONE | Feed/Prompt/Result分组抽帧、事件窗口、ROI、缓存和manifest | 真实运行需`ffmpeg/ffprobe` |
| P7 Judges | DONE | Provider中立MLLM、Qwen多模型视频候选/Codex适配器、Case原子多维评测、15模型免费链、末位`qwen3-vl-flash`付费兜底、180秒可审计超时、99元硬闸门、运行事实Metric、音轨Metric、基础ffmpeg CV与插件边界 | DINOv2/MUSIQ等候选权重尚未注册为启用Judge；专项CV后续以Challenger接入 |
| P8 Evaluation | DONE | Orchestrator、细则级Judge路由与覆盖检查、批次级重复/跨输入评分、硬门槛、双总分、精确批次续跑和结果Schema | R5/R6等仍由实际样本是否具备异常脚本/长会话决定可评性 |
| P9 Human Signals | DONE | 飞书视频+评语导入、不可变原文、Provider中立Normalizer、追加式人工Override、训练/校准/Holdout隔离、MLMM校准包与CV Trainer插件Challenger | 单条反馈不热更新Champion；新版本须经Holdout验证后显式发布 |
| P10 Feishu | DONE | Sheet/Base/Wiki读取，Case Upsert/附件/Ledger/回读/对账，评分百分比转换 | 真库写入与附件回下载待获得明确授权后smoke |
| P11 Release/Replay | DONE | Challenger、Holdout验证、回放、发布和回滚 | 发布仍需明确operator和验证文件 |
| P12 Reporting | DONE | 精确Run/Evaluation Batch的版本对比与单版本报告、Case/维度/细则多统计量、人工修订、P0/P1/P2、JSON/Markdown | 对比阈值随Score Schema版本维护；无阈值时不臆造绝对合格线 |
| P13 Unified CLI | DONE | RUNBOOK命令、`run`内强制ContextChecker、配置内容指纹缓存、稳定退出码、生成预算批准门、`evaluation-budget status/authorize/pause` | 新命令须同步RUNBOOK和E2E |

### 1.1 外部运行就绪度

| 边界 | 代码状态 | 本轮已验证 | 真实运行前输入 |
| --- | --- | --- | --- |
| XMAX离线REST/COS | 已实现 | Fake、合同、失败/续跑测试；2026-08-20按官方上传协议完成2次真实付费任务并成功下载结果 | 后续批量运行仍需`XMAX_API_KEY`、`cos-python-sdk-v5`和与冻结计划绑定的预算批准 |
| XMAX实时SDK | 已实现新旧API双路 | `tsc --noEmit`、JS语法、无Key安全失败 | Key、预算批准、可用WebRTC环境；未做付费smoke |
| 预处理/音频 | 已实现ffmpeg适配器 | Fake与PCM包络测试 | 当前机器PATH中需安装`ffmpeg`/`ffprobe` |
| Qwen视频MLLM | 已实现JSON Mode/本地Schema校验/盲评/15候选免费额度回退、唯一末位`qwen3-vl-flash`付费兜底、原生媒体角色隔离、整条Case原子重试、生成操作合同和实际Feed截图输入；SQLite按请求预留并执行99元本地硬上限 | 2026-08-20实测`qwen3-vl-plus`同请求识别两段视频和一张参考图，角色无串位（5089输入+273输出Token）；2026-08-24移除10个日期快照别名，当前总列表16个；已验证quota规则跨HTTP状态回退、未知quota停批、同模型网络退避、15免费模型到付费闸门和流式排空由自动测试覆盖 | `QWEN_API.csv`；Base64超限媒体需Provider可访问URL；前15个保持免费用尽即停，付费前只关闭末位泛化`qwen3-vl-flash`的该开关，并人工充值、执行本地授权；本地估算不覆盖账户外部调用 |
| Codex CLI MLLM | 保留可替换Provider | Fake与错误路径 | 仅在Judge Pack改配后启用 |
| CV/Metric Judge | 动态Python插件协议、基础画质、音轨及运行事实Judge已完成 | Manifest/Schema/三档阈值/不伪造分测试 | 专项身份、姿态、跟踪模型可按维度替换Shadow MLLM路由 |
| 飞书 | `lark-cli`真实命令适配已实现 | Fake、分页/幂等/策略测试；2026-08-20已真实完成Case upsert、百分比评分、三类附件上传和回读对账 | 换Base/Table时仍需真实字段映射和明确写入授权 |

## 2. 依赖顺序

```text
P0
├─ P1
├─ P0.5 → P0.7
├─ P0.5 → P2 → P3 → P4/P5 → P6
├─ P0.5 + P0.7 + P2 + P3合同 → P2.5
├─ P7
└─ P10基础仓库

P4/P5 + P6 + P7 + P1 → P8
P8 → P9 → P11
P8 + P9 → P12
P0.7 + P2-P12 → P13
P0.5 + P2/P3/P4/P5/P8/P9/P11 → P10完整同步
```

## 3. 工作包合同

### P1 Scene Weighting

阅读：`docs/scene-weighting.md`、`docs/evaluation-pipeline.md`。

目标文件：`src/xmax_test/evaluation/weights.py`、Fusion实现、`schemas/evaluation-result.schema.json`。

验收基线：Fusion同时输出Canonical和Scenario Score；硬门槛不受权重影响；结果保存命中规则和有效权重。

### P0.5 Persistence

阅读：`docs/storage.md`、`docs/data-contracts.md`、`docs/implementation-contract.md`。

目标文件：`storage/sqlite.py`、`storage/artifacts.py`、`storage/migrations.py`、`storage/migrations/0001_initial.sql`、`tests/storage/`。

CLI目标：`xmax-test db migrate|check`、`xmax-test artifacts verify|gc --dry-run`。

验收：空库和已有库迁移幂等；追加事件可重建状态；并发写不丢失；Artifact写入原子且校验哈希；路径穿越被拒绝；可用临时目录完成离线测试。

### P0.7 Stage Orchestration

阅读：`docs/stage-orchestration.md`、`docs/data-contracts.md`、`docs/implementation-contract.md`。

目标文件：`pipeline/models.py`、`pipeline/selectors.py`、`pipeline/manifests.py`、`pipeline/dependencies.py`、`pipeline/orchestrator.py`、`pipeline/streaming.py`、`storage/sqlite.py`、`tests/pipeline/`。

CLI目标：`xmax-test stage show`、`xmax-test batch show`、以及统一Run中的阶段解析。

验收：Selector按ID/过滤/Manifest冻结为稳定快照；每次执行生成Stage Manifest；输入/配置哈希不变可续跑；哈希变化新建Stage Run；缺失依赖只返回错误和建议命令，不执行未授权阶段；测试确认`evaluate`不会调用生成Adapter。

### P2 Assets

阅读：`docs/assets.md`、`docs/feishu-database.md`、`docs/external-inputs.md`。

目标文件：

```text
src/xmax_test/assets/models.py
src/xmax_test/assets/registry.py
src/xmax_test/assets/validator.py
src/xmax_test/assets/sources/local.py
src/xmax_test/assets/sources/http.py
src/xmax_test/assets/sources/feishu.py
src/xmax_test/assets/sources/feishu_sheet.py
src/xmax_test/assets/sources/feishu_bitable.py
src/xmax_test/assets/sources/feishu_wiki.py
tests/assets/
```

CLI目标：`xmax-test ingest assets discover|sync|verify`。

验收：Sheet/Base/Wiki fake来源可完整跑通；Sheet检查截断与真实行号，Base分页到结束，Wiki解析真实对象；保存revision和附件token；同内容幂等；内容变化生成新asset；损坏媒体进入invalid/quarantined；输出哈希和ffprobe信息。

### P2.5 Existing Result Ingestion

阅读：`docs/stage-orchestration.md`、`docs/assets.md`、`docs/feishu-database.md`和`docs/operation-recipes.md`。

目标文件：`ingest/results.py`、`ingest/models.py`、`ingest/feishu_case.py`、`ingest/local_results.py`、`ingest/manifest_results.py`、`storage/sqlite.py`、`tests/ingest/`。

CLI目标：`xmax-test ingest results --config config/existing-results.json`。

验收：飞书Case全分页快照、本地目录和Stage Manifest fake都能导入；下载并校验结果/Feed/Prompt；产生正常Asset、TestCase和completed Run Batch；保存origin/provenance；缺失模式、Recipe、被编辑视频或音轨基准时报错；导入不写远端；重复导入同快照幂等。

### P3 Planning

阅读：`docs/test-planning.md`、`docs/operation-recipes.md`、`docs/feishu-database.md`。

目标文件：`planning/models.py`、`planning/recipes.py`、`planning/builder.py`、`planning/case_numbers.py`、`planning/budget.py`、`planning/strategies.py`、`storage/sqlite.py`。

CLI目标：`xmax-test plan preview|build|show`。

验收：相同输入、远端编号快照和seed产生相同case_id/case_number；默认5次且可覆盖；显式模式优先、互动默认实时、其他默认离线；每条Case冻结配方、被编辑视频、音轨来源和API绑定；后续批次从远端最大后缀继续；预算预览无外部副作用；缺失素材有明确跳过原因；计划通过Schema。

### P3.5 Task Allocation and Execution

阅读：`docs/test-planning.md`、`docs/task-execution.md`、`docs/stage-orchestration.md`。

目标文件：`planning/strategies.py`、`tasks/service.py`、`tasks/runtime.py`、`storage/migrations/0002_test_tasks.sql`、`schemas/test-task.schema.json`。

CLI目标：`xmax-test task show|claim|run`、`xmax-test worker status|run`。

验收：全量、随机组合、随机总任务数和指定组合都在Plan时可复现冻结；除`random_runs`总数语义外，`repeat_count`默认5且可覆盖；Worker不重新选组合；多Worker租约不重复领取；过期可回收；已完成任务幂等；基础设施失败可续跑；单条全Fake生成、评测、飞书同步和对账通过。

### P4 Offline Generation

阅读：`docs/generation-offline.md`、`docs/operation-recipes.md`和项目根上层参考时序。

目标文件：

```text
generation/offline/rest_adapter.py
generation/offline/session_api.py
generation/offline/rtc_adapter.py
generation/offline/state_machine.py
generation/offline/repository.py
generation/fakes.py
tests/generation/offline/
```

CLI目标：`xmax-test generate offline --plan-id ... [--resume]`。

验收：fake覆盖图片参考与视频参考两种绑定，验证Prompt视频作为`refVideoPath`且Feed截图作为`refImagePath`；保存音轨基准；覆盖成功、error、heartbeat失败、超时、下载损坏和重试；失败也关闭Session；重复提交保护；结果和原始事件可续跑。

### P5 Realtime Generation

阅读：`docs/generation-realtime.md`。

目标文件：

```text
realtime-harness/package.json
realtime-harness/src/session.ts
realtime-harness/src/capture.ts
realtime-harness/src/events.ts
realtime-harness/src/rtc-log.ts
realtime-harness/src/fake-sdk.ts
src/xmax_test/generation/realtime/controller.py
tests/generation/realtime/
```

CLI目标：`xmax-test generate realtime --plan-id ... [--headed]`。

验收：固定Feed单轮不被自动循环污染；内置drag和`sendTracks`单/多指事件可回放，30 FPS与streamSetting坐标映射可验证；所有SDK回调有时间戳；显式发布/订阅音频；输入/输出流和逐帧数据可保存；fake支持断开/重连；无真实Key也能跑测试。

### P6 Preprocessing

阅读：`docs/evaluation-pipeline.md`、`docs/codex-mlmm.md`。

目标文件：`evaluation/preprocess.py`、`evaluation/sampling.py`、`evaluation/roi.py`、`evaluation/contact_sheet.py`。

CLI目标：`xmax-test preprocess --run ...`。

验收：全局和事件采样覆盖首尾；参数/版本进入manifest；同输入缓存命中；无法解码时不产生伪证据；联系图不用于运行指标。

### P7 Judges

阅读：`docs/cv-judges.md`、`docs/codex-mlmm.md`、`docs/judge-responsibilities.md`。

目标文件：`judges/mlmm/`、`judges/worker.py`、`judges/plugins/`、`judges/registry.py`、`tests/judges/`。

CLI目标：`xmax-test judges list|check|run`。

验收：MLLM Provider非JSON自动重试并保存原始输出；Judge缺依赖时明确不可用；插件逐细则输出通过Schema；blind输入不含模型/人工结论；OpenAI兼容API、Codex CLI和fake Provider可替换且离线测试不访问外网。

### P8 Evaluation

阅读：`docs/evaluation-pipeline.md`、`docs/scene-weighting.md`。

目标文件：`evaluation/orchestrator.py`、`evaluation/fusion.py`、`evaluation/gates.py`、`evaluation/group_metrics.py`、`evaluation/aggregation.py`、`storage/sqlite.py`。

CLI目标：`xmax-test evaluate --run-batch-id|--run-id ...`。

验收：动态加载Benchmark；Judge按细则分工且可续跑；只有Benchmark明确N/A的细则可从分母移除，缺评不得重归一；重复/跨输入指标在冻结批次完成后回填；输出Canonical/Scenario分；硬门槛生效；输入可为生成或导入的completed Run；评测路径不依赖XMAX Adapter或飞书下载器。

### P9 Human Signals

阅读：`docs/human-feedback.md`、`docs/benchmark-lifecycle.md`。

目标文件：`feedback/importer.py`、`feedback/normalizer.py`、`feedback/router.py`、`feedback/proposals.py`、`feedback/overrides.py`、`feedback/training.py`、`storage/sqlite.py`。

CLI目标：`xmax-test human import|feedback|normalize|partition`。

验收：原文与AI原分不可覆盖；人工总分/细则分以追加Override形成有效分；未知细则拒绝；低置信度不进入学习；unmapped产生提案；Train生成MLMM校准包或调用CV Trainer；单条override不热更新Judge；Holdout无法被训练读取，Challenger必须验证后显式发布。

### P10 Feishu

阅读：`docs/feishu-database.md`、`docs/external-inputs.md`。

目标文件：`feishu/client.py`、`feishu/attachments.py`、`feishu/sync.py`、`feishu/ledger.py`、`feishu/reconcile.py`。

CLI目标：`xmax-test feishu dry-run|sync|verify|reconcile`。

验收：默认Base和Feed/Prompt/Case三表fake测试；真实结构先读后写；每Run一条Case，使用`case编号 + 模型版本`upsert；失败为0%、未重评为空；内部0–100评分写入飞书0–1百分比字段时执行除100，回读执行乘100；`none/score_only/metadata_only/attachments_only/full`策略有独立fake测试，`score_only`禁止创建Case和上传附件；附件和字段分阶段；Sheet/Base来源与Case后缀可对账；中断续跑；回读发现缺失/重复/冲突；dry-run不写入。

### P11 Release/Replay

阅读：`docs/benchmark-lifecycle.md`、`docs/operations.md`。

目标文件：`evaluation/replay.py`、`evaluation/release.py`、`judges/releases.py`。

CLI目标：`xmax-test replay run`、`xmax-test release validate|promote|rollback`。

验收：Holdout隔离；旧结果不覆盖；新旧差异报告；失败版本不能promote；Champion可回滚。

### P12 Reporting

阅读：`docs/version-reporting.md`和`report-templates/model-version-update-report.md`。

目标文件：`reporting/comparison.py`、`reporting/classification.py`、`reporting/renderer.py`、`reporting/service.py`、`reporting/single_version.py`、`reporting/repository.py`、`tests/reporting/`。

CLI目标：`xmax-test report model-update --request config/run-request.json`、`xmax-test report single-version --request config/single-version-report.json`。

验收：版本对比必须显式选择两侧Run/Evaluation Batch，相同配对样本计算双分数变化；不可比配置拒绝升降结论；每个请求场景有独立小节；P0/P1/P2按已发布策略归类；新增Hard Gate失败必入P2。单版本报告必须输出总分、全量维度、全量细则、逐Case、多统计量、优劣项及建议；失败Run计0%；人工有效修订进入报告且保留AI原值；JSON与Markdown同时落盘并通过Schema。

### P13 Unified CLI

阅读：`RUNBOOK.md`。

目标：实现RUNBOOK中的全部命令、稳定退出码、`--json`输出和`--dry-run`。

验收：一个全fake端到端测试从素材到报告及对账通过；另有“只生成”、“旧Run只评测”、“飞书Case导入后评测且不写远端”、“只回写评分”四个全fake端到端测试；缺少插件输入时一次性列全缺项；真实外部调用前有批准门槛；重复运行不重复写入；版本更新请求生成P0/P1/P2完整报告。

## 4. Agent交接格式

完成一个工作包后在最终回复和本文件状态中说明：实现文件、测试命令、通过数量、未实现项、需要的下一外部输入。不要仅回复“架构已完成”或“应该可以”。
