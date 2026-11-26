# N.E.K.O. Passport Bridge

让 **FoloToy AI Passport（ESP32-C3）** 成为 [N.E.K.O](https://github.com/Project-N-E-K-O/N.E.K.O)（猫娘计划）的桌面终端：文字和官方音色语音同步到设备，按住 OK 说话即可对话，切换遥控器模式后可当作蓝牙拍照快门。

> 非官方项目。与 Project N.E.K.O、FoloToy 均无隶属关系，仅通过公开插件 SDK 与设备协议对接。

## 功能

| 能力 | 说明 |
|---|---|
| 对讲机 | 按住 OK 说话，设备端 PCM 上行 → 电脑端 Whisper 转写 → 交给猫娘 → 回复文字与语音回送设备 |
| 遥控器 | 蓝牙 HID 拍照快门，配对 `NEKO-Remote`，退出模式自动关闭蓝牙 |
| 番茄钟 / 神签 | 独立小工具页面 |
| 局域网自动发现 | 设备向 UDP 8766 广播，插件应答，无需手动填电脑 IP |

## 工作原理

```
FoloToy AI Passport (ESP32-C3)
        │  WebSocket  ws://<PC-IP>:8765
        │  ├─ TEXT 帧：JSON 控制包
        │  └─ BIN  帧：PCM 音频帧
        ▼
neko_passport_bridge（本插件，独立进程）
        │  plugin.sdk  IPC
        ▼
N.E.K.O 宿主（对话 / TTS / 记忆）
```

设备发现：设备向 `UDP 8766` 广播，插件应答后由设备主动连 `8765`。发现失败时可在设备面板手填 `ws://<电脑IP>:8765`。

## 通信协议

协议版本 `v = 1`，实现见 [`protocol.py`](./protocol.py)。

**控制包**走 WebSocket TEXT 帧，JSON，单包上限 4096 字节，必备字段 `v` / `type` / `id`：

```
hello · wake · asr_text · battery · audio_start · audio_end
heartbeat · ack · reply_text · tts · expression · audio_out · error
```

**上行音频**走 BIN 帧：

```c
struct { char magic[4]="NKA1"; uint32 stream_id; uint32 seq; uint32 len; } + PCM   // PCM ≤ 2048 B
```

**下行音频**走 BIN 帧，16 kHz：

```c
struct { char magic[4]="NKP1"; uint32 stream_id; uint32 seq; } + PCM   // PCM ≤ 1024 B
```

`reply_text` 按 192 字节分片，带 `part` / `parts` / `final`；`tts` 与 `audio_out` 用 `state = start | done` 包住一段播放。未确认包进 128 条待确认缓存，设备 `ack` 后移除。

## 安装

本仓库只提供插件源码，不提供打包好的 `.neko-plugin` 安装包，也不提供预编译固件。

```bash
git clone https://github.com/wslxacgm/N.E.K.O-PASSPORT.git
cd N.E.K.O-PASSPORT
uv sync          # 依赖见 pyproject.toml / uv.lock
```

把该目录放到 N.E.K.O 的插件目录，或通过插件管理器的「本地目录」导入，然后：

1. 启用插件，并打开「开机自启」。
2. 首次启动会给 N.E.K.O 前端注入音频镜像钩子，日志出现 `NEKO Passport: 已注入音频镜像钩子` 后，**重启一次 N.E.K.O** 让页面加载新 JS。
3. 面板地址：`http://127.0.0.1:8765/`（启动时会自动打开）。

依赖按 `pyproject.toml` / `uv.lock` 在首次运行时从网络拉取，不再随包分发。

## 固件

设备端固件不在本仓库内，本仓库也不发布预编译固件。设备需要运行与下文协议兼容的固件（`NKA1` / `NKP1` 分帧、连接 `ws://<PC-IP>:8765`），可以自行构建，也可以使用你已有的版本。

刷写参考（ESP32-C3、8 MB flash，仅刷应用分区时保留配网信息）：

```bash
esptool.py --chip esp32c3 -p COMx -b 460800 write_flash 0x10000 <your-app.bin>
```

Windows 下可用 `python -m esptool` 代替 `esptool.py`。

## 配网与防火墙

**配网**

1. 首次开机进入热点 `NEKO-Passport-xxxx`，手机连上。
2. 浏览器打开 `http://192.168.4.1`，选 2.4G Wi-Fi 并填密码，保存后自动重启。
3. 重新配网：长按上键 10 秒清空配置。

**防火墙**：首次运行需允许 Python 通过「专用网络」：

- `TCP 8765` — WebSocket 与面板
- `UDP 8766` — 设备自动发现

## 配置

参考 [`config.example.toml`](./config.example.toml)，实际配置位于 `N.E.K.O\plugins\neko_passport_bridge\config\plugin.toml`。

| 字段 | 默认 | 说明 |
|---|---|---|
| `host` / `port` | `0.0.0.0` / `8765` | WebSocket 与面板监听地址 |
| `discovery` | `true` | 是否响应 UDP 8766 广播 |
| `token` | `neko-passport-dev` | 连接令牌，**建议改成随机串** |
| `cache_capacity` | `128` | 待确认包缓存条数 |
| `whisper_model` | `base` | Whisper 模型规格 |
| `whisper_language` | 空 | 留空自动识别 |
| `local_files_only` | `false` | 只使用本地模型文件 |
| `max_audio_seconds` | `30` | 单次录音上限 |
| `panel_auto_open` | `true` | 启动时自动打开面板 |

## 已知限制

- 插件会修改 N.E.K.O 程序文件 `static/app/app-audio-playback.js`（约 30 行），原文件备份为 `*.neko-passport.bak`。N.E.K.O 升级覆盖后，下次启动插件会自动重打；卸载插件约 1 分钟内由独立清理进程自动移除钩子。这是本项目唯一改动宿主文件的地方。
- 首次按住说话会下载 Whisper `base` 模型（约 140 MB），需等待日志出现模型就绪。
- 设备有声但卡顿：确认电脑与设备在同一局域网且距离较近，插件已按实时速率发送。
- 设备完全没声：先看面板「前端钩子」是否为「已注入」；未注入则重启 N.E.K.O 或检查前端文件是否被升级覆盖。

## 目录结构

```
neko-passport/
├─ plugin.toml            插件清单（id / entry / SDK 版本区间）
├─ __init__.py
├─ neko_plugin.py         插件入口与生命周期
├─ bridge_server.py       WebSocket 服务、面板 HTTP
├─ discovery.py           UDP 8766 发现应答
├─ protocol.py            包格式、分片、待确认缓存
├─ reply_adapter.py       猫娘回复 → 设备包
├─ whisper_asr.py         本地语音识别
├─ frontend_tap.py        前端音频镜像钩子注入与清理
├─ config.example.toml    配置模板
├─ tests/                 单元测试
└─ uv.lock
```

## 许可

[Apache License 2.0](./LICENSE)。插件代码基于 N.E.K.O 插件 SDK（同样为 Apache-2.0）开发。

本仓库仅包含插件源码，不附带任何角色素材、Live2D 模型、音色文件或固件二进制；这些资产各自由其原作者许可，与本仓库的 Apache-2.0 无关。
