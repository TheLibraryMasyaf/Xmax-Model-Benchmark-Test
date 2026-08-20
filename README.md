# XMAX Test

XMAX 离线与实时视频模型测试平台。已实现可追溯、可续跑、可版本化的分阶段核心流程：

```text
素材下载
→ Feed × Prompt 测试计划
→ 离线/实时视频生成
→ CV + 可插拔MLLM（当前Qwen3-VL）自动评测
→ 人工评测集与结果纠错
→ Judge 学习、验证与发布
→ 模型版本更新 P0/P1/P2 报告
→ 飞书数据库同步与维护
```

流程由`ingest / plan / generate / preprocess / evaluate / feedback / report / sync / reconcile`九个可独立运行的阶段组成。默认完整运行按标准Manifest交接；单阶段运行只需提供已有批次/Manifest选择器，不得静默补跑其他阶段。

当前仓库包含真实 REST/COS、浏览器实时 SDK、Qwen3-VL OpenAI兼容Provider、Codex CLI备选Provider、音频指标、CV Python插件、飞书 `lark-cli`的适配边界及全 Fake 回归。巨大 CV 权重、GPU 运行环境、密钥和飞书映射仍是允许插件式提供的外部输入；缺失时该维度返回不可评，不伪造中性分。[BENCHMARK.md](BENCHMARK.md) 已录入当前暂定评测标准，整体以Shadow运行；第一轮测试后通过新版本调整维度、权重和规则，不原地覆盖历史。

没有历史上下文的建设 Agent 先读 [AGENTS.md](AGENTS.md) 和 [IMPLEMENTATION.md](IMPLEMENTATION.md)；项目建成后的操作者只按 [RUNBOOK.md](RUNBOOK.md) 执行。

## 1. 快速检查

