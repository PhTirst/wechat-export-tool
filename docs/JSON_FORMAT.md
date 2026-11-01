# 导出 JSON 格式

`messages.json` 使用 UTF-8，顶层是消息数组。字段沿用原导出原型，并增加实际附件结果 `media`、本地消息编号 `local_id` 和解析状态 `parse_status`。当前格式以本仓库的序列化代码为准，不是微信官方协议。

可读日期与导出时间均采用 `Asia/Shanghai`（UTC+08:00）。Unix 时间戳 `ts` 表示绝对时间，单位为秒。

## 消息字段

| 字段 | JSON 类型 | 含义 |
|---|---|---|
| `ts` | number | Unix 时间戳，秒 |
| `ts_iso` | string | `Asia/Shanghai` 时区、带 `+08:00` 偏移的 ISO 8601 日期时间 |
| `sender` | string | 发送者显示名 |
| `sender_wxid` | string | 发送者内部标识 |
| `chat_room` | string | 会话显示名；单聊也使用此字段 |
| `conversation_id` | string | 所选联系人的内部标识或群会话标识 |
| `content` | string | 可读正文或媒体占位文字，不是完整原始 XML |
| `msg_type` | integer | 消息类型 |
| `is_self` | boolean | 是否由当前账号发送；无法可靠确定身份时不得按昵称猜测 |
| `is_group_chat` | boolean | 是否为群聊 |
| `server_id` | string | 服务器消息编号，以字符串保存以避免大整数精度丢失；缺失时为空字符串 |
| `local_id` | integer / null | 原分片中的本地消息编号，不保证跨分片或跨账号唯一 |
| `attachment_meta` | object / null | 解析自原消息的媒体属性，字段随类型变化；移除 `aeskey`、`aes_key` |
| `parse_status` | string | `parsed`：已生成可读正文或媒体占位文字；`unparsed`：保留未解析占位项 |
| `media` | array | 此消息实际附件的导出结果，可以为空 |

常见 `msg_type`：`1` 文本、`3` 图片、`34` 语音、`42` 名片、`43` 视频、`47` 表情、`48` 位置、`49` 文件或卡片、`50` 通话记录、`10000` 系统消息。类型 `49` 还包含链接、引用、转发等子类型，不能将它全部视为文件。

`attachment_meta` 可包含图片标识、文件校验值、时长等属性，友好 JSON 中不会输出 `aeskey`、`aes_key` 字段。它仍保留解析阶段的信息；附件是否已成功导出，应读取 `media[].status`。

未知类型、空消息或无法解析的消息不会被静默省略。它们仍在数组中，`parse_status` 为 `unparsed`，`content` 使用说明性占位文字，完整原始行另存于 `raw_messages.jsonl`。`parsed` 也不等于附件可用：消息解析状态和媒体导出状态是两个独立字段。

## 附件字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `kind` | string | `image`、`audio`、`video` 或 `file` |
| `status` | string | `available`：导出且通过校验；`missing`：未能导出 |
| `path` | string | 相对导出目录的文件路径；缺失时为空 |
| `name` | string | 建议的显示或下载名称 |
| `mime` | string | MIME 类型，无法确定时可能为空 |
| `reason` | string | 缺失原因，或缩略图/预览等补充说明 |
| `thumbnail` | boolean，可选 | 是否仅找到缩略图 |
| `poster` | string，可选 | 视频封面的相对路径 |
| `original_path` | string，可选 | 另存原编码的相对路径 |

`path`、`poster` 和 `original_path` 都相对于导出文件夹，不能单独移动 `index.html`。一个消息可以有多个媒体条目，例如兼容播放副本和原视频下载项。

统计中的附件数量是消息引用数；不同消息可能引用同一个去重后的文件，引用数不等于磁盘上的唯一文件数。

## 虚构示例

下列标识、正文和路径均为虚构，`attachment_meta` 只展示部分属性：

```json
[
  {
    "ts": 1792382400.0,
    "ts_iso": "2026-10-19T12:00:00+08:00",
    "sender": "示例好友",
    "sender_wxid": "example-contact",
    "chat_room": "示例好友",
    "conversation_id": "example-contact",
    "content": "[语音 2s]",
    "msg_type": 34,
    "is_self": false,
    "is_group_chat": false,
    "server_id": "1234567890123456789",
    "local_id": 42,
    "parse_status": "parsed",
    "attachment_meta": {
      "kind": "voice",
      "voice_length_ms": 2400
    },
    "media": [
      {
        "kind": "audio",
        "status": "available",
        "path": "attachments/audio/example.wav",
        "name": "example.wav",
        "mime": "audio/wav",
        "reason": ""
      }
    ]
  }
]
```

## 原始行与导出统计

`raw_messages.jsonl` 每行保存所选会话的一条原始数据库行，包含来源分片、会话标识、发送者标识及 `row`。二进制字段以 `{"$bytes_base64":"..."}` 表示，避免有损转码。它是无损原始行归档，可能保留原消息中的媒体属性，包括未从原始 XML 中移除的媒体密钥值。工具不会向任何导出文件添加数据库页密钥。

`manifest.json` 当前 `schema_version` 为 `1`，汇总所选会话、消息数量、原始行数量、未解析消息类型、附件引用数量及缺失原因。常用字段有 `target`、`conversation_id`、`kind`、`generated_at`、`timezone`、`messages`、`raw_rows`、`unparsed_by_type`、`media_references`、`counts` 和 `missing_reasons`。

`messages` 包含未解析占位项。`generated_at` 与 `timezone` 使用 `Asia/Shanghai`。`status: "complete"` 表示导出流程与验证完成，不表示所有消息都已解析或所有附件都存在；应同时查看 `unparsed_by_type` 和 `missing_reasons`。这些统计不能用来推断手机端或服务器端历史是否完整。
