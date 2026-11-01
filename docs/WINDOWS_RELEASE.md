# Windows 便携版使用与构建

## 下载启动

从 [GitHub Releases](https://github.com/PhTirst/wechat-export-tool/releases) 下载 `WeChatExportTool-v0.2.0-windows-x64.zip`，解压后双击其中 `WeChatExportTool/WeChatExportTool.exe`。这是包含运行时的 Windows x64 便携包，无需安装 Python、Go 或 Node.js。

```text
WeChatExportTool/
  WeChatExportTool.exe       桌面界面
  wechat-export.exe          控制台 CLI
  _internal/                共享 Python、Tk 和 Python 依赖
  runtime/                  Node、SILK、取钥程序等运行组件
  licenses/                 本项目和直接来源的许可证
  third-party-licenses/      捆绑组件的许可证与声明
  bundle-manifest.json       本次构建的运行时和文件清单
```

两个 exe 共用同目录资源，不能单独复制。移动时应移动整个 `WeChatExportTool` 目录。软件包不包含微信、账号缓存、真实聊天、数据库或密钥。

Release 的 `SHA256SUMS.txt` 提供下载包校验值：

```powershell
Get-FileHash .\WeChatExportTool-v0.2.0-windows-x64.zip -Algorithm SHA256
```

FFmpeg/ffprobe 是否随具体包提供、精确组件版本和许可范围，以包内清单及 [Windows 运行时说明](WINDOWS_RUNTIME_LICENSES.md) 为准。FFmpeg 是可选组件；已有且浏览器支持的视频可原样导出，特殊编码的兼容转换和特殊图片预览可能需要它。

## 图形界面

“微信数据目录”应选择包含本地账号子目录的 `xwechat_files`，不是账号内部的 `db_storage`。可在微信设置中确认本机位置，再用“浏览…”选择。点击“刷新账号”，从“本地账号”下拉框选择正确账号；“保存设置”供下次启动和 CLI 共用。

已有有效缓存可直接读取会话。密钥尚未就绪时，点击“获取本机密钥”，等待界面提示监听就绪后，按提示在微信中操作。部分历史或语音数据库要查看、播放后才加载。程序不自动退出登录、重启微信或下载远端缺失附件。

“停止监听”请求取钥程序安全结束，结束前保持程序打开。获取部分密钥仍可能不足以读取旧历史或部分附件。已读取并选中会话后，可勾选“同时尝试所选会话的图片密钥”再取钥；图片分支仍受客户端版本和媒体格式限制。

点击“读取联系人 / 群聊”，用搜索框和“全部”“联系人”“群聊”筛选。实际导出使用所选行的稳定 ID；重名时请核对标识，不会自动合并会话。

“导出保存到”默认是当前用户的 `Documents/WeChatExports`，选择的是父目录。每次“导出所选会话”都会新建 `wechat_上海时间戳` 子目录，避免覆盖先前备份。导出在后台运行，完成后点击“打开离线网页”或“打开导出目录”。

网页无需 HTTP 服务，可在浏览器直接查看可用图片、音视频和文件。缺失附件和未解析消息仍保留原因或占位。移动备份时复制整个导出子目录，包括 JSON、HTML 和附件，不要只复制网页。

## 合成演示和 CLI

```powershell
.\WeChatExportTool.exe --demo
.\wechat-export.exe doctor --json
.\wechat-export.exe accounts --json
```

`--demo` 用程序生成的账号、同名联系人、群聊、消息与媒体体验界面，不读取真实微信账号。正常使用不加该选项。CLI 的参数、JSON 合同和退出码见 [CLI 自动化说明](CLI_AUTOMATION.md)。

配置和受 Windows 保护的密钥缓存默认在 `%LOCALAPPDATA%\WeChatExportTool`，不放入软件目录或明文导出。移动软件不搬走本机缓存；删除软件目录不会自动删除用户配置或已有备份。导出原始行可能保留消息的媒体属性，但工具不会将数据库页密钥添加到导出。

## 从源码构建便携包

以下步骤供发布维护者使用，普通用户不需要执行。需要 Windows x64、Python 3.11+、Git、Go 1.26+；准备 SILK 包还需要 npm。建议从干净的源码检出构建。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
npm ci --ignore-scripts
.\scripts\build_capture.ps1
.\.venv\Scripts\python.exe scripts/prepare_windows_runtime.py --output .build/runtimes
.\scripts\build_windows.ps1 -PythonPath .\.venv\Scripts\python.exe -RuntimeRoot .\.build\runtimes -DistPath .\dist\windows -WorkPath .\.build\pyinstaller -CaptureExe .\bin\wxmori-media.exe
```

构建使用 PyInstaller 收集 GUI、CLI 和共享运行时，按确定白名单加入取钥程序、Node 和 SILK 资源，不复制用户账号、缓存或既有导出。源码、固定上游提交与补丁保留来源，运行时清单保留 SHA256 和许可声明。

发布前应运行合成测试、GUI 演示及 exe 的 `doctor --json`，并从新解压的发布目录验证 GUI 与 CLI。开发验证不使用真实聊天和密钥作为固定测试数据。修改依赖或捆绑资源后，应重新核对来源、许可证和完整目录结构。
