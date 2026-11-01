# WeChat Export Tool

面向 **Windows x64 微信 4.x** 的本地聊天记录导出工具。通过桌面界面或 CLI 选择一个联系人或群聊，将本机已保存的消息导出为结构化 JSON，以及可以直接打开的含附件离线网页。

This local Windows WeChat exporter includes a native desktop UI and an automation-friendly CLI. It exports a selected conversation to JSON and an offline HTML viewer, with locally available attachments.

目前为早期版本，微信版本、数据库代次和附件缓存状态都会影响兼容性。未知、空白或无法解析的消息仍进入导出列表，以占位文字和 `parse_status: "unparsed"` 标记，并保留原始行；未能导出的附件也会标注原因。启动成功或找到数据库不代表完整导出成功。

## 能做什么

- 枚举本地账号，按备注、昵称或会话标识查找联系人与群聊；同名时明确选择。
- 导出 `messages.json`、`raw_messages.jsonl`、`manifest.json` 和 `index.html`。
- 导出可定位的本地图片、表情、语音、视频和文件；语音转换为 WAV，必要时生成适合浏览器播放的视频副本。
- 离线网页支持分页、关键词和日期筛选、图片放大、音视频播放和文件下载。
- 校验数据库副本、消息与附件的关联、导出文件路径及媒体格式，显示缺失原因。

常规导出读取本地文件和已有密钥缓存，不自动重启微信、不自动启动调试取钥，也不从远端 CDN 下载缺失附件。安装依赖、构建取钥程序需要网络；查看导出网页不需要网络。

## 下载 Windows 便携版

