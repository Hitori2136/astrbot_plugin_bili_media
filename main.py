"""B 站视频 / 音频提取插件。

指令：
    /提取视频 <B 站视频链接>   发送视频
    /提取音频 <B 站视频链接>   提取音频，按 60 秒切片发送 QQ 语音条

设计约束（按 AstrBot 4.29.1 / 4.19.5 的公共接口编写）：
- 只处理 B 站普通视频：BV/av 号、b23.tv 短链、分 P 链接；
- 视频走 yt-dlp，默认 720P / 30MB 上限；
- 音频走 yt-dlp + ffmpeg 转 16kHz 单声道 WAV，再用标准库 wave 切片；
- 语音使用 AstrBot 的 Record 组件：OneBot/NapCat 与 QQ 官方机器人各自会转 silk；
- 不依赖 requests，不修改 AstrBot 核心依赖版本。
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record, Video
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_bili_media"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BILI_REFERER = "https://www.bilibili.com/"
DEFAULT_TIMEOUT = 300
MAX_ERROR_CHARS = 160

_URL_RE = re.compile(r"https?://[^\s<>'\"）)】\]]+", re.IGNORECASE)
_BV_RE = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])", re.IGNORECASE)
_AV_RE = re.compile(r"(?<![0-9A-Za-z])av\d+(?![0-9A-Za-z])", re.IGNORECASE)
_B23_RE = re.compile(
    r"(?<![0-9A-Za-z.])(?:www\.)?b23\.tv/[A-Za-z0-9]+", re.IGNORECASE
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_COOKIE_HEADER_RE = re.compile(r"(?i)(cookie\s*:\s*)[^\r\n]+")
_COOKIE_PAIR_RE = re.compile(
    r"(?i)\b(SESSDATA|bili_jct|DedeUserID|sid|access_key|refresh_token)"
    r"\s*=\s*[^;\s]+"
)
_URL_CRED_RE = re.compile(r"(?i)(https?://)[^/\s@]+@")
_TRAILING_CHARS = " \t\r\n<>'\"`，。；！？、,.;!?）)】]》」』&"


# --------------------------------------------------------------------------- #
# 异常与数据结构
# --------------------------------------------------------------------------- #
class UserVisibleError(Exception):
    """消息会直接回给用户；不要把堆栈或原始响应带出去。"""


class UsageError(UserVisibleError):
    pass


class UnsupportedLinkError(UserVisibleError):
    pass


class MissingDependencyError(UserVisibleError):
    pass


class FfmpegMissingError(UserVisibleError):
    pass


class DownloadFailedError(UserVisibleError):
    pass


class MediaTooLargeError(UserVisibleError):
    pass


class AudioConversionError(UserVisibleError):
    pass


@dataclass(frozen=True)
class BiliTarget:
    video_id: str
    page: int
    url: str
    original: str = ""

    @property
    def display_id(self) -> str:
        if self.page > 1:
            return f"{self.video_id} P{self.page}"
        return self.video_id


@dataclass
class MediaMeta:
    title: str
    duration: float
    uploader: str = ""
    webpage_url: str = ""
    info: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class FormatChoice:
    format_id: str
    height: int | None
    estimated_bytes: int | None
    label: str = ""


@dataclass
class DownloadedVideo:
    path: Path
    height: int | None
    size_bytes: int


@dataclass
class AudioChunks:
    paths: list[Path]
    duration: float
    total_chunks: int
    truncated: bool


# --------------------------------------------------------------------------- #
# 文本、URL 与脱敏
# --------------------------------------------------------------------------- #
def redact(text: object) -> str:
    """把 Cookie、URL 认证信息替换掉，避免日志泄露。"""
    out = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}***", str(text))
    out = _COOKIE_PAIR_RE.sub(lambda m: f"{m.group(1)}=***", out)
    out = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}***@", out)
    return out


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _short_error(text: object) -> str:
    cleaned = _strip_ansi(redact(text)).strip()
    cleaned = re.sub(r"(?i)^error:\s*", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:MAX_ERROR_CHARS]


def _clean_title(title: object) -> str:
    text = _CONTROL_RE.sub("", str(title or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:60] or "B站视频"


def _fmt_duration(seconds: float) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _clean_token(token: str) -> str:
    token = token.strip()
    previous = None
    while token and token != previous:
        previous = token
        token = token.rstrip(_TRAILING_CHARS)
    return token


def _extract_url_token(text: str) -> str:
    """从用户文本里提取一个 URL、BV 号或 av 号。"""
    raw = str(text or "").strip()
    match = _URL_RE.search(raw)
    if match:
        return _clean_token(match.group(0))

    match = _BV_RE.search(raw)
    if match:
        return match.group(0)

    match = _AV_RE.search(raw)
    if match:
        return match.group(0)

    match = _B23_RE.search(raw)
    if match:
        return _clean_token(match.group(0))
    return ""


def _video_id_from_segment(segment: str) -> str | None:
    match = re.fullmatch(r"BV([0-9A-Za-z]{10})", segment, re.IGNORECASE)
    if match:
        return "BV" + match.group(1)
    match = re.fullmatch(r"av(\d+)", segment, re.IGNORECASE)
    if match:
        return "av" + match.group(1)
    return None


def _query_page(query: str) -> int:
    values = urllib.parse.parse_qs(query, keep_blank_values=False).get("p") or []
    if not values:
        return 1
    try:
        page = int(values[0])
    except (TypeError, ValueError):
        return 1
    return max(1, min(page, 9999))


def _is_bili_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return host == "bilibili.com" or host.endswith(".bilibili.com")


def _target_from_id(video_id: str, page: int, original: str = "") -> BiliTarget:
    url = f"https://www.bilibili.com/video/{video_id}"
    if page > 1:
        url += f"?p={page}"
    return BiliTarget(video_id=video_id, page=page, url=url, original=original)


def _resolve_b23_blocking(url: str, timeout: float) -> str:
    request = urllib.request.Request(  # noqa: S310 - 只允许 b23.tv
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": BILI_REFERER,
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return str(response.geturl())


async def _resolve_b23(url: str, timeout: float = 10.0) -> str:
    try:
        final = await asyncio.wait_for(
            asyncio.to_thread(_resolve_b23_blocking, url, timeout), timeout=timeout + 5
        )
    except asyncio.TimeoutError as exc:
        raise UserVisibleError("短链解析超时，请稍后重试或换用完整链接。") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("b23 resolve failed: %s", redact(exc))
        raise UserVisibleError("短链解析失败，请检查链接或换用完整链接。") from exc

    host = (urllib.parse.urlsplit(final).hostname or "").lower()
    if final == url or not _is_bili_host(host):
        raise UserVisibleError("短链没有跳转到 B 站视频，请换用完整链接。")
    return final


async def parse_bili_target(text: str, timeout: float = 10.0) -> BiliTarget:
    """把用户输入解析成规范的 B 站视频地址。"""
    token = _extract_url_token(text)
    if not token:
        raise UsageError(
            "用法：/提取视频 <B站视频链接>\n"
            "示例：/提取视频 https://www.bilibili.com/video/BV1xx411c7mD"
        )

    video_id = _video_id_from_segment(token)
    if video_id:
        return _target_from_id(video_id, 1, token)

    if not re.match(r"^[a-zA-Z][\w+.-]*://", token):
        token = "https://" + token.lstrip("/")

    parts = urllib.parse.urlsplit(token)
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsupportedLinkError("只支持 http/https 的 B 站视频链接。")

    host = (parts.hostname or "").lower().rstrip(".")
    if host == "b23.tv" or host.endswith(".b23.tv"):
        final = await _resolve_b23(token, timeout)
        return await parse_bili_target(final, timeout)

    if not _is_bili_host(host):
        raise UnsupportedLinkError(
            "只支持 B 站普通视频链接（BV/av 号、b23.tv 短链）；其他站点暂不支持。"
        )

    segments = [urllib.parse.unquote(item) for item in parts.path.split("/") if item]
    if len(segments) >= 2 and segments[0].lower() == "video":
        video_id = _video_id_from_segment(segments[1])
        if not video_id:
            raise UnsupportedLinkError("没有识别到有效的 BV 号或 av 号。")
        page = _query_page(parts.query)
        return _target_from_id(video_id, page, text[:300])

    if segments and segments[0].lower() in {
        "bangumi",
        "live",
        "space",
        "read",
        "audio",
        "cheese",
        "festival",
    }:
        raise UnsupportedLinkError(
            "暂不支持番剧、直播、空间、专栏等链接，请发送 B 站普通视频链接。"
        )

    raise UnsupportedLinkError(
        "没有识别到 B 站视频 ID，请发送形如 https://www.bilibili.com/video/BV... 的链接。"
    )


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "y"}:
            return True
        if lowered in {"0", "false", "no", "off", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))  # 配置页偶尔会给出 "720.0"
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _as_float(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return max(low, min(number, high))


@dataclass
class PluginSettings:
    max_video_height: int = 720
    max_video_mb: int = 30
    max_audio_mb: int = 200
    audio_chunk_seconds: int = 60
    max_audio_chunks: int = 30
    audio_send_interval: float = 0.6
    send_progress: bool = True
    cooldown_seconds: int = 20
    max_concurrent: int = 1
    timeout_seconds: int = DEFAULT_TIMEOUT
    cookie: str = ""
    cookies_file: str = ""
    proxy: str = ""
    ffmpeg_path: str = ""
    cleanup_delay_seconds: float = 600.0

    @property
    def max_video_bytes(self) -> int:
        return self.max_video_mb * 1024 * 1024

    @property
    def max_audio_bytes(self) -> int:
        return self.max_audio_mb * 1024 * 1024

    @classmethod
    def from_config(cls, raw: dict | None) -> PluginSettings:
        raw = raw or {}

        def get(key: str, default: object = None) -> object:
            value = raw.get(key, default)
            return default if value is None else value

        cookie = str(get("cookie", "") or "").strip()
        if cookie and "=" not in cookie:
            cookie = f"SESSDATA={cookie}"

        cookies_file = str(get("cookies_file", "") or "").strip()
        if cookies_file:
            candidate = Path(os.path.expanduser(cookies_file))
            if candidate.is_file():
                cookies_file = str(candidate)
            else:
                logger.warning("B 站 cookies_file 不存在，已忽略：%s", cookies_file)
                cookies_file = ""

        return cls(
            max_video_height=_as_int(get("max_video_height", 720), 720, 144, 2160),
            max_video_mb=_as_int(get("max_video_mb", 30), 30, 1, 500),
            max_audio_mb=_as_int(get("max_audio_mb", 200), 200, 10, 1000),
            audio_chunk_seconds=_as_int(
                get("audio_chunk_seconds", 60), 60, 1, 60
            ),
            max_audio_chunks=_as_int(get("max_audio_chunks", 30), 30, 1, 120),
            audio_send_interval=_as_float(
                get("audio_send_interval", 0.6), 0.6, 0.0, 5.0
            ),
            send_progress=_as_bool(get("send_progress", True), True),
            cooldown_seconds=_as_int(get("cooldown_seconds", 20), 20, 0, 600),
            max_concurrent=_as_int(get("max_concurrent", 1), 1, 1, 3),
            timeout_seconds=_as_int(
                get("timeout_seconds", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT, 30, 900
            ),
            cookie=cookie,
            cookies_file=cookies_file,
            proxy=str(get("proxy", "") or "").strip(),
            ffmpeg_path=str(get("ffmpeg_path", "") or "").strip(),
        )


# --------------------------------------------------------------------------- #
# ffmpeg 与 WAV
# --------------------------------------------------------------------------- #
def _imageio_ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # 下载/解压失败等
        logger.debug("imageio-ffmpeg unavailable: %s", redact(exc))
        return None
    return exe if exe and Path(exe).is_file() else None


def resolve_ffmpeg(configured_path: str = "") -> str:
    """按配置 -> PATH -> imageio-ffmpeg 的顺序找到 ffmpeg。"""
    configured_path = str(configured_path or "").strip()
    if configured_path:
        candidate = Path(os.path.expanduser(configured_path))
        if candidate.is_dir():
            candidate = candidate / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if candidate.is_file():
            return str(candidate)
        raise FfmpegMissingError(
            f"配置的 ffmpeg 路径不存在：{configured_path}"
        )

    found = shutil.which("ffmpeg")
    if found:
        return found

    bundled = _imageio_ffmpeg_exe()
    if bundled:
        return bundled

    raise FfmpegMissingError(
        "未找到 ffmpeg，无法处理音视频。请安装 ffmpeg，或在插件配置里填写 "
        "ffmpeg_path；也可以重新安装插件依赖（imageio-ffmpeg 会自带二进制）。"
    )


async def convert_to_wav(
    source: Path,
    target: Path,
    ffmpeg: str,
    timeout: float = 180.0,
) -> Path:
    """把任意音频转成 16kHz 单声道 16-bit PCM WAV，方便切片与 silk 编码。"""
    target.parent.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() == ".wav":
        try:
            with wave.open(str(source), "rb") as wav:
                if (
                    wav.getnchannels() == 1
                    and wav.getsampwidth() == 2
                    and wav.getframerate() == 16000
                ):
                    shutil.copyfile(source, target)
                    return target
        except (wave.Error, OSError):
            pass

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = creationflags

    # 不用 PIPE 读 stderr：某些沙箱/Windows 环境会拒绝创建匿名管道；
    # 写日志文件同样能拿到错误详情，且对长输出更稳。
    log_path = target.parent / "ffmpeg.log"
    with open(log_path, "wb") as log_file:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            **popen_kwargs,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AudioConversionError("音频转码超时，请换更短的视频。") from exc

    detail = ""
    try:
        detail = _short_error(
            log_path.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        pass

    if process.returncode != 0:
        raise AudioConversionError(f"音频转码失败：{detail or 'ffmpeg 返回错误'}")

    if not target.exists() or target.stat().st_size <= 44:
        raise AudioConversionError("音频转码失败：没有生成有效的 WAV 文件。")
    return target


def slice_wav(
    wav_path: Path,
    chunk_seconds: int,
    max_chunks: int,
    outdir: Path,
) -> AudioChunks:
    """按固定时长把 WAV 切片；不重编码，只重写 PCM 帧。"""
    chunk_seconds = max(1, int(chunk_seconds))
    max_chunks = max(1, int(max_chunks))
    outdir.mkdir(parents=True, exist_ok=True)

    with wave.open(str(wav_path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        frame_rate = source.getframerate()
        total_frames = source.getnframes()
        if frame_rate <= 0 or total_frames <= 0:
            raise AudioConversionError("音频为空，无法切片。")

        frames_per_chunk = max(1, int(frame_rate * chunk_seconds))
        total_chunks = max(1, math.ceil(total_frames / frames_per_chunk))
        limit = min(total_chunks, max_chunks)
        paths: list[Path] = []

        for index in range(limit):
            remaining = total_frames - index * frames_per_chunk
            frames = source.readframes(min(frames_per_chunk, remaining))
            if not frames:
                break
            chunk_path = outdir / f"part_{index + 1:03d}.wav"
            with wave.open(str(chunk_path), "wb") as target:
                target.setnchannels(channels)
                target.setsampwidth(sample_width)
                target.setframerate(frame_rate)
                target.writeframes(frames)
            paths.append(chunk_path)

        duration = total_frames / frame_rate
        if not paths:
            raise AudioConversionError("音频切片失败：没有生成任何语音段。")
        return AudioChunks(
            paths=paths,
            duration=duration,
            total_chunks=total_chunks,
            truncated=total_chunks > limit,
        )


# --------------------------------------------------------------------------- #
# 视频格式选择
# --------------------------------------------------------------------------- #
def _format_size(fmt: dict, duration: float) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    tbr = fmt.get("tbr")
    if isinstance(tbr, (int, float)) and tbr > 0 and duration > 0:
        return int(float(tbr) * 1000 / 8 * duration)
    return None


def _video_rank(fmt: dict) -> tuple:
    vcodec = str(fmt.get("vcodec") or "").lower()
    ext = str(fmt.get("ext") or "").lower()
    tbr = fmt.get("tbr") or 0
    try:
        tbr = float(tbr)
    except (TypeError, ValueError):
        tbr = 0.0
    return (1 if vcodec.startswith("avc1") else 0, 1 if ext == "mp4" else 0, tbr)


def _audio_rank(fmt: dict) -> tuple:
    ext = str(fmt.get("ext") or "").lower()
    acodec = str(fmt.get("acodec") or "").lower()
    abr = fmt.get("abr") or fmt.get("tbr") or 0
    try:
        abr = float(abr)
    except (TypeError, ValueError):
        abr = 0.0
    return (1 if ext == "m4a" or acodec.startswith("mp4a") else 0, abr)


def choose_video_formats(
    info: dict[str, Any],
    max_height: int,
    max_bytes: int,
) -> list[FormatChoice]:
    """从 yt-dlp 的 formats 中挑出若干候选，优先 h264 + m4a。"""
    formats = [item for item in (info.get("formats") or []) if isinstance(item, dict)]
    duration = info.get("duration") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0

    video_only: list[tuple[int, dict]] = []
    audio_only: list[dict] = []
    progressive: list[tuple[int, dict]] = []

    for fmt in formats:
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        height = fmt.get("height")
        try:
            height = int(height)
        except (TypeError, ValueError):
            height = 0

        has_video = vcodec not in (None, "none")
        has_audio = acodec not in (None, "none")
        if has_video and has_audio and height > 0:
            progressive.append((height, fmt))
        elif has_video and height > 0:
            video_only.append((height, fmt))
        elif has_audio and not has_video:
            audio_only.append(fmt)

    best_audio = max(audio_only, key=_audio_rank) if audio_only else None
    audio_size = _format_size(best_audio, duration) if best_audio else 0

    choices: list[FormatChoice] = []
    seen: set[str] = set()
    heights = sorted({height for height, _ in video_only if height <= max_height}, reverse=True)

    for height in heights:
        candidates = [fmt for h, fmt in video_only if h == height]
        best_video = max(candidates, key=_video_rank)
        video_size = _format_size(best_video, duration)
        estimated = None
        if video_size is not None:
            estimated = video_size + (audio_size or 0)
        format_id = str(best_video.get("format_id") or "")
        if best_audio:
            format_id += f"+{best_audio.get('format_id')}"
        if not format_id or format_id in seen:
            continue
        seen.add(format_id)
        choices.append(
            FormatChoice(
                format_id=format_id,
                height=height,
                estimated_bytes=estimated,
                label=f"{height}P",
            )
        )

    # 只有渐进式流（音视频在一起）时，退而求其次。
    for height, fmt in sorted(progressive, key=lambda item: item[0], reverse=True):
        if height > max_height:
            continue
        format_id = str(fmt.get("format_id") or "")
        if not format_id or format_id in seen:
            continue
        seen.add(format_id)
        choices.append(
            FormatChoice(
                format_id=format_id,
                height=height,
                estimated_bytes=_format_size(fmt, duration),
                label=f"{height}P",
            )
        )

    if not choices:
        fallback = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
        choices.append(FormatChoice(format_id=fallback, height=None, estimated_bytes=None))

    limit = int(max_bytes * 0.97)
    fitting = [
        choice
        for choice in choices
        if choice.estimated_bytes is None or choice.estimated_bytes <= limit
    ]
    too_large = [choice for choice in choices if choice not in fitting]
    fitting.sort(key=lambda choice: choice.height or 0, reverse=True)
    too_large.sort(key=lambda choice: choice.estimated_bytes or 0)
    return (fitting + too_large)[:4]


# --------------------------------------------------------------------------- #
# yt-dlp 封装
# --------------------------------------------------------------------------- #
class _YdlLogger:
    def debug(self, message: str) -> None:
        if message and not message.startswith("[debug]"):
            logger.debug("yt-dlp: %s", redact(message))

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp: %s", redact(message))

    def error(self, message: str) -> None:
        logger.error("yt-dlp: %s", redact(message))


def map_download_error(exc: BaseException) -> UserVisibleError:
    if isinstance(exc, UserVisibleError):
        return exc
    text = _short_error(exc)
    lowered = text.lower()

    if "ffmpeg" in lowered and (
        "not found" in lowered or "not installed" in lowered or "path" in lowered
    ):
        return FfmpegMissingError(
            "未找到可用的 ffmpeg，无法合并/转码。请安装 ffmpeg 或填写 ffmpeg_path。"
        )
    if "unsupported url" in lowered:
        return UnsupportedLinkError("yt-dlp 不支持这个链接，请检查是否为 B 站视频。")
    if "http error 412" in lowered or "risk control" in lowered:
        return DownloadFailedError(
            "B 站拒绝了请求（风控/412），请稍后重试，或配置 B 站 Cookie。"
        )
    if "max-filesize" in lowered or "larger than" in lowered:
        return MediaTooLargeError("文件超过大小上限，已停止下载。")
    if "login" in lowered or "premium" in lowered or "vip" in lowered or "大会员" in text:
        return DownloadFailedError(
            "该视频需要登录或大会员权限，请在插件配置里填写 B 站 Cookie（SESSDATA）。"
        )
    if "private" in lowered or "隐私" in text:
        return DownloadFailedError("该视频是私密视频，无法访问。")
    if "requested format is not available" in lowered or "no video formats" in lowered:
        return DownloadFailedError("没有找到符合清晰度/大小要求的视频格式。")
    if (
        "not available" in lowered
        or "unavailable" in lowered
        or "does not exist" in lowered
        or "不存在" in text
        or "已删除" in text
        or "http error 404" in lowered
    ):
        return DownloadFailedError("视频不存在、已删除或当前地区不可用。")
    if "timed out" in lowered or "timeout" in lowered:
        return DownloadFailedError("下载超时，请稍后重试或换更短的视频。")
    if "http error 403" in lowered:
        return DownloadFailedError(
            "B 站拒绝访问（403），请稍后重试；必要时配置 Cookie 或代理。"
        )
    detail = text or "未知错误"
    return DownloadFailedError(f"下载失败：{detail}")


def _find_downloaded(root: Path, extensions: set[str]) -> Path | None:
    candidates: list[Path] = []
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if path.name.endswith((".part", ".ytdl", ".temp")):
            continue
        if path.suffix.lower() in extensions:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_size)


def _is_retryable_video_error(exc: BaseException) -> bool:
    """某些 B 站清晰度在当前账号/IP 下不可用时，换低一档往往能成功。"""
    text = _short_error(exc).lower()
    return (
        "requested format" in text
        or "no video formats" in text
        or "没有找到符合清晰度" in text
    )


class BiliService:
    """把 yt-dlp 的同步调用包在 asyncio.to_thread 里。"""

    def __init__(self, settings: PluginSettings, ffmpeg: str | None = None):
        self.settings = settings
        self.ffmpeg = ffmpeg

    def set_ffmpeg(self, ffmpeg: str) -> None:
        self.ffmpeg = ffmpeg

    def _base_opts(self, outdir: Path) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "no_color": True,
            "noplaylist": True,
            "socket_timeout": min(30, self.settings.timeout_seconds),
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "cachedir": False,
            "overwrites": True,
            "continuedl": True,
            "restrictfilenames": True,
            "windowsfilenames": True,
            "outtmpl": {"default": str(outdir / "%(id)s.%(ext)s")},
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": BILI_REFERER,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            "logger": _YdlLogger(),
            "nocheckcertificate": False,
            "geo_bypass": True,
            "ignoreerrors": False,
            "extract_flat": False,
        }
        if self.ffmpeg:
            opts["ffmpeg_location"] = self.ffmpeg
        if self.settings.cookies_file:
            opts["cookiefile"] = self.settings.cookies_file
        if self.settings.cookie:
            opts["http_headers"]["Cookie"] = self.settings.cookie
        if self.settings.proxy:
            opts["proxy"] = self.settings.proxy
        return opts

    async def _extract(
        self, url: str, opts: dict[str, Any], download: bool
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            try:
                import yt_dlp
            except ImportError as exc:
                raise MissingDependencyError(
                    "缺少 yt-dlp 依赖。请重载插件让 AstrBot 自动安装 requirements.txt，"
                    "或手动执行 pip install yt-dlp。"
                ) from exc
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(run), timeout=self.settings.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise DownloadFailedError("处理超时，请稍后重试或换更短的视频。") from exc
        except UserVisibleError:
            raise
        except Exception as exc:
            raise map_download_error(exc) from exc

        if not isinstance(info, dict):
            raise DownloadFailedError("解析失败：yt-dlp 没有返回视频信息。")
        return info

    async def _unwrap_info(self, info: dict[str, Any], page: int) -> dict[str, Any]:
        if info.get("_type") not in {"playlist", "multi_video"}:
            return info
        entries = [item for item in (info.get("entries") or []) if isinstance(item, dict)]
        if not entries:
            raise DownloadFailedError("没有找到可下载的视频。")
        index = min(max(page - 1, 0), len(entries) - 1)
        entry = entries[index]
        if not entry.get("formats"):
            entry_url = entry.get("webpage_url") or entry.get("url")
            if entry_url:
                opts = self._base_opts(Path(tempfile.gettempdir()))
                entry = await self._extract(str(entry_url), opts, False)
        if isinstance(entry, dict) and entry.get("_type") in {"playlist", "multi_video"}:
            return await self._unwrap_info(entry, 1)
        return entry

    async def probe(self, target: BiliTarget) -> MediaMeta:
        opts = self._base_opts(Path(tempfile.gettempdir()))
        info = await self._extract(target.url, opts, False)
        info = await self._unwrap_info(info, target.page)
        return MediaMeta(
            title=_clean_title(info.get("title") or info.get("fulltitle")),
            duration=_as_float(info.get("duration"), 0.0, 0.0, 86400.0),
            uploader=str(info.get("uploader") or ""),
            webpage_url=str(info.get("webpage_url") or target.url),
            info=info,
        )

    async def fetch_video(
        self, target: BiliTarget, meta: MediaMeta, workdir: Path
    ) -> DownloadedVideo:
        choices = choose_video_formats(
            meta.info, self.settings.max_video_height, self.settings.max_video_bytes
        )
        if not choices:
            raise DownloadFailedError("没有找到可下载的视频格式。")

        last_size = 0
        for index, choice in enumerate(choices):
            attempt = workdir / f"video_try_{index}"
            attempt.mkdir(parents=True, exist_ok=True)
            opts = self._base_opts(attempt)
            opts["format"] = choice.format_id
            opts["merge_output_format"] = "mp4"
            try:
                await self._extract(target.url, opts, True)
                path = _find_downloaded(
                    attempt, {".mp4", ".mkv", ".webm", ".flv", ".m4v"}
                )
                if path is None:
                    raise DownloadFailedError("下载完成但没有找到视频文件。")
                size = path.stat().st_size
                last_size = size
                if size <= self.settings.max_video_bytes:
                    return DownloadedVideo(path=path, height=choice.height, size_bytes=size)
                logger.info(
                    "视频 %s 大小 %s 超过上限 %s，尝试更低清晰度",
                    choice.label or choice.format_id,
                    size,
                    self.settings.max_video_bytes,
                )
                shutil.rmtree(attempt, ignore_errors=True)
            except UserVisibleError as exc:
                shutil.rmtree(attempt, ignore_errors=True)
                if index + 1 < len(choices) and _is_retryable_video_error(exc):
                    logger.info("清晰度 %s 不可用，尝试下一档", choice.label)
                    continue
                raise
            except Exception as exc:
                shutil.rmtree(attempt, ignore_errors=True)
                raise map_download_error(exc) from exc

        raise MediaTooLargeError(
            f"视频超过 {self.settings.max_video_mb}MB 上限，已尝试较低清晰度仍超出"
            f"（最后一次 {last_size / 1024 / 1024:.1f}MB）。可提高 max_video_mb，"
            "或改用 /提取音频。"
        )

    async def fetch_audio(
        self, target: BiliTarget, meta: MediaMeta, workdir: Path
    ) -> Path:
        attempt = workdir / "audio"
        attempt.mkdir(parents=True, exist_ok=True)
        opts = self._base_opts(attempt)
        opts["format"] = (
            "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"
        )
        opts["max_filesize"] = self.settings.max_audio_bytes
        await self._extract(target.url, opts, True)
        path = _find_downloaded(
            attempt, {".m4a", ".mp3", ".webm", ".opus", ".aac", ".flac", ".ogg", ".wav"}
        )
        if path is None:
            raise DownloadFailedError("下载完成但没有找到音频文件。")
        if path.stat().st_size > self.settings.max_audio_bytes:
            raise MediaTooLargeError(
                f"音频源文件超过 {self.settings.max_audio_mb}MB 上限。"
            )
        return path


# --------------------------------------------------------------------------- #
# 消息组件兼容层
# --------------------------------------------------------------------------- #
def _make_video_component(path: Path) -> Video:
    factory = getattr(Video, "fromFileSystem", None)
    if callable(factory):
        return factory(str(path))
    return Video(file=str(path), path=str(path))


def _make_record_component(path: Path) -> Record:
    factory = getattr(Record, "fromFileSystem", None)
    if callable(factory):
        return factory(str(path))
    return Record(file=str(path), path=str(path))


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
def _build_supported_platforms():
    enum = filter.PlatformAdapterType
    supported = enum.AIOCQHTTP | enum.QQOFFICIAL
    extra = getattr(enum, "QQOFFICIAL_WEBHOOK", None)
    if extra is not None:
        supported |= extra
    return supported


_SUPPORTED_PLATFORMS = _build_supported_platforms()


class BiliMediaPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.settings = PluginSettings.from_config(self.config)
        self.service = BiliService(self.settings)
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent)
        self._cooldowns: dict[str, float] = {}
        self._ffmpeg: str | None = None
        self._tmp_dirs: set[Path] = set()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._cleanup_delay = self.settings.cleanup_delay_seconds

    # ---------------------------- 指令入口 ---------------------------- #
    @filter.command("提取视频")
    @filter.platform_adapter_type(_SUPPORTED_PLATFORMS)
    async def extract_video(self, event: AstrMessageEvent, url: str = ""):
        async for result in self._handle_media(event, url, want_audio=False):
            yield result

    @filter.command("提取音频")
    @filter.platform_adapter_type(_SUPPORTED_PLATFORMS)
    async def extract_audio(self, event: AstrMessageEvent, url: str = ""):
        async for result in self._handle_media(event, url, want_audio=True):
            yield result

    # ---------------------------- 主流程 ------------------------------ #
    async def _handle_media(
        self, event: AstrMessageEvent, url: str, want_audio: bool
    ):
        self._block_llm(event)

        query = str(url or "").strip()
        if not query:
            query = self._strip_command(event)

        try:
            target = await parse_bili_target(query)
        except UserVisibleError as exc:
            yield event.plain_result(str(exc))
            return

        remaining = self._cooldown_remaining(event)
        if remaining > 0:
            yield event.plain_result(
                f"操作太频繁，请 {max(1, int(remaining))} 秒后再试。"
            )
            return
        self._mark_cooldown(event)

        workdir: Path | None = None
        acquired = False
        success = False
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=15.0)
                acquired = True
            except asyncio.TimeoutError:
                yield event.plain_result("当前任务较多，请稍后再试。")
                return

            workdir = Path(tempfile.mkdtemp(prefix="astrbot_bili_media_"))
            self._tmp_dirs.add(workdir)
            ffmpeg = self._ensure_ffmpeg()

            meta = await self.service.probe(target)
            if self.settings.send_progress:
                yield event.plain_result(self._progress_text(meta, want_audio))

            if want_audio:
                source = await self.service.fetch_audio(target, meta, workdir)
                wav_path = workdir / "audio.wav"
                await convert_to_wav(
                    source,
                    wav_path,
                    ffmpeg,
                    timeout=min(240, self.settings.timeout_seconds),
                )
                chunks = slice_wav(
                    wav_path,
                    self.settings.audio_chunk_seconds,
                    self.settings.max_audio_chunks,
                    workdir / "chunks",
                )
                for index, chunk in enumerate(chunks.paths):
                    self._track_file(event, chunk)
                    success = True
                    yield event.chain_result([_make_record_component(chunk)])
                    if index + 1 < len(chunks.paths):
                        await asyncio.sleep(self.settings.audio_send_interval)
                if chunks.truncated:
                    yield event.plain_result(
                        f"音频共 {_fmt_duration(chunks.duration)}，超过单次 "
                        f"{len(chunks.paths)} 段限制，仅发送前 {len(chunks.paths)} 段"
                        f"（每段 {self.settings.audio_chunk_seconds} 秒）。"
                    )
            else:
                downloaded = await self.service.fetch_video(target, meta, workdir)
                self._track_file(event, downloaded.path)
                success = True
                yield event.chain_result([_make_video_component(downloaded.path)])

        except UserVisibleError as exc:
            logger.warning("B站媒体处理失败：%s", redact(str(exc)))
            yield event.plain_result(str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("B站媒体处理出现未预期错误", exc_info=True)
            yield event.plain_result("处理失败，请稍后再试。详情已写入日志。")
        finally:
            if acquired:
                self._semaphore.release()
            if workdir is not None:
                if success:
                    self._schedule_cleanup(workdir)
                else:
                    self._rmtree(workdir)

    # ---------------------------- 辅助 -------------------------------- #
    def _ensure_ffmpeg(self) -> str:
        if self._ffmpeg is None:
            self._ffmpeg = resolve_ffmpeg(self.settings.ffmpeg_path)
            self.service.set_ffmpeg(self._ffmpeg)
        return self._ffmpeg

    def _block_llm(self, event: AstrMessageEvent) -> None:
        setter = getattr(event, "should_call_llm", None)
        if callable(setter):
            try:
                setter(False)
            except Exception:
                logger.debug("should_call_llm(False) 调用失败", exc_info=True)

    def _strip_command(self, event: AstrMessageEvent) -> str:
        text = str(event.get_message_str() or "").strip()
        for name in ("提取视频", "提取音频"):
            text = re.sub(
                rf"^\s*[!！/／]?\s*{re.escape(name)}\s*", "", text, count=1
            )
        return text.strip()

    def _cooldown_key(self, event: AstrMessageEvent) -> str:
        platform = ""
        sender = ""
        try:
            platform = str(event.get_platform_name() or "")
            sender = str(event.get_sender_id() or "")
        except Exception:
            pass
        if not sender:
            try:
                sender = str(event.get_group_id() or "")
            except Exception:
                sender = ""
        return f"{platform}:{sender or 'unknown'}"

    def _cooldown_remaining(self, event: AstrMessageEvent) -> float:
        key = self._cooldown_key(event)
        now = time.monotonic()
        if len(self._cooldowns) > 512:
            self._cooldowns = {
                item: until for item, until in self._cooldowns.items() if until > now
            }
        until = self._cooldowns.get(key, 0.0)
        if until <= now:
            self._cooldowns.pop(key, None)
            return 0.0
        return until - now

    def _mark_cooldown(self, event: AstrMessageEvent) -> None:
        seconds = self.settings.cooldown_seconds
        if seconds > 0:
            self._cooldowns[self._cooldown_key(event)] = time.monotonic() + seconds

    def _progress_text(self, meta: MediaMeta, want_audio: bool) -> str:
        title = _clean_title(meta.title)
        duration = f"（{_fmt_duration(meta.duration)}）" if meta.duration > 0 else ""
        if want_audio:
            estimate = ""
            if meta.duration > 0:
                count = min(
                    math.ceil(meta.duration / self.settings.audio_chunk_seconds),
                    self.settings.max_audio_chunks,
                )
                estimate = f"，预计 {count} 段语音"
            return f"已识别《{title}》{duration}，正在提取音频{estimate}…"
        return (
            f"已识别《{title}》{duration}，正在下载 ≤"
            f"{self.settings.max_video_height}P 视频"
            f"（上限 {self.settings.max_video_mb}MB）…"
        )

    def _track_file(self, event: AstrMessageEvent, path: Path) -> None:
        tracker = getattr(event, "track_temporary_local_file", None)
        if callable(tracker):
            try:
                tracker(str(path))
            except Exception:
                logger.debug("track_temporary_local_file 调用失败", exc_info=True)

    def _schedule_cleanup(self, path: Path) -> None:
        delay = float(self._cleanup_delay)
        if delay <= 0:
            self._rmtree(path)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._rmtree(path)
            return

        async def cleanup() -> None:
            try:
                await asyncio.sleep(delay)
            finally:
                self._rmtree(path)

        task = loop.create_task(cleanup())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _rmtree(self, path: Path) -> None:
        self._tmp_dirs.discard(path)
        shutil.rmtree(path, ignore_errors=True)

    async def terminate(self) -> None:
        """插件卸载/重载时清理临时文件与后台任务。"""
        tasks = list(self._cleanup_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for path in list(self._tmp_dirs):
            self._rmtree(path)
        logger.debug("astrbot_plugin_bili_media 已卸载")
