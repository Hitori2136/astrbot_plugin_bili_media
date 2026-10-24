# astrbot_plugin_bili_media

B 站视频 / 音频提取插件：发送 `/提取视频` 拿视频，发送 `/提取音频` 拿可点击的 QQ 语音条；支持扫码登录 B 站账号。

## 功能

| 指令 | 权限 | 行为 |
| --- | --- | --- |
| `/提取视频 <B站链接>` | 所有人 | 下载视频并发送（默认 720P、30MB 上限） |
| `/提取音频 <B站链接>` | 所有人 | 提取音频，按 60 秒切片，连续发送多条 QQ 语音 |
| `/账号登陆` | 管理员 | 发送 B 站登录二维码，扫码后自动保存 Cookie |
| `/账号状态` | 管理员 | 查看当前使用的是扫码账号还是配置 Cookie |
| `/退出登陆` | 管理员 | 清除扫码登录凭据 |

`/账号登陆` 也可以写作 `/账号登录`，`/退出登陆` 也可以写作 `/退出登录`。

支持的链接：`BV` 号、`av` 号、`b23.tv` 短链、带 `?p=` 的分 P 链接。
不支持番剧、直播、空间、专栏等链接。

## 扫码登录（可选）

插件首次安装后不需要手动找 Cookie：

1. AstrBot 管理员在私聊或群里发送 `/账号登陆`；
2. 机器人发送二维码图片；
3. 用哔哩哔哩 App 扫码，并在手机上确认；
4. 机器人提示登录成功，凭据写入
   `data/plugin_data/astrbot_plugin_bili_media/credentials.json`；
5. 后续 `/提取视频`、`/提取音频` 自动使用该账号。

扫码凭据优先于 WebUI 里配置的 `cookie`，也优先于 `cookies_file`。发送
`/退出登陆` 后会回退到配置里的 Cookie。该 JSON 文件包含账号凭据，请不要外传。

> `/账号登陆` 默认只允许 AstrBot 管理员触发，避免群友把自己的账号绑到机器人上。
> 如果私聊里也无法使用，请先在 AstrBot 配置里把自己的 QQ 设为管理员。

## 安装

- 推荐：在 AstrBot WebUI → 插件 → 安装插件，上传 `dist/astrbot_plugin_bili_media-v1.1.0.zip`。
- 或者把本目录放进 `data/plugins/`，重载插件。

插件依赖：

- `yt-dlp`：解析和下载 B 站视频；
- `imageio-ffmpeg`：系统没有 ffmpeg 时提供兜底二进制；
- `qrcode[pil]`：生成扫码登录二维码；
- 系统 `ffmpeg`：有则优先使用，视频合并和音频转码都需要它。

B 站未登录通常只能取到 480P/360P。扫码登录或手动填写 `cookie` 后可以取更高清晰度。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `max_video_height` | `720` | 视频最大清晰度 |
| `max_video_mb` | `30` | 视频大小上限，超过会自动降清晰度 |
| `audio_chunk_seconds` | `60` | 语音切片长度，QQ 语音上限 60 秒 |
| `max_audio_chunks` | `30` | 单次最多发送多少段语音 |
| `audio_send_interval` | `0.6` | 多段语音发送间隔（秒） |
| `send_progress` | `true` | 是否先发一条“正在处理”的提示 |
| `cooldown_seconds` | `20` | 单用户冷却，0 表示关闭 |
| `max_concurrent` | `1` | 同时处理的任务数 |
| `timeout_seconds` | `300` | 单次解析/下载超时 |
| `max_audio_mb` | `200` | 原始音频文件大小上限 |
| `cookie` | 空 | B 站 Cookie，扫码登录优先；也可手动填 SESSDATA |
| `cookies_file` | 空 | Netscape 格式 cookies.txt 路径 |
| `proxy` | 空 | 下载代理，如 `http://127.0.0.1:7890` |
| `ffmpeg_path` | 空 | 自定义 ffmpeg 路径，留空自动查找 |

## QQ 语音说明

插件发送的是 AstrBot 的 `Record` 消息段：

- OneBot v11 / NapCat：由 NapCat 转成 silk 并显示为语音条；
- QQ 官方机器人：由 AstrBot 转成 `tencent_silk` 并走富媒体上传。

普通 QQ 语音最长 60 秒，所以插件会把音频切片后逐条发送；超过
`max_audio_chunks` 的视频只发送前 N 段，并给出提示。

> QQ 官方机器人的**视频**消息依赖 AstrBot 4.29+ 的 `qqofficial` 适配器；
> 4.19 及更早的官方适配器只处理图片和语音。若使用旧版，请优先用 OneBot/NapCat，
> 或升级 AstrBot。音频消息在旧版上仍可发送。

## 开发与测试

```bash
python -m unittest discover -s tests -v
python scripts/build_zip.py
```

测试使用 `tests/astrbot/` 里的假 AstrBot 包，不需要启动 AstrBot，也不会消耗网络额度。

## 许可证

MIT
