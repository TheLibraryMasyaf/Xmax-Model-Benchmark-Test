# CV与Metric Judge插件接入手册

本文负责新增CV Judge或确定性指标Judge。现有建议模型、隔离原则和学习边界见 `docs/cv-judges.md`，路由责任见 `docs/judge-responsibilities.md`，评测编排见 `docs/evaluation-pipeline.md`。

## 1. 何时创建Judge插件

适合独立Judge的能力包括：

- 可重复计算的视频质量、帧、音频、时序或运行指标；
- 有明确输入、模型版本和输出合同的专用CV模型；
- 能独立返回维度证据，而不需要决定场景权重的检测器。

仅修改某维度权重、档位或文字说明时，不创建新Judge。需要综合多个Judge结果时，使用现有评测编排和融合合同，不让某个插件私自覆盖其他Judge。

## 2. 插件合同

插件必须满足 `src/xmax_test/judges/base.py` 中的 `JudgePlugin`：

```python
class CustomJudge:
    def manifest(self) -> dict:
        ...

    def evaluate(self, context: dict) -> list[dict]:
        ...
```

`manifest()` 必须包含稳定且版本化的信息：

- `judge_id`；
- `version`；
- `kind`，通常为 `cv` 或 `metric`；
- `supported_dimensions`；
- `supported_modes`；
- `required_inputs`；
- `entrypoint`，格式为 `module:object`；
- 资源和超时声明。

`evaluate()` 返回的每条判断遵循 `schemas/judgment.schema.json`，至少明确：

- `dimension_id`；
- `verdict`；
- 非空`criterion_results`，且每项有Benchmark中真实的`criterion_id`；
- 每条细则的0/1/2 `score`（不可评时为`null`）；
- `confidence`；
- `assessable`；
- 可读 `evidence`；
- 可复算或审计的 `raw_metrics`。

顶层维度`score`会由Worker从该Judge已提交的细则求平均，仅供审计。最终Fusion忽略该字段，从所有Judge的逐细则结果重新计算。不得把一个局部检测分复制给本维度的其他并行细则。

缺少输入、模型加载失败或该样本不适用时，返回不可评并说明原因；不得返回虚构的50分。

## 3. 实现边界

- 插件只评估Manifest声明的维度和模式。
- 输入只从评测上下文和Artifact引用获取，不隐式下载飞书Case或触发视频生成。
- 预处理应复用统一产物；插件特有预处理必须版本化并记录参数。
- 模型权重、运行库和设备要求写入Manifest或部署说明，不写入评分标准。
- 输出原始测量值和证据，不在插件内应用场景权重。
- 模型更新必须改变Judge版本；旧结果继续引用旧版本。

## 4. 注册方式

在 `config/judges.json` 中添加并默认以Shadow方式启用或在专用测试配置中启用：

```json
{
  "judge_id": "custom-video-cv",
  "version": "0.1.0-shadow",
  "kind": "cv",
  "enabled": true,
  "supported_dimensions": ["<维度ID>"],
  "supported_modes": ["offline"],
  "required_inputs": ["result_video"],
  "entrypoint": "your_package.judges:CustomJudge",
  "timeout_seconds": 300
}
```

当前CV/Metric动态加载器使用无参数 `plugin_type()` 实例化。插件必须提供零参数构造函数，或用零参数包装类从项目规定位置读取非密钥配置。需要通用构造参数注入时属于后续加载器增强，不能在文档中假定已经支持。

## 5. 维度责任与融合

接入前先更新或核对 `docs/judge-responsibilities.md`：

- 一个细则由某个Judge独立主判时，保留它自己的判断和证据；
- 多个Judge覆盖同一细则时，当前按细则算术平均融合，并保存每个Judge身份和证据；
- 不得仅因为新增主Judge就丢弃同维度其他平行细则；
- 插件输出细则分，Fusion等权汇总可评细则为维度分，最终总分由Score Schema、场景权重和不可评处理共同产生。

如果现有融合合同不能表达新关系，应先提出融合规则建设工作包，而不是在插件里读取其他Judge结果后自行算总分。

## 6. 最小测试集

每个新Judge至少覆盖：

1. 正常输入得到符合Schema的判断；
2. 缺少必需输入返回不可评；
3. 损坏视频、解码失败或模型加载失败；
4. 输出分数范围、维度ID和模式正确；
5. 相同输入和版本得到可重复结果，或明确记录随机种子；
6. 超时和资源不足不会无限等待；
7. Manifest与 `config/judges.json` 的ID、版本、维度一致；
8. Fake不依赖GPU、网络或真实模型权重。

## 7. 离线验收和Shadow验证

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m xmax_test judges list --root .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m xmax_test judges check --root .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

再用固定校准集完成：

- 与人工评语或已接受标注的一致性检查；
- 对典型Good、Bad和边界样本的证据复核；
- 新旧Judge版本并行Shadow对比；
- 失败、不可评和耗时比例统计；
- 回滚到旧Judge版本的验证。

当前 `judges check` 不是完整模型权重、GPU和真实媒体的端到端检查。报告中必须注明实际验证层级。
