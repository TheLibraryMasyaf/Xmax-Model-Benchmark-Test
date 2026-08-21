# 评分标准与权重修改手册

本文只负责 `BENCHMARK.md` 中评分维度、评分锚点、权重档、场景规则、硬门槛和Score Schema的插件式修改。实现细节和发布原则分别见 `docs/scene-weighting.md` 与 `docs/benchmark-lifecycle.md`。

## 1. 能直接修改的内容

- 维度名称、定义、适用模式和证据要求；
- 每个维度的评分锚点和不可评条件；
- 新增维度、停用维度或用新维度替换旧维度；
- 通用权重档；
- 场景权重档、场景匹配规则和规则优先级；
- 硬门槛；
- 最终百分比Score Schema及其版本；
- 变更日志。

权重和总分不属于Judge。修改这些内容时不得把数值同步写进CV或MLLM Prompt代码。

## 2. 修改评分细则

1. 复制当前合同为新 `benchmark_version`，不要原地改变已发布版本的语义。
2. 保持原 `dimension_id` 的前提是该维度测量对象没有变化；仅调整措辞、档位边界或证据要求时按生命周期规则选择Patch或Minor版本。
3. 如果测量对象发生变化，创建新 `dimension_id`，将旧维度标记为停用或被替换，不复用旧ID表达新含义。
4. 检查所有引用该维度的：
   - `weight_profiles`；
   - `scene_weight_rules`；
   - `hard_gates`；
   - `score_schemas`；
   - `config/judges.json` 中 `supported_dimensions`。
5. 在变更日志记录原因、版本、迁移关系和预期影响。

新增维度后没有Judge覆盖时，该维度只能保持不可评或Shadow状态，不能偷偷由相邻维度代判。

## 3. 修改权重

1. 在 `BENCHMARK.md` 中创建新的权重档版本或新档案，不覆盖已用于正式结果的档案。
2. 检查每个权重非负，目标维度存在且在对应模式可用。
3. 通用权重作为基础；场景规则只选择或调整权重，不进入Judge输入。
4. 多条场景规则同时命中时，严格使用 `docs/scene-weighting.md` 定义的优先级、合成和归一化算法。
5. 不适用或不可评维度按Score Schema从分子、分母同时剔除；不得自动补成50分或其他中性值。
6. 修改硬门槛时同时记录触发条件、结果状态和版本，确保最终报告可解释。

如果第一轮测试只是探索权重，应保持Shadow，不把探索性权重改写为历史正式分。

## 4. 修改后的引用完整性检查

逐项确认：

- 每个权重引用的维度都存在；
- 已停用维度不再被新权重档使用；
- 场景规则引用的场景存在于 `config/scenarios.json`；
- 每套权重经过归一化后合计为100%；
- Score Schema能处理不可评维度和硬门槛；
- `config/judges.json` 对正式维度有明确覆盖，或明确标为暂不可评；
- 旧评测批次仍能按其原版本解释。

## 5. 离线检查与发布前验证

在没有真实生成和外部调用的情况下至少运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m xmax_test benchmark-check --root .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m xmax_test judges list --root .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

对历史批次的影响使用版本化回放检查：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m xmax_test replay run --run-batch-id <历史批次ID> --root .
```

发布前还应按 `docs/benchmark-lifecycle.md` 完成快照、回放、留出集和Shadow验证。若当前CLI检查能力不足以验证某项新合同，不得把缺失检查解释为合同有效。

## 6. 必须升级为架构修改的情况

以下情况不能只改Benchmark：

- 新维度需要当前上下文中不存在的新输入资产；
- 新标准要求增加流水线阶段或改变阶段依赖；
- 总分不再能由现有Score Schema表达；
- 需要新的Judge融合算法，而不是权重档或路由调整；
- 需要覆盖、迁移或删除已有正式结果。

遇到这些情况先创建建设工作包，不在 `BENCHMARK.md` 中塞入无法执行的伪配置。
