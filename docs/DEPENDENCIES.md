# 依赖、来源与实现边界

本项目以 GPL-3.0-only 发布。下表区分运行依赖、构建来源与格式研究参考；它们保留各自许可证，不能用主项目许可证替换其原始声明。

## 固定源码来源

| 项目 | 固定版本或提交 | 使用范围 | 许可证 |
|---|---|---|---|
| [chatlog-keeper](https://github.com/labazhou2024/chatlog-keeper) | `2225294fd0761e10ce15540f21e7dbb2ac304c3c` | 数据库页面校验、私有快照解密与消息解析；通过 Python 依赖安装 | MIT |
| [WxMori](https://github.com/axiomzero-qed/WxMori) | `055f6992c0a78164b720d47ef48830825fc0b92a` | Windows 取钥程序基础；构建时应用本仓库补丁 | MIT |
| [silk-wasm](https://github.com/idranme/silk-wasm) | `3.7.1` | Node 批处理将本地 SILK 音频解码为 PCM，随后写入 WAV | MIT |

`chatlog-keeper` 固定在源码提交，而非假设存在同版本 PyPI 发布。不要把固定提交升级为浮动 `main` 后仍沿用原兼容性声明。

WxMori 的 PE 定位、硬件断点与安全解除、AES-256 数据库取钥、DPAPI 密钥保护等基础来自上游。本仓库的补丁集中于媒体数据库覆盖、已有密钥续采、可选图片探针、目标进程筛选和完成状态判断。构建需要 Git 与 Go 1.26+；安装导出工具本身不需要 Go，但没有兼容取钥程序及有效缓存时无法读取加密数据库。

本仓库不分发这些项目的完整工作副本、SILK wasm 二进制、微信程序或 FFmpeg 程序。Python/npm/Go 安装器会按依赖清单取得各自组件。第三方许可副本见 [../licenses/](../licenses/)。

## 通用运行组件

具体可安装版本由 `pyproject.toml`、`package.json` 和锁文件决定；下表描述职责，不表示所有历史版本的许可证条款都完全相同。

| 组件 | 用途 | 许可说明 |
|---|---|---|
| Python 3.11+ | CLI、数据库与导出流程 | PSF；由用户安装 |
| [PyCryptodome](https://github.com/Legrandin/pycryptodome) | 标准 AES 等密码学原语 | BSD-2-Clause 与 Unlicense，具体见所安装发行版 |
| [NumPy](https://github.com/numpy/numpy) | 有界内存候选的向量筛选 | 主体 BSD-3-Clause；发行包还保留内含组件的其他声明 |
| [python-zstandard](https://github.com/indygreg/python-zstandard) | 消息 Zstandard 解压 | BSD-3-Clause；附带 Zstandard 的声明仍适用 |
| [Pillow](https://github.com/python-pillow/Pillow) | 图片格式识别、像素解码和校验 | 当前发行版使用 MIT-CMU；具体见安装包 LICENSE |
| Node.js 18+ | 可选 SILK 解码批处理宿主 | Node.js 及其内含组件各自条款；由用户安装 |
| [FFmpeg / ffprobe](https://ffmpeg.org/) | 可选视频转码、媒体探测、特殊图像预览 | LGPL/GPL 等条款取决于具体构建选项；本项目不附带其二进制 |

采用用户自己的 FFmpeg 构建时，应以该构建附带的许可证为准。不同构建包含的编解码器也不同，程序会记录转码或探测失败，不会把扩展名改成 MP4 就视为播放成功。

WxMori 还有 Go 模块依赖，精确清单位于上述固定提交的 [`go.mod`](https://github.com/axiomzero-qed/WxMori/blob/055f6992c0a78164b720d47ef48830825fc0b92a/go.mod) 和 `go.sum`。本项目的源码构建不会改变它们的许可证；如果另行分发编译后的程序，应同时处理该二进制内全部依赖的许可要求。

## 媒体格式研究参考

| 项目 | 本项目参考内容 | 分发关系 |
|---|---|---|
| [kclx/WeChatDecrypted](https://github.com/kclx/WeChatDecrypted) | 表情元数据、表情包偏移及媒体格式兼容性研究 | GPL-3.0；不是运行依赖，安装脚本不下载其工作副本 |
| [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) | 本地图片命名、WXGF/HEVC 格式兼容性研究 | Apache-2.0；不是运行依赖，安装脚本不下载其工作副本 |

这些参考有助于理解媒体格式，不构成对所有客户端版本的支持承诺。本项目未使用 WeFlow 代码、DLL 或二进制。

## 我们新增的部分

- 账号配置、联系人/群聊选择与非交互 CLI。
- 只针对所选会话的快照、原始行归档、消息与媒体关联及导出编排。
- 对 XML 终止空字节等解析差异的恢复、稳定排序与发送者身份核对。
- 附件 `available` / `missing` 状态、格式验证、相对路径及完整性检查。
- 含附件的离线网页、分页与筛选、图片放大、音视频和下载控件。
- 对捕获器的媒体与图片适配补丁及相应合成测试。

标准加密算法、数据库基础解密、SILK 编解码和视频编码器均依赖现有开源实现；新增整合代码不改变其来源归属。
