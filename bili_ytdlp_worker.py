"""Isolated yt-dlp worker.

Runs one yt-dlp operation in a separate process so the parent can reliably
terminate it on timeout. The parent writes a JSON request and reads a JSON
result; cookies never appear in the command line.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


class _SilentLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: bili_ytdlp_worker.py request.json result.json", file=sys.stderr)
        return 2

    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])

    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result_path.write_text(
            json.dumps(
                {"ok": False, "error": f"读取 yt-dlp 请求失败：{exc}", "error_type": type(exc).__name__},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 2

    opts = dict(payload.get("opts") or {})
    # 这些对象无法跨进程传递，worker 自己提供静默 logger。
    opts.pop("logger", None)
    opts.pop("progress_hooks", None)
    opts["logger"] = _SilentLogger()

    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                str(payload.get("url") or ""), download=bool(payload.get("download"))
            )
            if info is None:
                raise RuntimeError("yt-dlp 没有返回视频信息")
            info = ydl.sanitize_info(info)
        result = {"ok": True, "info": info}
    except BaseException as exc:
        # 写 stderr 供父进程记录，结果文件只放脱敏后的错误。
        traceback.print_exc()
        result = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }

    try:
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception as exc:
        print(f"写入 yt-dlp 结果失败：{exc}", file=sys.stderr)
        return 3
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
