# Third-party notices / 第三方声明

WeChat Export Tool is distributed under **GPL-3.0-only**. See [LICENSE](LICENSE). The project builds on the following open-source work; upstream copyright and license notices remain applicable.

本项目的适配、导出与展示代码以 GPL-3.0-only 发布。下述第三方组件保留各自许可证和版权声明；主项目许可证不替代它们。

## Direct upstream projects

### chatlog-keeper

- Source: <https://github.com/labazhou2024/chatlog-keeper>
- Pinned commit: `2225294fd0761e10ce15540f21e7dbb2ac304c3c`
- Copyright: Copyright (c) 2026 labazhou2024
- License: MIT; verbatim copy in [licenses/chatlog-keeper-MIT.txt](licenses/chatlog-keeper-MIT.txt).
- Use: database authentication/decryption helpers, snapshot support and WeChat message parsing. Installed as a Python source dependency; this repository supplies the selected-conversation export and media workflow.

### WxMori

- Source: <https://github.com/axiomzero-qed/WxMori>
- Pinned commit: `055f6992c0a78164b720d47ef48830825fc0b92a`
- Copyright: Copyright (c) 2026 axiomzero-qed
- License: MIT; verbatim copy in [licenses/WxMori-MIT.txt](licenses/WxMori-MIT.txt).
- Use: the foundation of the separately built Windows capture executable. This repository applies a patch for media-database coverage, capture continuation, image probes, process selection and completion conditions. Core PE discovery, debugging/detachment and protected keyring mechanisms are upstream work.

### silk-wasm

- Source: <https://github.com/idranme/silk-wasm>
- npm version: `3.7.1`
- Copyright: Copyright (c) 2024 idranme
- License: MIT; verbatim copy in [licenses/silk-wasm-MIT.txt](licenses/silk-wasm-MIT.txt).
- Use: optional SILK-to-PCM decoding invoked by a local Node.js batch adapter. The wasm package is installed using npm rather than copied into this source repository.

## Format research references

- [kclx/WeChatDecrypted](https://github.com/kclx/WeChatDecrypted), GPL-3.0: emoji metadata, package offsets and media-format compatibility research. This project is not a runtime dependency; the complete GPL text is retained as this repository's [LICENSE](LICENSE).
- [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica), Apache-2.0: local image naming and WXGF/HEVC compatibility research. This project is not a runtime dependency. Its license text is retained in [licenses/wechatauto-replica-Apache-2.0.txt](licenses/wechatauto-replica-Apache-2.0.txt).

The format adapters do not claim that the underlying formats or cryptographic algorithms were independently invented here. No WeFlow source, DLL or binary is included.

## Other installed or external components

Python, PyCryptodome, NumPy, python-zstandard, Pillow, Node.js, FFmpeg/ffprobe, and WxMori's transitive Go modules retain their own licenses. Their role and licensing scope are described in [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md). Installed distributions can include additional bundled-library notices; consult those distributions when redistributing them.

This repository does not bundle Python wheels, Node.js, FFmpeg, WeChat or precompiled capture binaries. Download/build steps and local execution are separate from publication of this source repository.
