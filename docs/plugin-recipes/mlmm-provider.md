# MLLM Provider接入手册

本文只负责更换或新增MLLM传输Provider。MLLM如何组织视频、Feed、Prompt、操作说明和结构化输出，见 `docs/codex-mlmm.md`；维度责任分配见 `docs/judge-responsibilities.md`。

## 1. 先选择接入方式

| Provider能力 | 接入方式 | 是否需要新Python实现 |
|---|---|---|
| 支持OpenAI兼容Chat/Responses风格接口 | `openai_compatible` | 否 |
| 通过本地Codex CLI执行 | `codex_cli` | 否 |
| 私有HTTP协议、本地服务或其他SDK | `python_plugin` | 是 |

不要为了模型名称不同而创建新Provider；只有传输协议、媒体上传方式或响应协议无法由现有配置表达时才使用 `python_plugin`。

## 2. OpenAI兼容Provider

在 `config/judges.json` 中复制现有MLLM Judge为新版本，修改其 `provider` 配置。真实字段以 `schemas/judge-manifest.schema.json` 和现有示例为准，核心形态如下：

```json
{
  "judge_id": "mlmm-main",
  "version": "<新版本-shadow>",
  "kind": "mlmm",
  "enabled": true,
  "supported_dimensions": ["<维度ID>"],
  "supported_modes": ["offline", "realtime"],
  "provider": {
    "type": "openai_compatible",
    "endpoint": "<API地址>",
    "model": "<模型名>",
    "api_key_env": "<环境变量名>",
    "timeout_seconds": 300,
    "direct_media": true
  }
}
```

只保存环境变量名，不把Key写入JSON。模型是否支持视频直传、图片数量、Base64上限和JSON Schema响应，必须根据真实API能力配置；不能因为API接受请求就假定它理解了视频。

## 3. 自定义Python Provider合同

自定义类必须满足 `src/xmax_test/judges/mlmm/base.py` 中的 `MlmmProvider`：

```python
class CustomProvider:
    @property
    def provider_id(self) -> str:
        return "custom-provider"

    def complete_json(
        self,
        *,
        prompt: str,
        image_paths: list[str],
        output_schema: dict,
        media_inputs: list[dict] | None = None,
    ):
        ...
```

返回值必须是 `MlmmResponse`，至少保留：

- 解析后的JSON `payload`；
- 可审计的 `raw_text`；
- `provider_id` 和实际模型名；
- Token或计量 `usage`；
- 请求ID、回退次数等 `metadata`。

Provider只负责传输、媒体适配、结构化响应和错误归一化，不负责场景权重、总分或维度路由。

配置通过Provider entrypoint和kwargs注入：

```json
{
  "provider": {
    "type": "python_plugin",
    "entrypoint": "your_package.provider:CustomProvider",
    "kwargs": {
      "endpoint": "<非密钥配置>",
      "api_key_env": "<环境变量名>"
    }
  }
}
```

入口对象必须能被 `module:object` 导入，且构造函数接受配置中的 `kwargs`。

## 4. Provider必须处理的情况

- 文本、图片和视频角色保持分离，不把Feed、Prompt素材和结果视频混为同一无标签附件；
- 保留结构化操作说明，使模型知道生成任务实际执行了什么；
- 超时、限流、额度耗尽、认证失败和畸形JSON使用可区分的错误；
- 只有配置明确允许时才进行模型回退，并记录实际使用模型；
- 重试使用同一业务输入，不重复制造无法关联的评测结果；
- 不在日志中输出Key、完整认证Header或敏感URL参数；
- 无法直接识别视频时使用项目预处理产物，不私自改变采样语义。

## 5. 最小测试集

新增Provider必须有不访问外网的Fake或传输Stub，至少覆盖：

1. 正常结构化响应；
2. 视频或图片输入角色保持正确；
3. 超时后按上限重试；
4. 限流或额度耗尽后的允许回退；
5. 永久认证失败不无限重试；
6. 返回非JSON、缺字段或越界分数；
7. usage和实际模型名被保存；
8.日志不包含密钥。

## 6. 注册和离线验收

1. 使用新的Judge版本，先保持Shadow。
2. 在 `config/judges.json` 注册Provider和支持维度。
3. 确认旧Provider仍可作为回滚目标，不在同一版本下偷换模型。
4. 运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m xmax_test judges list --root .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m xmax_test judges check --root .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

当前 `judges check` 只确认注册表能够加载，不等价于真实API冒烟测试。必须另用不计费样本验证真实媒体上传和结构化响应，并明确区分Fake、离线Stub和真实API结果。

## 7. 当前边界

- `python_plugin` 已支持 `kwargs`，通常不需要修改Composition。
- 如果新Provider需要异步生命周期、常驻连接、批处理回调或当前合同之外的输入，属于接口扩展，应建立建设工作包。
- Provider切换不会自动证明新旧Judge可比；仍需校准集、留出集和Shadow对照。
