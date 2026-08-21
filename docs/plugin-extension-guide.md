# 插件式扩展操作入口

本文是交给没有项目建设上下文的 Agent 使用的扩展导航。它只处理不改变总体流水线结构的修改；完整测试执行仍按 `RUNBOOK.md`，跨模块实现约束仍以 `docs/implementation-contract.md` 为准。

## 1. 先判断修改类型

| 目标 | 必读分册 | 主要修改位置 | 通常是否修改流水线代码 |
|---|---|---|---|
| 修改评分描述、档位、证据要求 | `docs/plugin-recipes/benchmark-and-weights.md` | `BENCHMARK.md` | 否 |
| 新增、停用或替换评分维度 | `docs/plugin-recipes/benchmark-and-weights.md` | `BENCHMARK.md`、Judge覆盖配置 | 通常否 |
| 修改通用权重、场景权重或硬门槛 | `docs/plugin-recipes/benchmark-and-weights.md` | `BENCHMARK.md` | 否 |
| 更换OpenAI兼容的MLLM | `docs/plugin-recipes/mlmm-provider.md` | `config/judges.json`、密钥环境变量 | 否 |
| 接入非OpenAI兼容的MLLM API或本地服务 | `docs/plugin-recipes/mlmm-provider.md` | 新Provider模块、测试、`config/judges.json` | 通常否 |
| 新增CV或确定性指标Judge | `docs/plugin-recipes/judge-plugin.md` | 新Judge模块、测试、`config/judges.json` | 通常否 |
| 新增全新的插件类别、构造方式或调度阶段 | `docs/architecture.md`、`IMPLEMENTATION.md` | Composition、Schema、流水线和测试 | 是；不属于本文范围 |

不要因为修改评分标准而把权重写进Judge，也不要因为更换MLLM而复制一套评测编排。

## 2. 修改前的权威来源

发生冲突时遵循 `AGENTS.md` 的优先级。插件式扩展至少读取：

1. `AGENTS.md`；
2. 本文和对应分册；
3. `docs/data-contracts.md` 与涉及的Schema；
4. `docs/implementation-contract.md`；
5. 评分相关修改再读取 `BENCHMARK.md`、`docs/benchmark-lifecycle.md`；
6. Judge相关修改再读取 `docs/judge-responsibilities.md` 和对应Judge文档。

旧参考目录不是当前合同，不得从旧评分文档补齐或覆盖 `BENCHMARK.md`。

## 3. 共同不变量

- 维度ID、场景和权重来自版本化合同，不在Python中写死成Enum或分支表。
- Judge只输出维度判断、分数、置信度和证据；Judge不决定场景权重和总分。
- 原始判断追加保存；新规则通过新版本、回放或新评测批次生效，不覆盖旧结果。
- 新外部Provider或Judge必须有确定性Fake、失败路径和无真实密钥的测试。
- 配置和样例不得包含真实API Key；密钥只从环境变量或项目规定的凭据文件读取。
- 插件无法评估时返回不可评，不伪造中性分。
- 新版本先Shadow验证；未经留出集或回放验证，不替换当前Champion。
- 正在进行正式运行时，不修改活动的 `BENCHMARK.md`、`config/judges.json` 或已启用插件代码。

## 4. 标准工作顺序

```text
识别修改类型
→ 阅读对应分册和合同
→ 创建新版本或新插件ID
→ 只修改该扩展点负责的文件
→ 增加或更新测试替身
→ 运行静态/离线检查
→ 回放或Shadow验证
→ 记录版本、变更原因和回滚目标
→ 再决定是否发布
```

扩展不能通过既有协议表达时，不要绕过协议写临时分支。把它记录为新的建设工作包，再按 `IMPLEMENTATION.md` 修改架构。

## 5. 交付说明模板

Agent完成扩展后至少报告：

```markdown
修改类型：Benchmark / 权重 / MLLM Provider / CV Judge / Metric Judge
旧版本：
新版本：
修改文件：
支持的维度与模式：
是否改变历史结果：否；如需变化，通过哪个回放或新批次产生：
离线检查：
Shadow或留出集结果：
已知限制：
回滚目标：
```

不得用“配置已添加”代替实际加载检查，也不得用Fake通过声称真实外部API已经验证。
