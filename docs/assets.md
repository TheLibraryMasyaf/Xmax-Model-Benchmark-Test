# 素材管理

本文只说明素材下载、校验、去重、登记和可用性；不负责测试组合或生成。

## 1. 素材类型

- Feed视频：模型输入视频或实时测试输入源。
- Prompt文字：生成指令。
- Prompt图片：人物、角色、服装、风格等参考。
- Prompt视频：动作、舞蹈、运镜、口型等动态参考。
- mask视频：需要时传给离线推理。
- 场景元数据：主体类型、运动、光照、镜头、互动等标签。

## 2. 下载适配器

素材来源应通过适配器接入：

```python
class AssetSource:
    def list(self, query) -> list[RemoteAsset]: ...
    def download(self, remote, destination) -> DownloadResult: ...
```

必须实现的适配器：

- 飞书电子表格（Sheet）单元格图片、附件和视频。
- 飞书多维表格（Base）附件、图片和文本字段。
- 飞书Wiki链接解析器：先解析到底层Sheet、Base或文档，再交给对应适配器。
- 已有本地 Feed目录。
- 显式 HTTP(S) URL。

`feishu_sheet`、`feishu_bitable`和`feishu_wiki`必须是不同`kind`，不能用一个含义不清的`feishu_table`让执行Agent猜远端对象类型。Sheet读取必须检查`has_more`、`truncated`、`actual_range`、`row_indices`和`col_indices`；Base必须分页到`has_more=false`。飞书读写默认显式使用`lark-cli --as user`。

下载过程必须幂等：同一远端版本已经存在且哈希一致时跳过；内容改变则生成新 `asset_id`。

## 3. 下载后校验

视频至少检查：

- 文件签名和可解码性。
- 字节数、SHA-256。
- 时长、宽高、FPS、视频流。
- 音频流是否存在、采样率和时长。
- 关键时间点能否成功解码。

图片至少检查：格式、宽高、色彩通道、解码和哈希。

`curl`或下载命令成功退出不等于素材完整，只有媒体校验通过后才能标记为 `ready`。

## 4. 素材状态

```text
discovered → downloading → downloaded → validating → ready
                                      ↘ invalid
                                      ↘ quarantined
```

- `invalid`：确定不可用，如格式错误或内容为空。
- `quarantined`：暂时无法验证或身份/授权存疑，不进入默认测试计划。

## 5. Asset Manifest

每个素材记录：

```json
{
  "asset_id": "asset_...",
  "kind": "feed_video",
  "uri": "var/artifacts/assets/asset_.../source.mp4",
  "sha256": "...",
  "source": {
    "type": "feishu",
    "record_id": "...",
    "field": "Feed"
  },
  "status": "ready",
  "media": {
    "duration_s": 8.17,
    "width": 704,
    "height": 1280,
    "fps": 24,
    "has_audio": false
  }
}
```

## 6. 飞书同步边界

Asset模块只生成本地台账和同步事件；真正的飞书写入由Feishu Adapter完成。Asset模块不得依赖飞书列号或表格布局。

每个飞书来源快照必须保存来源URL、对象类型、revision、表/Sheet ID、真实行号或record ID、字段、附件token、文件名和字节数。下载缓存以附件token索引，内容去重最终以SHA-256为准。来源没有完整读取时不得报告全量完成。