```bash
cd xmax-test
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/xmax-test project-check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

预期输出：

```text
Project scaffold complete
BENCHMARK valid: version=0.1.0-draft status=shadow dimensions=23 weight_profiles=2 scene_weight_rules=14 score_schemas=1 scenarios=32
```

## 2. 目录

```text
xmax-test/
├── AGENTS.md                    # 无上下文建设 Agent 的强制入口与规则
├── IMPLEMENTATION.md            # 剩余建设任务、目标文件和验收条件
├── RUNBOOK.md                   # 建成后的唯一常规操作手册
├── BENCHMARK.md                 # 当前Shadow暂定标准，后续按版本调整
├── report-templates/            # 模型版本更新等最终报告模板
├── config/                      # 本地配置模板，不保存密钥
├── docs/                        # 按职责拆分的设计说明
├── schemas/                     # 跨模块 JSON Schema
├── src/xmax_test/
│   ├── assets/                  # 素材获取、校验和登记
│   ├── planning/                # Feed × Prompt × 模式 × 重复次数
│   ├── generation/
│   │   ├── offline/             # 离线生成适配器
│   │   └── realtime/            # 浏览器实时 SDK 测试 Harness
│   ├── evaluation/              # 评测编排、融合和发布门槛
│   ├── judges/                  # CV/MLLM/音频 Judge 插件注册
│   ├── feedback/                # 并行人工评测线与学习路由
│   ├── feishu/                  # 飞书多维表格同步
│   └── storage/                 # 本地元数据和产物存储
└── tests/                       # 单测、失败路径和全 Fake 端到端
```

## 3. 文档入口

| 文档 | 只负责说明 |
| --- | --- |
| [架构决策](docs/decisions.md) | 已确定且不得由执行 Agent 临场重选的技术决策 |
| [外部输入合同](docs/external-inputs.md) | Benchmark、场景包、密钥和插件配置的接入边界 |
| [跨模块实现合同](docs/implementation-contract.md) | 分层、ID、CLI、错误、Fake和测试的统一约定 |
| [本地存储](docs/storage.md) | SQLite、事件、Artifact和迁移边界 |
| [整体技术架构](docs/architecture.md) | 服务边界、主流程、依赖方向 |
| [可拆分阶段](docs/stage-orchestration.md) | Stage Manifest、独立运行、历史结果导入与同步策略 |
| [数据合同](docs/data-contracts.md) | ID、状态、Schema、产物和版本 |
| [素材管理](docs/assets.md) | 下载、去重、校验、素材台账 |
| [测试计划](docs/test-planning.md) | 组合、重复、预算、可复现性 |
| [玩法与输入操作配方](docs/operation-recipes.md) | 默认模式、Feed/Prompt API角色、音轨来源和实时互动 |
| [离线生成](docs/generation-offline.md) | 离线任务适配器和状态机 |
| [实时生成](docs/generation-realtime.md) | XMAX JS SDK、录流和 R 指标采集 |
| [自动评测编排](docs/evaluation-pipeline.md) | CV/MLLM/指标 Judge 的调用和融合 |
| [场景动态权重](docs/scene-weighting.md) | 预设权重档、规则合成、归一化和审计 |
| [Judge责任矩阵](docs/judge-responsibilities.md) | 各类评测标准应由谁主判、辅助和提供事实 |
| [CV Judge](docs/cv-judges.md) | 建议模型、插件协议、训练边界 |
| [MLLM Provider](docs/codex-mlmm.md) | Qwen3-VL/Codex调用、免费额度回退、JSON输出和重试 |
| [人工信号](docs/human-feedback.md) | 独立人工集、纠错、维度提案与学习 |
| [评测标准生命周期](docs/benchmark-lifecycle.md) | 新增、修改、停用、回放与发布 |
| [飞书数据库](docs/feishu-database.md) | 表设计、幂等同步、附件和对账 |
| [运行与安全](docs/operations.md) | 密钥、付费门槛、续跑、监控与故障处理 |
| [版本更新报告](docs/version-reporting.md) | 基线/新版可比性、P0/P1/P2归类和报告生成 |

## 4. 不可变约束

1. `BENCHMARK.md` 是评测维度、评分锚点和总分方案的唯一来源。
2. 业务代码不得定义固定的 C/O/R 枚举；维度 ID 从 Benchmark 合同动态加载。
3. 原始素材、API事件、CV输出、Codex输出和人工原文只能追加，不能被归一化结果覆盖。
4. 人工可以立即覆盖单条评测结果，但模型学习必须创建 Challenger 并通过 Holdout。
5. 离线与实时生成共用 TestCase、GenerationRun、Judgment 合同，不共用不适合的运行指标。
6. 飞书是协作数据库和展示面，不是唯一事实源；本地元数据与原始产物必须可重建飞书内容。
7. 批量计费生成前必须先输出组合数、预计任务数、预计积分范围和跳过项，再由操作者放行。
8. 默认飞书投影为Feed数据、Prompt数据、Case数据；每次Run独立占一个Case编号，失败为0%、未重评为空。
9. 默认每个Feed × Prompt重复5次但可覆盖；Case只保存单次百分比，组平均与分布只出现在报告。
10. 输入API角色和音轨来源由Operation Recipe冻结；互动玩法默认实时，其他默认离线，显式指定优先。
11. 阶段之间只通过版本化批次、实体引用和Stage Manifest交接；生成、评测、报告和飞书同步均可单独授权运行。
12. 缺失上游输入时报合同错误，禁止静默执行未列入Run Request的阶段，尤其禁止隐式付费生成。

## 5. 运行准备

建设状态和外部实测边界见 [IMPLEMENTATION.md](IMPLEMENTATION.md)，无上下文的执行 Agent 只需按 [RUNBOOK.md](RUNBOOK.md) 操作。真实运行前先执行 `context-check`：它会一次性列出 `ffmpeg/ffprobe`、COS SDK、Node/Playwright/XMAX SDK、Codex、Judge 插件、Key 和飞书映射等缺项。

## 6. 设计来源和排除项

- 素材、计划、离线批量生成和增量流水线参考 [XMAX视频生成测试工作流](../参考文件/Xmax模型能力测试工作流/README.md)。
- Session、RTC、heartbeat和模型生命周期参考 [离线任务发起模型推理流程](../离线任务发起模型推理流程.md)。
- 实时生成按照 [XMAX官方实时SDK文档](https://platform.xmaxai.com/docs/capabilities/realtime-video/sdk-reference) 设计。
- 参考工作流目录里的旧 `docs/评价标准.md` 未接入本项目；后续只接受根目录 `BENCHMARK.md` 中的新标准。
