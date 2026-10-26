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

支持的链接：`BV` 号、`av` 号、带 `?p=` 的分 P 链接，以及路径本身是
`BV`/`av` 的 `b23.tv` 链接（如 `https://b23.tv/BV1xx411c7mD`）。

> `b23.tv` 的任意短码（如 `https://b23.tv/EkD01Wz`）需要访问短链服务，
> 部分服务器出口 IP 会被 B 站返回 412 风控，无法可靠解析。遇到这种链接，
> 请在 B 站 App/网页里复制完整视频链接后再发送。

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

### 方式一：从 AstrBot 插件市场安装（推荐）

1. 打开 AstrBot WebUI，进入 `插件` / `扩展管理` 页面；
2. 切换到 `插件市场`（AstrBot Plugin Market）；
3. 搜索 `astrbot_plugin_bili_media` 或 `B站视频/音频提取`；
4. 点击 `安装`，安装完成后按提示重载插件；
5. 插件依赖会依据 `requirements.txt` 自动安装。

### 方式二：上传压缩包（无需 Git）

1. 从仓库的 Release 下载 `astrbot_plugin_bili_media-v1.0.0.zip`；
2. 打开 AstrBot WebUI → `插件` → `安装插件` → 「上传压缩包」，选中该 zip；
3. 安装完成后插件会自动加载，接着到插件配置里修改配置即可。

### 方式三：克隆仓库

```bash
cd AstrBot/data/plugins
git clone https://github.com/Hitori2136/astrbot_plugin_bili_media
```

然后在 WebUI 的 `插件` 页面点「重载插件」，或重启 AstrBot。
插件会依据 `requirements.txt` 自动安装依赖。

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
| `max_download_mb` | `500` | 单个下载文件的硬上限，防止异常视频占满磁盘 |
| `max_parse_mb` | `16` | yt-dlp 解析结果和扫码凭据文件的最大读取大小 |
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

## License

MIT，详见 [LICENSE](LICENSE)。
