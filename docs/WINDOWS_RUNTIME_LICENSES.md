# Windows 便携包的运行时与许可

便携包把 GUI、CLI 和 Python/Tk 运行时一起打包，并另附独立的取钥程序、Node.js 与 SILK 解码资源。最终用户无需安装 Python、Go、Node.js 或 npm。开发者重新构建仍需这些工具；开发依赖不等于最终用户运行依赖。

每个便携包的 `runtime/runtime-manifest.json` 是该包实际包含组件的清单。它列出固定版本、上游下载地址、选中文件及逐文件 SHA-256。构建脚本只复制 `selected_files` 中的文件，下载缓存、微信账号数据、密钥缓存和开发临时文件不属于分发清单。

## Node.js 与 SILK

| 组件 | 固定版本 | 包内位置 | 保留的许可 |
|---|---|---|---|
| Node.js Windows x64 | `24.22.2` | `runtime/node/node.exe` | `runtime/node/LICENSE`：Node.js MIT 及其内含组件的完整声明 |
| silk-wasm | `3.7.1` | `runtime/silk/lib/index.cjs`、`silk.wasm` | `runtime/silk/LICENSE`：包装层 MIT |
| silk-wasm 内含的 codec | 同上 | 内含于 WASM | `runtime/silk/SILK-CODEC-LICENSE.txt`：原 codec 分支 BSD 声明 |
| Skype SILK SDK | 同上 | 内含于 WASM | `runtime/silk/SKYPE-SILK-SDK-LICENSE.txt`：原 SDK BSD 风格声明 |

Node.js 来自[官方发行目录](https://nodejs.org/dist/v24.22.2/)，使用 `node-v24.22.2-win-x64.zip`。固定 SHA-256 为：

```text
ca1bff5d8ead13c9f2ea404a176db46bd430704d5b5113734049fa365e7799e2
```

该值在[官方 SHASUMS256.txt](https://nodejs.org/dist/v24.22.2/SHASUMS256.txt)中核对。便携包只取 `node.exe` 与完整 `LICENSE`，不附 npm、安装器或 PATH 修改脚本。

silk-wasm 来自[固定 npm 包](https://registry.npmjs.org/silk-wasm/-/silk-wasm-3.7.1.tgz)，同时校验 `package-lock.json` 中的 SHA-512 integrity。该 tarball 的 SHA-256 为：

```text
668087436d9a7160544eaefd256ce774697fb39c4ff1985897c5bb9c45580df0
```

包装层、codec 与 SDK 的许可分别保留，不能只用包装层 MIT 代替内含组件的声明。额外许可固定到该版本对应的[源码提交 `2793875764ad98245c177ac496eb5b7100a71038`](https://github.com/idranme/silk-wasm/tree/2793875764ad98245c177ac496eb5b7100a71038)。许可副本也位于本仓库的 `licenses/`。

## 取钥程序与 Go 模块

`runtime/capture/wxmori-media.exe` 基于固定的 WxMori 提交及本仓库补丁构建。WxMori 本身为 MIT；Go 运行时、实际链接模块及其内含组件的原始许可保存在 `runtime/capture-licenses/`。

准备脚本通过 `go list -deps -json ./cmd/wxmori` 枚举 Windows x64 命令实际链接的模块，再复制各模块中的许可与 NOTICE。它不把完整开发模块图中的仅测试依赖当成运行依赖。最终模块版本和许可文件路径写入 manifest；`modernc.org/sqlite`、`modernc.org/libc` 等组件保留自己的多段声明，SQLite 的公共领域声明也予以保留。

Go 模块的 MIT、BSD 和 Apache 等许可不因本项目采用 GPL-3.0-only 而消失。构建工具还保留 Go 标准库及其 vendored 组件的许可。精确源码来源仍见 [DEPENDENCIES.md](DEPENDENCIES.md) 和固定上游的 `go.mod`、`go.sum`。

## Python 标准库内含的 OpenSSL

冻结的 Python 标准库还带有独立的 libssl/libcrypto DLL。本次构建的 Python 使用 OpenSSL `3.0.16`。准备脚本严格解析实际构建 Python 的 `ssl.OPENSSL_VERSION` 文本以选择精确上游 tag，并保存其 Apache-2.0 `LICENSE.txt` 到 `runtime/OpenSSL/`，同时写明来源归属。对应上游 tag 没有独立的 `NOTICE` 文件；包内 `NOTICE.txt` 明确标注为本项目整理的归属说明。

这份声明针对 CPython 随附的 OpenSSL。Node.js 内含的 OpenSSL 版本可能不同，由 Node.js 完整 `LICENSE` 中的声明另行覆盖。

## FFmpeg 是独立的可选组件

是否附带 FFmpeg 以便携包 manifest 的 `ffmpeg_bundled` 为准；该字段为 `false` 时，包内没有 FFmpeg 或 ffprobe。此时仍能导出本机可定位的原视频文件，是否在浏览器中播放取决于原视频编码。需要转码或特殊图像解码的内容可能缺少浏览器兼容预览，不能据文件扩展名断言可播放。

用户可在配置中显式指定自己安装的 FFmpeg/ffprobe。不同构建的许可和编解码器不同，应保留其原始许可。发行包不从开发者电脑的 PATH 抓取任意 FFmpeg 二进制，也不复制未知来源的已安装版本。

若发行包选择附带本仓库从官方源码构建的最小 FFmpeg，必须同时完成匹配源码、构建命令、LGPL 及内含声明的分发，并在该次 Release 中提供对应源码下载。未完成这些步骤的第三方二进制不进入默认包。具体构建与编码器能力以附随 FFmpeg manifest 和构建说明为准。

## 开发者准备命令

```powershell
.\scripts\build_capture.ps1
python scripts/prepare_windows_runtime.py
```

准备脚本把资源写入 `.build/runtimes`，校验固定下载摘要，并生成 runtime manifest。`scripts/build_windows.ps1` 随后按清单校验、复制资源。GUI/CLI 所含 Python、Tcl/Tk 和 Python 扩展包的许可由打包步骤另行收集；这份文档不替代实际便携包内的完整第三方许可目录。
