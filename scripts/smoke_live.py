"""Live smoke test (network + yt-dlp + ffmpeg).

Run only in a dev environment:
    python scripts/smoke_live.py

It downloads a short public Bilibili video's audio and a low-resolution video,
then verifies WAV slicing. Nothing is sent to QQ.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))  # 假 astrbot 包
sys.path.insert(0, str(ROOT))

import main as bili  # noqa: E402

URL = "https://www.bilibili.com/video/BV1GJ411x7h7"
WORK = ROOT / ".tmp" / "live-smoke"


async def run() -> int:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    target = await bili.parse_bili_target(URL)
    print("target:", target.video_id, "page", target.page, target.url)

    ffmpeg = bili.resolve_ffmpeg("")
    print("ffmpeg:", ffmpeg)

    settings = bili.PluginSettings(max_video_height=360, max_video_mb=20)
    service = bili.BiliService(settings, ffmpeg)
    meta = await service.probe(target)
    print("meta:", meta.title, meta.duration, meta.uploader)
    print("formats:", len(meta.info.get("formats") or []))

    audio_src = await service.fetch_audio(target, meta, WORK / "audio-job")
    print("audio src:", audio_src.name, audio_src.stat().st_size)

    wav = await bili.convert_to_wav(audio_src, WORK / "audio.wav", ffmpeg)
    print("wav:", wav.name, wav.stat().st_size)

    chunks = bili.slice_wav(wav, 60, 5, WORK / "chunks")
    print(
        "chunks:",
        len(chunks.paths),
        "total",
        chunks.total_chunks,
        "truncated",
        chunks.truncated,
        "duration",
        round(chunks.duration, 2),
    )
    for chunk in chunks.paths:
        print("  -", chunk.name, chunk.stat().st_size)

    video = await service.fetch_video(target, meta, WORK / "video-job")
    print(
        "video:",
        video.path.name,
        round(video.size_bytes / 1024 / 1024, 2),
        "MB",
        "height",
        video.height,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
