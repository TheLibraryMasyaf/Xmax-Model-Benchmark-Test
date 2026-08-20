# 飞书数据库维护

本文只说明飞书素材读取、默认目标Base、Feed/Prompt/Case三表投影、编号、附件、幂等和对账。生成与评测仍以本地不可变对象为事实源。

## 1. 默认目标与身份

默认目标Base：`https://zcn0qf3aul03.feishu.cn/base/NdlnbJNiwadmlYsZDj8c9y7Hnhg`。

| 业务表 | table_id | 用途 |
| --- | --- | --- |
| Feed数据 | `tblE5VK7UH4aRHtU` | Feed素材及覆盖标签 |
| Prompt数据 | `tblfeF0q2vqMn2S8` | Prompt文字、素材及覆盖标签 |
| Case数据 | `tblohc666GKQCi1A` | 每一次实际生成尝试及其结果 |

飞书读写默认使用`lark-cli --as user`。用户可在`config/feishu.json`覆盖目标Base或表映射；没有覆盖时使用上述默认目标。真实凭据只从`.env`或lark-cli身份读取。适配器使用`base +field-list/+record-list/+record-upsert/+record-upload-attachment/+record-download-attachment`、`sheets +cells-get`和`wiki +node-get`；任何写入前先读真实字段结构。

## 2. 飞书既是来源也是协作投影

素材来源至少支持：

- 电子表格Sheet：读取完整有效区域、单元格图片和附件。
- 多维表格Base：分页读取记录、文本、选项和附件。
- Wiki：先解析到底层对象，再路由到Sheet、Base或文档适配器。

常规数据流是从Feed/Prompt表全量下载并冻结素材快照，把每次生成/导入的Run上传或回写到Case表。当前`FeishuSyncService` 的业务写路径以Case表为默认目标；Feed/Prompt表是素材来源和对账对象，不会在一次Case同步中被意外改写。

本地SQLite、原始下载、GenerationRun和EvaluationResult仍是可重建事实源；飞书是团队维护入口和最终查看面。同步不得用飞书行号作为业务ID。

## 3. Feed数据投影

固定业务字段：`feed编号`、`feed文件`、`内容tag`、`测试tag`、`是否是测试数据`。

- `feed编号`按最终入库顺序连续编号，至少三位。
- `feed文件`可以是图片或视频；内容哈希相同视为同一素材版本。
- 内容tag描述主体和内容；测试tag描述构图、光线、动态、分辨率等测试结构。
- 只有`是否是测试数据=是`的记录进入默认覆盖式计划。

## 4. Prompt数据投影

固定业务字段：`prompt编号`、`prompt文字`、`prompt素材`、`内容tag`、`测试tag`、`是否是测试数据`。

- `prompt文字`必须是可直接提交的祈使指令，不能只写玩法、歌曲或舞蹈名称。
- `prompt素材`允许为空，也允许图片或视频。
- Prompt按“规范化文字 + 素材内容哈希”去重。
- 玩法名先由Operation Recipe解析成默认Prompt和输入绑定；无法命中时进入待补充清单，不由Agent临场猜API参数。

## 5. Case数据是一条Run一行

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| case编号 | 文本 | `feedXXX_promptYYY`或带连续后缀 |
| case文件 | 附件 | 本次生成结果；生成失败时允许为空 |
| case评分 | 百分比 | 单次Run最终分；未重评为空，生成失败为0% |
| case说明 | 文本 | 本次结果问题、优点或失败原因 |
| feed文件 | 附件 | 对应Feed；视频参考配方还要附Feed截图 |
| prompt文字 | 文本 | 实际提交的Prompt |
| prompt素材 | 附件 | 实际使用的Prompt图片或视频 |
| Xmax模型版本 | 文本 | 必须以`x`开头 |

Case表不保存重复组平均值、中位数或其他组统计。每一次测试单独占一行；报告器按需读取这些独立事实进行聚合。

## 6. Case编号与重复

- 只计划一次且没有同组合历史结果时可用`feed002_prompt037`。
- 计划多次时在冻结TestPlan阶段直接分配`_01..._NN`。
- 后续同版本、同Feed、同Prompt再次测试时，完整读取现有编号后从最大后缀继续。
- 生成失败也占用后缀并写Case：`case文件`为空、`case评分=0%`、`case说明`保存失败分类。
- 重试创建新Run和新Case编号，不覆盖失败记录。
- 飞书幂等唯一键是`case编号 + Xmax模型版本`。
- 本地Case产物按`Case数据/Xmax x版本号/`分目录；不同模型版本可以出现同一case编号。

