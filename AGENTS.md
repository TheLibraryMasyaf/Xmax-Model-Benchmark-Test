# Agent Instructions

本目录必须能够被没有历史对话上下文的Agent独立理解。任何建设或执行任务先按本文件读取，不要依赖父目录聊天记录、记忆或旧评测文档。

## 1. 必读顺序

1. `README.md`：项目范围、目录和不可变约束。
2. `IMPLEMENTATION.md`：当前完成度、下一个工作包、目标文件和验收命令。
3. 与任务对应的单一组件文档，例如 `docs/generation-offline.md`。
   涉及生成输入、Prompt视频、模式或互动时必须同时读取`docs/operation-recipes.md`和`config/operation-recipes.json`。
   涉及统一Run、单阶段执行、已有结果导入或同步策略时必须读取`docs/stage-orchestration.md`。
4. `docs/data-contracts.md` 与相关 `schemas/*.json`。
5. `docs/decisions.md`：已经决定的技术取舍，不要重新发散设计。
6. `docs/implementation-contract.md`：所有工作包共同遵循的实现、错误和测试合同。
7. 真实执行或改流水线时必须读取 `docs/fail-safe-checks.md`，不得绕过前置检查或用残缺结果出分。
8. 只有评测任务才读取 `BENCHMARK.md`；标准为空时不得从旧资料推断补齐。

如果任务是修改评分标准、权重、MLLM Provider或CV/Metric Judge等插件式扩展，先读取 `docs/plugin-extension-guide.md`，再按其路由读取对应 `docs/plugin-recipes/` 分册。

建成后的实际运行按 `RUNBOOK.md`，外部输入按 `docs/external-inputs.md`。

模型版本更新测试还必须读取 `docs/version-reporting.md`，并使用 `report-templates/model-version-update-report.md` 生成最终报告；不得只在聊天回复中给口头总结。

## 2. 权威来源优先级

发生冲突时按以下顺序处理：

```text
用户本次明确要求
→ BENCHMARK.md中的已发布合同
→ schemas/*.json
→ AGENTS.md / IMPLEMENTATION.md / RUNBOOK.md
→ 对应组件文档
→ README.md
→ 参考文件
```

参考目录只提供素材/API/历史实现证据，不是当前评测标准。禁止读取或复制 `参考文件/Xmax模型能力测试工作流/docs/评价标准.md` 作为新Benchmark。

## 3. 建设任务执行规则

1. 在 `IMPLEMENTATION.md` 找到明确工作包和依赖。
2. 如果依赖未完成，先完成依赖或明确报告阻塞，不要在重复位置造临时实现。
3. 只实现该工作包列出的公共接口、目标文件和CLI合同。
4. 原始数据和外部事件使用追加写；不得用派生结果覆盖。
5. 所有新增外部边界必须有Adapter、超时、重试、原始日志和测试替身。
6. 新配置必须有 `.example.json`、JSON Schema和无密钥默认值。
7. 新功能必须添加自动测试，并更新 `IMPLEMENTATION.md` 状态。
8. 完成前运行本文件第7节的全量检查。
9. 单阶段任务只执行Run Request显式列出的阶段；输入缺失时报错并给出缺失引用，不自行补跑上游。

如果文档已明确算法和取舍，直接按文档实现，不需要向用户重复询问“应该怎么设计”。

如果任务是“完成/构建整个项目”，含义是按依赖顺序把 `IMPLEMENTATION.md` 中全部 `TODO/PARTIAL` 工作包实现到 `DONE`，直至全Fake端到端测试和RUNBOOK命令都通过；不是照RUNBOOK发起一次XMAX测试，也不能以“架构文档已存在”代替代码交付。除第5节的真实阻塞外，完成一个工作包后继续下一个，不要求用户逐包解释目的和做法。

## 4. 允许由用户插件式提供的内容

以下内容可以在任务开始时由用户提供，缺失时使用示例文件做dry-run，不应自行猜测：

- `BENCHMARK.md`：评分标准、维度、权重、硬门槛。
- `config/scenarios.json`：场景标签和场景清单。
- `config/operation-recipes.json`：玩法、默认模式、输入绑定、音轨来源和互动Profile；仓库已有默认版本，新玩法可用新版本替换。
- `config/asset-sources.json`：素材来源和字段映射。
- `.env`：XMAX Key、飞书凭据和本地工具路径。
- `config/feishu.json`：Base/Table映射。
- `config/judges.json`：已部署Judge插件和版本。
- `config/run-request.json`：本次运行范围。
- `config/existing-results.json`：可选，把飞书Case、本地目录或旧Manifest导入为可评测completed Run。

这些文件的合同见 `docs/external-inputs.md`。除这些显式插件输入外，系统行为应由仓库内代码和文档完全决定。

## 5. 必须停止并请求输入的情况

仅在以下情况请求用户：

- 要开始真实计费生成但没有明确批准预算清单。
- 必需密钥或飞书目标未提供，且任务明确要求真实外部执行。
- Benchmark仍为空，但任务要求产出正式评分。
- API能力与官方响应冲突，继续可能计费、污染数据或产生不可比较结果。
- 需要删除、覆盖或迁移已有正式数据。

普通代码建设、测试替身、dry-run和Schema工作不应因缺少真实Key而停住。

## 6. 禁止事项

- 不把维度ID写成固定Enum。
- 不让Codex自由生成每条样本的权重。
- 不把模型名、历史人工结论或预期输赢提供给盲评Judge。
- 不把飞书行号当业务ID。
- 不在日志、命令行示例或配置模板中保存真实密钥。
- 不声称未实现的Runner、Judge或飞书连接器已经可用。
- 不让`evaluate`隐式下载远程Case、创建生成Run或触发XMAX；这些只能由显式`ingest`/`generate`阶段完成。
- 不用联系图推断FPS、延迟、网络和设备指标。
- 不省略模型版本更新报告中的P0/P1/P2任一层级，也不在Score Schema缺少阈值时自创“明显提升”标准。

## 7. 完成前检查

```bash
cd xmax-test
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m xmax_test project-check --root .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

此外验证：新增JSON可解析、本地Markdown链接存在、没有提交密钥、工作包验收条件全部满足。

## 8. Definition of Done

一个工作包只有同时满足以下条件才可以标记完成：

- 目标代码和CLI存在，不是空壳。
- 单元测试和至少一个失败路径测试通过。
- 外部调用有可离线运行的fake/mock。
- 输入输出满足Schema。
- 原始日志、版本和错误可追溯。
- 对应组件文档与实际行为一致。
- `IMPLEMENTATION.md`状态已更新。