从 [GitHub Releases](https://github.com/PhTirst/wechat-export-tool/releases) 下载 `WeChatExportTool-v0.2.0-windows-x64.zip`，**解压整个目录**后双击 `WeChatExportTool.exe`。发布包包含 Python/Tk、取钥程序和 Node/SILK 运行时，用户无需安装 Python、Go 或 Node.js。

1. 在“微信数据目录”中选择包含账号子目录的 `xwechat_files`，点击“刷新账号”并选择正确账号。
2. 如密钥尚未就绪，点击“获取本机密钥”，等待监听就绪后按提示在本机微信中操作。程序不自动退出或重启微信；“停止监听”会等待取钥程序安全结束。
3. 点击“读取联系人 / 群聊”，通过搜索和类型筛选选择一个会话。同名会话应根据稳定标识区分。
4. 选择“导出保存到”的父目录，点击“导出所选会话”。每次导出会新建带时间戳的目录。
5. 完成后点击“打开离线网页”，备份时复制整个导出目录。

同目录的 `wechat-export.exe` 是供脚本和 AI 工具调用的控制台程序。请保留 `_internal`、`runtime` 等目录，不要只复制一个 exe。

```powershell
.\wechat-export.exe doctor --json
.\wechat-export.exe accounts --json
.\wechat-export.exe --account ACCOUNT_DIRECTORY conversations --query "示例好友" --kind contact --json
.\wechat-export.exe --account ACCOUNT_DIRECTORY export --conversation-id CONVERSATION_ID --output .\exports\selected-chat-001 --json
```

`ACCOUNT_DIRECTORY` 和 `CONVERSATION_ID` 是占位符，应替换为列表返回的稳定 ID。`--json` 模式不进行交互选择；成功结果写入标准输出，取钥进度写入标准错误，失败使用非零退出码。`doctor` 只检查运行时，不读取账号或密钥。

详细操作与构建见 [Windows 使用说明](docs/WINDOWS_RELEASE.md)，机器调用合同见 [CLI 自动化说明](docs/CLI_AUTOMATION.md)。运行 `WeChatExportTool.exe --demo` 可以仅用合成数据体验界面，不读取真实微信账号。FFmpeg/ffprobe 是否随具体发行包提供，以包内清单和 [运行时说明](docs/WINDOWS_RUNTIME_LICENSES.md) 为准；缺少它们时，特殊视频或特殊图片的转换可能不可用。

## 从源码安装

普通下载用户使用上面的 Windows 便携包即可，以下步骤供开发者使用。

需要 Windows x64、Python 3.11 或更新版本，以及 Git。语音导出另需 Node.js 18+。FFmpeg 和 ffprobe 可选，用于视频兼容转换和媒体校验；请自行安装支持所需编解码器的发行版本。

克隆或下载本仓库后，在项目根目录运行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
npm ci
wechat-export --help
```

不使用 PowerShell 激活脚本时，也可以直接调用 `.\.venv\Scripts\python.exe` 和 `.\.venv\Scripts\wechat-export.exe`。

Python 安装会从固定 Git 提交安装 `chatlog-keeper`。`npm ci` 安装固定版本的 `silk-wasm`，只在需要导出语音时使用。来源、版本与许可证见 [依赖说明](docs/DEPENDENCIES.md)。

### 构建取钥程序

需要 **Go 1.26+** 和 Git：

```powershell
.\scripts\build_capture.ps1
```

脚本获取固定版本的 WxMori、应用本仓库的兼容性补丁，生成 `bin/wxmori-media.exe`。源码树不提交微信、运行时或预编译取钥程序；Windows 发布资产另行包含经过清单核对的运行组件。

也可以通过 `--capture-exe` 指定自行构建的兼容程序；未应用补丁的上游程序不一定覆盖相同的媒体数据库。

## 使用

下面使用源码安装后的 `wechat-export` 命令。便携版将其替换为 `.\wechat-export.exe` 即可；参数与导出引擎相同。

以下 `ACCOUNT_DIRECTORY` 和 `CONVERSATION_ID` 都是占位符，请替换为列表中实际返回的值。

### 1. 选择本地账号

```powershell
wechat-export accounts
```

默认数据根目录为当前用户目录下的 `xwechat_files`。如微信文件保存在其他位置：

```powershell
wechat-export --data-root D:\WeChatData accounts
```

多个账号时可以按提示选择，也可以显式传入 `--account ACCOUNT_DIRECTORY`。非交互调用必须提供足够的信息，工具不会默认选第一个同名账号或会话。

### 2. 显式取钥（需要时）

```powershell
wechat-export --account ACCOUNT_DIRECTORY --capture-exe .\bin\wxmori-media.exe capture --timeout 300
```

取钥是独立的实验性操作。等待程序提示监听就绪后，再按提示在本机操作微信。某些数据库只在查看对应历史或播放语音时加载。`Ctrl+C` 可停止监听；成功补到部分密钥仍可能不足以导出全部内容。

本机同时运行多个微信进程时，可以显式限定目标 PID：

```powershell
wechat-export --account ACCOUNT_DIRECTORY --capture-exe .\bin\wxmori-media.exe capture --pid 12345 --timeout 300
```

图片取钥是额外的实验分支。已知会话标识时，可用 `capture --conversation-id CONVERSATION_ID` 只为该会话自动准备图片密文探针；`--images-probe PATH` 则允许高级用户显式提供探针，两者互斥。没有验证通过的图片密钥时，加密图片会被标记为缺失，不会冒充成功导出。

### 3. 查找联系人或群聊

```powershell
wechat-export --account ACCOUNT_DIRECTORY conversations --query "示例好友" --kind contact
wechat-export --account ACCOUNT_DIRECTORY conversations --kind group
```

列表会显示会话名称、类型和稳定标识。名称相同也应按标识区分。

需要继续尝试该会话的图片密钥时：

```powershell
wechat-export --account ACCOUNT_DIRECTORY --capture-exe .\bin\wxmori-media.exe capture --conversation-id CONVERSATION_ID --timeout 300
```

等待监听就绪后，再在该会话中打开本地图片。这个分支的成功与否仍取决于客户端版本和图片格式，不保证补齐全部附件。

### 4. 导出

交互选择联系人或群聊：

```powershell
wechat-export --account ACCOUNT_DIRECTORY export --output .\output
```

按已知会话标识导出，适合非交互运行：

```powershell
wechat-export --account ACCOUNT_DIRECTORY export --conversation-id CONVERSATION_ID --output .\output
```

输出目录应为新的导出目录。可省略 `--output`，使用当前目录 `exports` 下带上海时区时间戳的目录。

全局路径参数包括 `--ffmpeg`、`--ffprobe`、`--node` 和 `--silk-module`。例如，Node 不能自动解析 `silk-wasm` 时，可以明确指定安装后的入口：

```powershell
wechat-export --account ACCOUNT_DIRECTORY --silk-module .\node_modules\silk-wasm\lib\index.cjs export --conversation-id CONVERSATION_ID --output .\output
```

### 5. 打开网页与备份

用浏览器直接打开输出目录中的 `index.html`，无需本地 HTTP 服务。移动或备份时应复制整个导出目录，保留附件相对路径。

| 文件 | 用途 |
|---|---|
| `index.html` | 含附件的离线查看器 |
| `messages.json` | 消息列表、未解析消息占位项与附件导出状态 |
| `raw_messages.jsonl` | 所选会话的原始数据库行，二进制字段用 Base64 表示 |
| `manifest.json` | 消息数量、附件状态、缺失原因等统计 |
| 附件目录 | 网页引用的图片、音频、视频与文件 |

JSON 字段与示例见 [JSON 格式说明](docs/JSON_FORMAT.md)。附件是否可用以 `media[].status` 为准；原始媒体元数据中的旧处理提示不代表当前导出结果。

所有可读日期及导出时间使用 `Asia/Shanghai` 时区。`messages.json` 的 `attachment_meta` 会移除 `aeskey`、`aes_key`；`raw_messages.jsonl` 忠实保留原始行，可能仍含原消息的媒体属性。工具不会把数据库页密钥写入导出文件。

### 保存配置

```powershell
wechat-export config show
wechat-export config set --account ACCOUNT_DIRECTORY --capture-exe .\bin\wxmori-media.exe
wechat-export config set --clear-account
```

默认配置保存在 `%LOCALAPPDATA%\WeChatExportTool\config.json`，缓存也位于用户数据目录。配置不包含明文数据库密钥。可通过 `--config`、`--cache-dir` 覆盖位置。

## 兼容范围与已知限制

- 目标平台是 Windows x64 微信 4.x。微信更新可能改变取钥位置、数据库或媒体格式，支持范围不等于所有 4.x 版本。
- 只能导出本机实际保存的历史与附件。手机上仍有、电脑上未同步的内容不会自动出现。
- 数据库密钥与图片密钥可能不同；取得聊天正文不代表能解开全部附件。
- 表情、转发卡片和特殊媒体格式可能仍无法解析。消息保留占位项，`parse_status` 标记为 `unparsed`，原始行也会保留；附件则使用独立的缺失或未支持状态。
- 动态取钥、特定进程筛选和图片探针仍是实验功能。合成测试验证接口与数据处理，不代替所有客户端版本和真实账号的实测。
- 导出结果是明文。友好 JSON 已移除上述媒体密钥字段，但无损原始行归档可能包含原消息媒体属性；请把整个导出目录当作私人备份保存。本仓库不收集或上传这些内容。

## 开发与验证

源码图形界面和合成演示：

```powershell
.\.venv\Scripts\wechat-export-gui.exe
.\.venv\Scripts\python.exe -m wechat_export_tool.gui --demo
```

不读取微信文件，也可以先生成含图片、声音和文件的合成演示：

```powershell
python scripts/create_demo.py
Start-Process .\exports\demo\index.html
```

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

测试应使用合成账号、消息、数据库和媒体，不将真实聊天记录、密钥、数据库或本机账号路径提交到仓库。行为改动应同时验证单会话隔离、原始记录保留、附件路径与离线网页。

## 来源与许可证

本项目以 **GPL-3.0-only** 发布，见 [LICENSE](LICENSE)。它是在开源项目基础上实现的适配与导出工具，不宣称底层微信解密或媒体编解码算法为本项目独创。

主要依赖为 [chatlog-keeper](https://github.com/labazhou2024/chatlog-keeper)、[WxMori](https://github.com/axiomzero-qed/WxMori)、[silk-wasm](https://github.com/idranme/silk-wasm) 及 Python、Tk、Node.js、Pillow 等通用组件；FFmpeg/ffprobe 用于可选的媒体转换与探测。媒体格式适配还参考了 [WeChatDecrypted](https://github.com/kclx/WeChatDecrypted) 和 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) 的公开格式研究。

依赖与参考项目保留各自许可证。精确提交、职责边界和原始许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与 [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md)。