## 7. 单次评分与报告聚合边界

Case评分只取当前Score Schema声明的`case_score_output`，当前Shadow配置为Scenario Score，并以百分比展示。项目内部`case_score_percent`范围为0–100；飞书百分比数值单元是0–1，因此写入前必须除100，读回后必须乘100。例如内部85分写入`0.85`，飞书显示`85.00%`；不得直接写入`85`。内部0/1/2细则、Canonical Score、权重和证据仍保存在EvaluationResult，不投影成Case表固定列。

存量Case在没有按新Benchmark重评前，`case评分`保持空值。空值表示“未重评”，0%表示“已执行但生成失败、结果无效或最终得分为0”，两者不得混用。

版本报告才按`模型版本 + feed编号 + prompt编号`分组。主统计为包含失败0%的算术平均；详细数据包含逐Run百分比、样本数、中位数、最小值、最大值、标准差、P25/P75、成功率和Hard Gate失败数。聚合结果不回填任一Case行。

## 8. 附件与输入角色

- Feed、Prompt和Result使用独立附件字段。
- Artifact内部允许保存为`source.bin`，但飞书附件不得暴露内部名。上传前必须按文件签名/MIME确认真实格式，并使用业务名：Result为`<case编号>.<真实扩展名>`，Feed为`<feed编号>.<真实扩展名>`，Prompt素材为`<prompt编号>_prompt素材_<两位序号>.<真实扩展名>`。
- 图片参考配方：被编辑视频和预期音轨来源都是Feed。
- 视频参考配方：被编辑视频和预期音轨来源都是Prompt视频，Feed视频截图作为参考图；Case的`feed文件`同时保存原Feed和实际使用截图，截图名固定为`<feed编号>_feed截图.jpg`。
- 上传前验证媒体和SHA-256；大型日志只写URI或摘要附件。
- 附件上传和业务字段upsert分阶段记录；附件复用的作用域至少包含目标Base、表、记录、字段、SHA-256和业务文件名，同一素材用于不同重复Run时仍必须逐行执行附件写入。
- 回读发现附件名或数量不符合本Case的冻结输入时，`full`同步精确移除该字段中的错误附件并重新上传；不能仅因附件数量相同就判定完成。

## 9. 同步状态与幂等

```text
pending → uploading_attachments → upserting_fields → verifying → synced
                                                      ↘ error
                                                      ↘ conflict
```

1. 写入前读取真实表和字段类型，禁止仅凭文档猜测远端结构。
2. 完整读取已有编号和附件，确定Feed/Prompt续号及Case后缀。
3. 计算payload hash；相同则跳过。
4. 文本字段先upsert，附件后上传并回填token。
5. 每批最多200条，同一表串行写入。
6. 保存`来源候选 → 最终编号 → record_id → attachment token`进度。
7. 中断后从Ledger续跑，不重新编号或重复建行。

## 10. 回读与完成条件

写入后完整回读三张表并验证：

- `has_more=false`，记录数、编号唯一性和连续性正确。
- `case编号 + Xmax模型版本`无冲突。
- 必填文字、附件数量、文件名和token与本地清单一致。
- 多附件Case包含规定的Feed原文件和截图。
- 至少抽样下载远端附件并重新验证媒体。
- 本地有效、清单完整、远端写入成功、远端回读一致四项同时成立。

任何删除、历史编号重排或旧字段清理都需要单独明确授权；常规同步只追加或幂等更新。

## 11. 独立同步策略

飞书写入只能发生在Run Request显式包含`sync`阶段时。`none`不写远端；`score_only`只根据来源record ID或`case编号 + Xmax模型版本`更新已有Case的`case评分`和评测说明，不新建记录、不上传附件；`metadata_only`只写允许文本/数值；`attachments_only`只补已定位记录附件；`full`执行完整投影。

评分同步必须显式选择一个`evaluation_batch_id`，并按`run_id`传递该批次中的精确EvaluationResult；禁止在同步阶段自动读取“最新评分”或在多个评分中择优。只选择`run_batch_id`时视为上传未评测Run：完成项的`case评分`和`case说明`均为空，历史误写分数和“已生成”占位说明会被清空；生成失败仍写0%及具体失败分类。同一个Run可以重复评测，但始终对应同一Case行；只有重新生成形成的新Run才分配新的`_XX`编号和独立Case行。

`ingest results`对飞书永远是只读，它与`sync`使用不同Adapter权限边界。
