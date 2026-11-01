# CLI 与 AI 工具调用

Windows 发布包中的 `wechat-export.exe` 是控制台程序，使用与桌面界面相同的配置、导出引擎和本机受保护缓存。无需安装 Python、Go 或 Node.js；请保留完整解压目录。

## 最小流程

以下命令在解压后的 `WeChatExportTool` 目录中运行。自动化程序应使用 exe 的绝对路径，分别捕获标准输出、标准错误与退出码。

```powershell
.\wechat-export.exe doctor --json
.\wechat-export.exe accounts --json
.\wechat-export.exe --account ACCOUNT_DIRECTORY conversations --query "示例好友" --kind contact --json
.\wechat-export.exe --account ACCOUNT_DIRECTORY conversations --kind group --json
.\wechat-export.exe --account ACCOUNT_DIRECTORY export --conversation-id CONVERSATION_ID --output .\exports\selected-chat-001 --json
```

`ACCOUNT_DIRECTORY` 和 `CONVERSATION_ID` 是占位符，必须替换为列表返回的稳定 ID。名称可能重复，不应根据昵称自动选择第一个结果。`--kind` 接受 `all`、`contact`、`group`。

`--json` 模式不请求交互输入。多个账号未指定 `--account`，或多个匹配会话未指定 `--conversation-id`，均返回非零退出码。自动化宜始终传入这两个 ID，并为 `--output` 指定新的空目录。

数据目录不是默认位置时，可加 `--data-root D:\WeChatData`。输出目录不得位于微信账号数据内。省略 `--output` 时，CLI 在当前工作目录的 `exports` 下新建带上海时间戳的目录；脚本宜显式传绝对路径。

## JSON 合同与退出码

`--json` 下，业务命令将一份完整 JSON 值写入标准输出。成功结果保留各命令原有形状：

| 命令 | 标准输出 |
|---|---|
| `doctor` | 运行时、能力、命令与 JSON 合同对象 |
| `accounts` | 账号数组，含 `account_id`、`path`、`display_name` |
| `conversations` | 会话数组，含 `conversation_id`、`display_name`、`kind` 等 |
| `export` | 结果对象，含 `status`、`output_directory`、`messages`、`counts`、`missing_reasons`、`verified` |
| `capture` | 捕获状态对象，含 `returncode`、`status`、数据库数量和取消状态等 |
| `config show` / `config set` | 有效配置对象 / 保存结果对象 |

取钥进度只写标准错误，每行一个 JSON 对象，例如 `{"capture":{"state":"watching_login","ready_for_login":true}}`。不要合并两条流后再解析一个 JSON 文档。标准输出可以包含缩进与换行，应在进程结束后整体解析。

| 退出码 | 含义 |
|---|---|
| `0` | 命令完成 |
| `1` | 操作失败，或捕获器返回失败状态 |
| `2` | 参数、配置或目标选择无效/不完整 |
| `130` | 用户取消 |

参数、配置或执行异常通常返回以下错误结构：

```json
{
  "schema_version": 1,
  "ok": false,
  "exit_code": 2,
  "error": {
    "code": "invalid_arguments",
    "message": "存在多个微信账号；请先运行 accounts，再使用 --account 指定账号。"
  }
}
```

常见 `error.code` 为 `invalid_arguments`、`invalid_configuration`、`cancelled`、`operation_failed`；业务错误也可能使用安全的具体代码，例如 `database_key_missing_run_capture`。

`capture` 非零退出也可以携带正常状态对象。取消时结果可含 `cancelled: true`、`returncode: 130`、`status: "stopped"`。先检查进程退出码，再按实际结果形状处理，不能假设所有非零结果均含 `error` 字段。

`--help` 和 `--version` 保留面向人的文本输出。机器能力探测使用 `doctor --json`，不要把帮助文本作为 JSON 接口。

## 运行时探测

`doctor --json` 返回 `schema_version`、`version`、`frozen`、`platform`、`tools`、`capabilities`、`commands`、`json_contract`。工具项报告是否存在、路径及来源。此命令不读取账号、数据库或密钥缓存，也不启动取钥。

`voice_decode` 表示 Node/SILK 文件就绪；`video_transcode` 与 `media_probe` 分别反映 FFmpeg、ffprobe 可用性。文件存在不代表微信版本兼容或数据库密钥已加载，实际结果仍以导出状态为准。

## 显式取钥与取消

```powershell
.\wechat-export.exe --account ACCOUNT_DIRECTORY capture --timeout 300 --json
.\wechat-export.exe --account ACCOUNT_DIRECTORY capture --conversation-id CONVERSATION_ID --timeout 300 --json
```

取钥需要用户按就绪提示在本机微信中配合操作。某些数据库仅在查看历史或播放语音时加载。程序不会自动退出或重启微信。`--timeout` 为 1–600 秒；多个微信进程时可用 `--pid` 限定目标。

`--conversation-id` 只为目标会话准备图片密文探针，与高级选项 `--images-probe PATH` 互斥。图片密钥捕获仍属实验功能，不保证补齐全部附件。

交互控制台使用 `Ctrl+C` 停止，程序会先请求取钥子进程安全解除监听并退出。GUI 对应“停止监听”。调用方应为清理留出时间；普通取消不要采用强制终止进程。

## 配置与位置

```powershell
.\wechat-export.exe config show --json
.\wechat-export.exe config set --data-root D:\WeChatData --account ACCOUNT_DIRECTORY --json
.\wechat-export.exe config set --clear-account --json
```

默认配置在 `%LOCALAPPDATA%\WeChatExportTool\config.json`，与 GUI 共用。`--config PATH` 可使用单独配置；`--cache-dir PATH` 改变本机受保护缓存位置。配置不接受明文数据库密钥。

便携包自动定位 exe 旁的运行时，不要求加入 `PATH`。显式 `--capture-exe`、`--node`、`--silk-module`、`--ffmpeg`、`--ffprobe` 优先；自动定位的默认路径不会随配置固化到旧的便携目录。

## 判断结果

`export` 退出码 `0` 且 `verified: true` 表示本次导出通过校验，不代表全部手机历史已同步或附件齐全。继续读取 `missing_reasons` 与 `manifest.json` 中的 `unparsed_by_type`。逐条字段见 [JSON 格式说明](JSON_FORMAT.md)。导出 JSON、HTML、原始行和附件均为私人明文备份。
