# 外部插件式输入

本文只定义仓库外允许提供的内容。除这些文件和密钥外，建设与运行不得依赖历史聊天知识。

## 1. Benchmark Pack

文件：`BENCHMARK.md`。定义维度、Judge路由、权重Profile、场景权重规则、硬门槛和Score Schema。机器合同位于固定标记间；更新后运行 `benchmark-check`。

## 2. Scenario Pack

文件：`config/scenarios.json`，Schema为 `schemas/scenario-pack.schema.json`。仓库已提供与当前Shadow Benchmark配套的`0.1.0-draft`版本；后续可以用新版本替换。它定义标签枚举和场景，不定义权重，Benchmark规则只能引用此包中存在的场景ID、标签和值。

## 3. Asset Source Pack

文件：`config/asset-sources.json`。定义本地目录、飞书电子表格、飞书多维表格、Wiki解析入口或HTTP来源以及字段映射。模板：`config/asset-sources.example.json`。

路径和飞书ID属于配置，不应写入下载器代码。

## 4. Operation Recipe Pack

文件：`config/operation-recipes.json`，Schema为`schemas/operation-recipes.schema.json`。仓库提供默认版本，定义玩法别名、默认/允许模式、可执行Prompt、被编辑视频、预期音轨来源、离线API绑定和实时互动Profile。新增玩法必须通过新版本配方接入，不能让Runner或Agent临场猜测。

## 5. Judge Pack

文件：`config/judges.json`。列出已部署的CV、Codex、Metric和Fusion Judge。每项满足Judge Manifest；禁用项不参与覆盖率。

插件代码可以安装在独立Python环境或服务中，但必须提供稳定entrypoint、版本和fake测试替身。

实时互动Profile位于`config/interaction-profiles.json`，Schema为`schemas/interaction-profiles.schema.json`。Operation Recipe只引用版本化Profile ID，不在Runner内写死滑动坐标。

## 6. Feishu Pack

文件：`config/feishu.json`。定义Base、Feed/Prompt/Case三表、字段投影和Case评分来源。模板已经包含模型测试数据库默认目标；用户可覆盖。真实凭据从`.env`或lark-cli用户身份读取。没有该包时，本地工作流仍可运行，但Run Request不能包含`sync/reconcile`。

## 7. Existing Results Import Pack

可选文件：`config/existing-results.json`，Schema为`schemas/existing-results.schema.json`，模板为`config/existing-results.example.json`。用于把飞书Case数据、本地视频目录或已有Stage Manifest导入为可评测的completed Run Batch。

该配置必须明确来源、选择器、字段映射、生成模式解析顺序和校验策略。无法确定模式、Feed/Prompt、Recipe、被编辑视频或音轨来源时报错，不由Agent猜测。导入过程固定`write_remote=false`。

## 8. Run Request

文件：`config/run-request.json`，Schema为 `schemas/run-request.schema.json`。只描述本轮显式授权的`stages`、`stage_inputs`、依赖策略、同步策略、模式、重复数、过滤和dry-run/smoke，不复制Benchmark或场景内容。`execution_mode`默认`streaming`，`pipeline_queue_size`默认4；设为`batch`才使用整批屏障。

`dependency_policy` 固定为`explicit_only`，`missing_input_policy`固定为`error`。Selector遵循`schemas/pipeline-selector.schema.json`；动态过滤在执行前冻结。`sync_policy`为`none / score_only / metadata_only / attachments_only / full`。默认重复5次，可被本轮或单Case覆盖。模型版本更新请求增加`comparison`。

`filters`支持`feed_asset_ids`、`feed_limit`、`prompt_record_numbers`和`prompt_limit`。Limit在按业务编号稳定排序后应用，并进入Plan Hash；需要固定长期回归集时优先写明确ID/编号，需要快速抽取首批素材时使用Limit。

## 9. Secrets

`.env`允许：XMAX Key、飞书凭据、Codex路径和必要服务令牌。模板不包含真实值。密钥不能进入计划、Run记录、Codex Prompt、飞书字段或日志。

## 10. 接入检查

最终 `context-check` 应验证：

- JSON/Benchmark Schema。
- 文件路径和素材来源可访问性。
- Benchmark规则引用的维度、场景标签和Judge存在。
- Operation Recipe引用的素材角色、模式和互动Profile合法。
- Active维度覆盖率。
- 模式需要的Key和工具。
- `sync/reconcile`阶段需要的飞书表映射。
- 计费stage是否有预算预览。
- `report` stage是否提供两版模型、请求场景、可用比较策略和报告模板。
- 每个单独阶段的Selector是否能解析出全部必要上游产物，不自动补跑未授权阶段。
- `ingest results`是否能确定每条Case的输入、模式、Recipe、音轨基准和来源哈希。
- `sync`阶段与`sync_policy`是否匹配，`score_only`是否只命中已有Case。

检查应一次性报告全部问题，不让操作者逐个试错。

## 11. 文件与Schema对照

| 外部文件 | Schema/校验器 |
| --- | --- |
| `BENCHMARK.md` | `schemas/benchmark.schema.json` + Benchmark引用完整性检查 |
| `config/project.json` | `schemas/project-config.schema.json` |
| `config/scenarios.json` | `schemas/scenario-pack.schema.json` |
| `config/asset-sources.json` | `schemas/asset-sources.schema.json` |
| `config/operation-recipes.json` | `schemas/operation-recipes.schema.json` |
| `config/interaction-profiles.json` | `schemas/interaction-profiles.schema.json` |
| `config/judges.json` | `schemas/judge-registry.schema.json` |
| `config/feishu.json` | `schemas/feishu-config.schema.json` |
| `config/run-request.json` | `schemas/run-request.schema.json` |
| `config/existing-results.json` | `schemas/existing-results.schema.json` |

阶段产物不是用户手写配置：Stage Manifest由`schemas/stage-manifest.schema.json`校验，Selector快照由`schemas/pipeline-selector.schema.json`校验。

人工学习导出也不是手写配置：每行满足`schemas/learning-candidate.schema.json`，外部Challenger消费者必须同时校验`route_kind`、`data_partition`和`training_eligible`。

`.example.json`只用于复制和dry-run；真实文件不提交仓库。Schema验证使用项目依赖`jsonschema`的Draft 2020-12实现，跨文件引用相对`schemas/`解析。
