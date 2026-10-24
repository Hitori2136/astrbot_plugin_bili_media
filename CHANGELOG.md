# Changelog

## v1.1.0 - 2026-10-24

- 新增管理员指令 `/账号登陆`：发送 B 站扫码登录二维码，扫码确认后自动保存 Cookie。
- 新增 `/账号状态` 和 `/退出登陆`。
- 扫码凭据保存在 `data/plugin_data/astrbot_plugin_bili_media/credentials.json`，优先于配置里的 Cookie。
- 新增 `qrcode[pil]` 依赖。

## v1.0.0 - 2026-10-24

- 初始版本。
- 支持 `/提取视频` 与 `/提取音频`。
- 支持 BV/av/b23.tv 与分 P 链接。
- 音频按 60 秒切片，以 QQ 语音条发送。
- 兼容 OneBot v11（NapCat）与 QQ 官方机器人。
