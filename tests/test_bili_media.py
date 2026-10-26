"""Offline tests for astrbot_plugin_bili_media (no AstrBot install required)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
import wave
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # 假 astrbot 包优先
sys.path.insert(0, str(HERE.parent))   # main.py

# 沙箱下系统临时目录可能不可写，测试统一放在工作区已存在的 .tmp 下。
TEST_TMPDIR = HERE.parent / ".tmp" / "unittest"
TEST_TMPDIR.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TEST_TMPDIR)


def safe_mkdtemp(suffix=None, prefix=None, dir=None):
    """tempfile.mkdtemp 在 Windows 上用 0o700 建目录，DSH 沙箱下会不可写。

    测试环境改用默认权限建目录；生产代码仍使用标准库的安全临时目录。
    """
    base = Path(dir or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        candidate = base / f"{prefix or 'tmp'}{uuid.uuid4().hex[:10]}{suffix or ''}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return str(candidate)
    raise RuntimeError("无法创建测试临时目录")


tempfile.mkdtemp = safe_mkdtemp

import main as bili  # noqa: E402


def write_wav(path: Path, seconds: float, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, int(rate * seconds))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * frames)
    return path


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


class FakeEvent:
    def __init__(self, text: str = "", sender: str = "10001"):
        self._text = text
        self._sender = sender
        self.llm = None
        self.tracked: list[str] = []

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return ""

    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return {"text": text}

    def chain_result(self, chain):
        return {"chain": chain}

    def should_call_llm(self, value):
        self.llm = value

    def track_temporary_local_file(self, path):
        self.tracked.append(path)


class FakeService:
    def __init__(self, duration: float = 2.5, fail: Exception | None = None):
        self.duration = duration
        self.fail = fail
        self.probe_calls = 0
        self.audio_calls = 0
        self.video_calls = 0
        self.cookie = None
        self.active_processes = set()

    def set_cookie(self, cookie):
        self.cookie = cookie

    async def aclose(self):
        self.active_processes.clear()

    async def probe(self, target):
        self.probe_calls += 1
        if self.fail:
            raise self.fail
        return bili.MediaMeta(
            title="测试视频",
            duration=self.duration,
            uploader="tester",
            webpage_url=target.url,
            info={},
        )

    async def fetch_audio(self, target, meta, workdir):
        self.audio_calls += 1
        return write_wav(Path(workdir) / "source.wav", self.duration)

    async def fetch_video(self, target, meta, workdir):
        self.video_calls += 1
        path = Path(workdir) / "video.mp4"
        path.write_bytes(b"fake-mp4")
        return bili.DownloadedVideo(path=path, height=720, size_bytes=8)


class FakeLoginClient:
    def __init__(self, polls):
        self.polls = list(polls)
        self.generated = 0

    async def generate(self):
        self.generated += 1
        return bili.QrCode(url="https://example.com/qr", qrcode_key="test-key")

    async def poll(self, qrcode_key):
        if self.polls:
            return self.polls.pop(0)
        return bili.QrPoll(
            state=bili.QR_STATE_SUCCESS,
            payload={"url": ""},
            set_cookies=[
                "SESSDATA=test-sessdata; Path=/; Domain=.bilibili.com",
                "bili_jct=test-jct; Path=/",
                "DedeUserID=1001; Path=/",
            ],
        )

    async def fetch_nav(self, cookie):
        return {"uname": "测试UP", "mid": 1001}

    extract_cookie = staticmethod(bili.BiliLoginClient.extract_cookie)


class PluginTestBase(unittest.TestCase):
    def setUp(self):
        self._convert_patcher = mock.patch.object(
            bili, "convert_to_wav", new=self._fake_convert
        )
        self._convert_patcher.start()
        self.addCleanup(self._convert_patcher.stop)
        self.plugins: list[bili.BiliMediaPlugin] = []

    async def _fake_convert(self, source, target, ffmpeg, timeout=180, **kwargs):
        shutil.copyfile(source, target)
        return target

    def make_plugin(self, service: FakeService | None = None, **config):
        base = {
            "audio_chunk_seconds": 1,
            "max_audio_chunks": 30,
            "send_progress": True,
            "cooldown_seconds": 0,
        }
        base.update(config)
        plugin = bili.BiliMediaPlugin(None, base)
        plugin._ffmpeg = "ffmpeg"
        plugin.service = service or FakeService()
        plugin._schedule_cleanup = lambda path: None
        plugin._data_dir_path = Path(
            tempfile.mkdtemp(prefix="bili_credentials_")
        )
        plugin._credentials_loaded = True
        self.plugins.append(plugin)
        self.addCleanup(self._cleanup_plugin, plugin)
        return plugin

    def _cleanup_plugin(self, plugin):
        for path in list(plugin._tmp_dirs):
            shutil.rmtree(path, ignore_errors=True)
        for task in list(plugin._cleanup_tasks):
            task.cancel()
        if plugin._data_dir_path is not None:
            shutil.rmtree(plugin._data_dir_path, ignore_errors=True)

    async def collect(self, plugin, event, url, audio):
        items = []
        async for item in plugin._handle_media(event, url, audio):
            items.append(item)
        return items

    def run_handler(self, plugin, event, url, audio):
        return asyncio.run(self.collect(plugin, event, url, audio))

    async def collect_login(self, plugin, event):
        items = []
        async for item in plugin.account_login(event):
            items.append(item)
        return items

    async def collect_status(self, plugin, event):
        items = []
        async for item in plugin.account_status(event):
            items.append(item)
        return items

    async def collect_logout(self, plugin, event):
        items = []
        async for item in plugin.account_logout(event):
            items.append(item)
        return items


class UrlTests(unittest.TestCase):
    def test_bv_with_page_and_tracking(self):
        target = asyncio.run(
            bili.parse_bili_target(
                "看这个 https://www.bilibili.com/video/BV1GJ411x7h7/"
                "?p=2&spm_id_from=333.999，谢谢"
            )
        )
        self.assertEqual(target.video_id, "BV1GJ411x7h7")
        self.assertEqual(target.page, 2)
        self.assertEqual(
            target.url, "https://www.bilibili.com/video/BV1GJ411x7h7?p=2"
        )

    def test_bare_ids(self):
        self.assertEqual(
            asyncio.run(bili.parse_bili_target("BV1GJ411x7h7")).video_id,
            "BV1GJ411x7h7",
        )
        self.assertEqual(
            asyncio.run(bili.parse_bili_target("av170001")).video_id, "av170001"
        )

    def test_bv_case_is_preserved(self):
        bvid = "BV1GJ411x7h7"
        self.assertEqual(bili._video_id_from_segment(bvid), bvid)
        target = asyncio.run(
            bili.parse_bili_target(f"https://www.bilibili.com/video/{bvid}")
        )
        self.assertEqual(target.video_id, bvid)
        self.assertIn(bvid, target.url)

    def test_lowercase_bv_is_rejected(self):
        # BVID 区分大小写；不能把 bv... 改写成 BV... 后当成有效 ID。
        with self.assertRaises(bili.UsageError):
            asyncio.run(bili.parse_bili_target("bv1gj411x7h7"))

    def test_no_scheme_and_mobile_host(self):
        target = asyncio.run(
            bili.parse_bili_target("m.bilibili.com/video/BV1GJ411x7h7")
        )
        self.assertEqual(target.video_id, "BV1GJ411x7h7")

    def test_b23_direct_bv_link(self):
        target = asyncio.run(
            bili.parse_bili_target("https://b23.tv/BV1GJ411x7h7?p=2")
        )
        self.assertEqual(target.video_id, "BV1GJ411x7h7")
        self.assertEqual(target.page, 2)

    def test_b23_arbitrary_code_rejected(self):
        with self.assertRaises(bili.UnsupportedLinkError) as ctx:
            asyncio.run(bili.parse_bili_target("https://b23.tv/EkD01Wz"))
        self.assertIn("完整视频链接", str(ctx.exception))

    def test_rejects_other_sites(self):
        with self.assertRaises(bili.UnsupportedLinkError):
            asyncio.run(bili.parse_bili_target("https://youtube.com/watch?v=abc"))

    def test_rejects_lookalike_host(self):
        with self.assertRaises(bili.UnsupportedLinkError):
            asyncio.run(
                bili.parse_bili_target(
                    "https://evilbilibili.com/video/BV1GJ411x7h7"
                )
            )

    def test_rejects_bangumi_and_live(self):
        for url in (
            "https://www.bilibili.com/bangumi/play/ep123",
            "https://live.bilibili.com/123",
        ):
            with self.subTest(url=url), self.assertRaises(bili.UnsupportedLinkError):
                asyncio.run(bili.parse_bili_target(url))

    def test_missing_url_has_usage(self):
        with self.assertRaises(bili.UsageError) as ctx:
            asyncio.run(bili.parse_bili_target("/提取视频"))
        self.assertIn("用法", str(ctx.exception))

    def test_invalid_page_falls_back_to_one(self):
        target = asyncio.run(
            bili.parse_bili_target(
                "https://www.bilibili.com/video/BV1GJ411x7h7?p=abc"
            )
        )
        self.assertEqual(target.page, 1)


class FormatTests(unittest.TestCase):
    MB = 1024 * 1024

    @staticmethod
    def fmt(fid, height, vcodec, acodec, size=None, ext="mp4"):
        item = {
            "format_id": fid,
            "height": height,
            "vcodec": vcodec,
            "acodec": acodec,
            "ext": ext,
            "tbr": 1000,
        }
        if size is not None:
            item["filesize"] = size
        return item

    def test_720p_fits(self):
        info = {
            "duration": 100,
            "formats": [
                self.fmt("v720", 720, "avc1.64001F", "none", 20 * self.MB),
                self.fmt("v480", 480, "avc1.64001F", "none", 10 * self.MB),
                self.fmt("aud", None, "none", "mp4a.40.2", 2 * self.MB, "m4a"),
            ],
        }
        choices = bili.choose_video_formats(info, 720, 30 * self.MB)
        self.assertTrue(choices)
        self.assertEqual(choices[0].height, 720)
        self.assertIn("v720+aud", choices[0].format_id)

    def test_oversize_skips_to_lower_height(self):
        info = {
            "duration": 100,
            "formats": [
                self.fmt("v720", 720, "avc1", "none", 40 * self.MB),
                self.fmt("v480", 480, "avc1", "none", 10 * self.MB),
                self.fmt("aud", None, "none", "mp4a", 2 * self.MB, "m4a"),
            ],
        }
        choices = bili.choose_video_formats(info, 720, 30 * self.MB)
        self.assertEqual(choices[0].height, 480)
        self.assertIn("v480+aud", choices[0].format_id)

    def test_prefers_avc1(self):
        info = {
            "duration": 100,
            "formats": [
                self.fmt("hevc", 720, "hev1.1.6", "none", 10 * self.MB),
                self.fmt("h264", 720, "avc1.64001F", "none", 11 * self.MB),
                self.fmt("aud", None, "none", "mp4a", 2 * self.MB, "m4a"),
            ],
        }
        choices = bili.choose_video_formats(info, 720, 30 * self.MB)
        self.assertIn("h264+aud", choices[0].format_id)

    def test_progressive_fallback(self):
        info = {
            "duration": 100,
            "formats": [
                self.fmt("p360", 360, "avc1", "mp4a", 8 * self.MB),
            ],
        }
        choices = bili.choose_video_formats(info, 720, 30 * self.MB)
        self.assertEqual(choices[0].height, 360)
        self.assertEqual(choices[0].format_id, "p360")

    def test_no_formats_returns_expression(self):
        choices = bili.choose_video_formats({"formats": []}, 720, 30 * self.MB)
        self.assertEqual(len(choices), 1)
        self.assertIsNone(choices[0].height)
        self.assertIn("height<=720", choices[0].format_id)

    def test_format_error_mapping_is_retryable(self):
        error = bili.map_download_error(
            Exception("ERROR: Requested format is not available")
        )
        self.assertIn("清晰度", str(error))
        self.assertTrue(bili._is_retryable_video_error(error))


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bili_test_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_slice_wav(self):
        source = write_wav(self.tmp / "a.wav", 2.5)
        chunks = bili.slice_wav(source, 1, 10, self.tmp / "out")
        self.assertEqual(len(chunks.paths), 3)
        self.assertFalse(chunks.truncated)
        durations = [wav_duration(path) for path in chunks.paths]
        self.assertAlmostEqual(durations[0], 1.0, places=2)
        self.assertAlmostEqual(durations[1], 1.0, places=2)
        self.assertAlmostEqual(durations[2], 0.5, places=2)

    def test_slice_truncation(self):
        source = write_wav(self.tmp / "a.wav", 3.5)
        chunks = bili.slice_wav(source, 1, 2, self.tmp / "out")
        self.assertEqual(len(chunks.paths), 2)
        self.assertTrue(chunks.truncated)
        self.assertEqual(chunks.total_chunks, 4)

    def test_empty_wav_rejected(self):
        source = self.tmp / "empty.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
        with self.assertRaises(bili.AudioConversionError):
            bili.slice_wav(source, 1, 10, self.tmp / "out")


class FfmpegTests(unittest.TestCase):
    def test_configured_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="bili_ffmpeg_"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        exe = tmp / "ffmpeg"
        exe.write_bytes(b"")
        self.assertEqual(bili.resolve_ffmpeg(str(exe)), str(exe))

    def test_missing(self):
        with (
            mock.patch.object(bili.shutil, "which", return_value=None),
            mock.patch.object(bili, "_imageio_ffmpeg_exe", return_value=None),
        ):
            with self.assertRaises(bili.FfmpegMissingError):
                bili.resolve_ffmpeg("")

    def test_limit_settings_are_clamped(self):
        settings = bili.PluginSettings.from_config(
            {"max_download_mb": 0, "max_parse_mb": 999}
        )
        self.assertEqual(settings.max_download_mb, 10)
        self.assertEqual(settings.max_parse_mb, 128)


class WorkerTests(unittest.TestCase):
    def test_child_env_includes_sys_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="bili_site_packages_"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        sys.path.insert(0, str(tmp))
        self.addCleanup(lambda: sys.path.remove(str(tmp)))
        env = bili._build_child_env()
        entries = env.get("PYTHONPATH", "").split(os.pathsep)
        self.assertIn(str(tmp.resolve()), entries)

    def test_timeout_kills_worker(self):
        tmp = Path(tempfile.mkdtemp(prefix="bili_worker_"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        worker = tmp / "slow_worker.py"
        worker.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

        settings = bili.PluginSettings(timeout_seconds=1)
        service = bili.BiliService(settings)
        service.worker_path = worker

        started = time.monotonic()
        with self.assertRaises(bili.DownloadFailedError):
            asyncio.run(
                service._extract("https://example.com/video", {}, download=False)
            )
        self.assertLess(time.monotonic() - started, 8.0)


class RedactTests(unittest.TestCase):
    def test_cookie_and_url_credentials(self):
        text = "Cookie: SESSDATA=abc123; bili_jct=xyz\nhttps://u:p@example.com/x"
        out = bili.redact(text)
        self.assertNotIn("abc123", out)
        self.assertNotIn("xyz", out)
        self.assertNotIn("p@example", out)

    def test_short_error_strips_ansi(self):
        self.assertEqual(bili._short_error("\x1b[31mERROR: boom\x1b[0m"), "boom")


class LoginTests(PluginTestBase):
    def setUp(self):
        super().setUp()
        self._qr_patcher = mock.patch.object(bili, "make_qr_png", new=self._fake_qr)
        self._qr_patcher.start()
        self.addCleanup(self._qr_patcher.stop)

    @staticmethod
    def _fake_qr(data, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        return target

    def _success_poll(self):
        return bili.QrPoll(
            state=bili.QR_STATE_SUCCESS,
            payload={"url": ""},
            set_cookies=[
                "SESSDATA=test-sessdata; Path=/; Domain=.bilibili.com",
                "bili_jct=test-jct; Path=/",
                "DedeUserID=1001; Path=/",
            ],
        )

    def test_login_saves_credentials(self):
        plugin = self.make_plugin()
        plugin.login_client = FakeLoginClient([self._success_poll()])
        event = FakeEvent()
        items = asyncio.run(self.collect_login(plugin, event))

        self.assertEqual(len(items), 2)
        chain = items[0]["chain"]
        self.assertEqual(len(chain), 2)
        self.assertIn("二维码", chain[0].text)
        self.assertTrue(Path(chain[1].path).is_file())
        self.assertIn("登录成功", items[1]["text"])
        self.assertIn("测试UP", items[1]["text"])
        self.assertIs(event.llm, False)

        path = plugin._data_dir_path / bili.CREDENTIALS_FILENAME
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("SESSDATA=test-sessdata", data["cookie"])
        self.assertEqual(data["uid"], "1001")
        self.assertEqual(plugin.service.cookie, data["cookie"])

    def test_login_scanned_then_success(self):
        plugin = self.make_plugin()
        plugin.login_client = FakeLoginClient(
            [
                bili.QrPoll(bili.QR_STATE_SCANNED, {}, []),
                self._success_poll(),
            ]
        )
        items = asyncio.run(self.collect_login(plugin, FakeEvent()))
        self.assertEqual(len(items), 3)
        self.assertIn("已扫码", items[1]["text"])
        self.assertIn("登录成功", items[2]["text"])

    def test_login_expired(self):
        plugin = self.make_plugin()
        plugin.login_client = FakeLoginClient(
            [bili.QrPoll(bili.QR_STATE_EXPIRED, {}, [])]
        )
        items = asyncio.run(self.collect_login(plugin, FakeEvent()))
        self.assertEqual(len(items), 2)
        self.assertIn("已过期", items[1]["text"])
        self.assertFalse(
            (plugin._data_dir_path / bili.CREDENTIALS_FILENAME).exists()
        )

    def test_status_and_logout(self):
        plugin = self.make_plugin()
        plugin.login_client = FakeLoginClient([self._success_poll()])
        asyncio.run(self.collect_login(plugin, FakeEvent()))

        status = asyncio.run(self.collect_status(plugin, FakeEvent()))
        self.assertIn("测试UP", status[0]["text"])

        logout = asyncio.run(self.collect_logout(plugin, FakeEvent()))
        self.assertIn("已退出", logout[0]["text"])
        self.assertIsNone(plugin.service.cookie)
        self.assertFalse(
            (plugin._data_dir_path / bili.CREDENTIALS_FILENAME).exists()
        )

    def test_stored_credentials_applied_on_first_use(self):
        plugin = self.make_plugin()
        path = plugin._data_dir_path / bili.CREDENTIALS_FILENAME
        path.write_text(
            json.dumps({"cookie": "SESSDATA=stored; bili_jct=x", "uid": "42"}),
            encoding="utf-8",
        )
        plugin._credentials_loaded = False
        plugin._ensure_credentials()
        self.assertEqual(plugin.service.cookie, "SESSDATA=stored; bili_jct=x")

    def test_extract_cookie_falls_back_to_url(self):
        poll = bili.QrPoll(
            state=0,
            payload={
                "url": "https://passport.biligame.com/crossDomain?"
                "SESSDATA=from%2Curl&bili_jct=jwt&DedeUserID=7"
            },
            set_cookies=[],
        )
        cookie = bili.BiliLoginClient.extract_cookie(poll)
        self.assertIn("SESSDATA=from,url", cookie)
        self.assertIn("DedeUserID=7", cookie)

    def test_extract_cookie_without_sessdata_fails(self):
        poll = bili.QrPoll(state=0, payload={}, set_cookies=["foo=bar"])
        with self.assertRaises(bili.UserVisibleError):
            bili.BiliLoginClient.extract_cookie(poll)


class HandlerTests(PluginTestBase):
    URL = "https://www.bilibili.com/video/BV1GJ411x7h7"

    def test_video_success(self):
        plugin = self.make_plugin()
        event = FakeEvent()
        items = self.run_handler(plugin, event, self.URL, False)
        self.assertEqual(len(items), 2)
        self.assertIn("已识别", items[0]["text"])
        self.assertIs(event.llm, False)
        video = items[1]["chain"][0]
        self.assertTrue(Path(video.path).is_file())
        self.assertEqual(len(event.tracked), 1)

    def test_audio_success_and_chunks(self):
        plugin = self.make_plugin(FakeService(duration=2.5))
        event = FakeEvent()
        items = self.run_handler(plugin, event, self.URL, True)
        self.assertEqual(len(items), 4)  # 进度 + 3 段语音
        self.assertIn("预计 3 段语音", items[0]["text"])
        records = [item["chain"][0] for item in items[1:]]
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertTrue(Path(record.path).is_file())
        self.assertEqual(len(event.tracked), 3)

    def test_audio_truncated(self):
        plugin = self.make_plugin(FakeService(duration=3.5), max_audio_chunks=2)
        event = FakeEvent()
        items = self.run_handler(plugin, event, self.URL, True)
        self.assertEqual(len(items), 4)  # 进度 + 2 段 + 截断提示
        self.assertIn("仅发送前 2 段", items[-1]["text"])

    def test_no_progress(self):
        plugin = self.make_plugin(send_progress=False)
        event = FakeEvent()
        items = self.run_handler(plugin, event, self.URL, False)
        self.assertEqual(len(items), 1)
        self.assertIn("chain", items[0])

    def test_cooldown(self):
        plugin = self.make_plugin(cooldown_seconds=60)
        event = FakeEvent()
        first = self.run_handler(plugin, event, self.URL, False)
        second = self.run_handler(plugin, event, self.URL, False)
        self.assertEqual(len(first), 2)
        self.assertIn("频繁", second[0]["text"])
        self.assertEqual(plugin.service.probe_calls, 1)

    def test_invalid_link(self):
        plugin = self.make_plugin()
        event = FakeEvent()
        items = self.run_handler(plugin, event, "https://youtube.com/watch?v=x", False)
        self.assertIn("只支持 B 站", items[0]["text"])
        self.assertEqual(plugin.service.probe_calls, 0)

    def test_usage_via_message_text(self):
        plugin = self.make_plugin()
        event = FakeEvent(text="/提取视频")
        items = self.run_handler(plugin, event, "", False)
        self.assertIn("用法", items[0]["text"])

    def test_download_error_is_user_facing(self):
        service = FakeService(fail=bili.DownloadFailedError("下载失败：测试错误"))
        plugin = self.make_plugin(service)
        items = self.run_handler(plugin, FakeEvent(), self.URL, False)
        self.assertEqual(items[0]["text"], "下载失败：测试错误")

    def test_ffmpeg_missing(self):
        plugin = self.make_plugin()
        plugin._ffmpeg = None
        with mock.patch.object(
            bili,
            "resolve_ffmpeg",
            side_effect=bili.FfmpegMissingError("未找到 ffmpeg，无法处理音视频。"),
        ):
            items = self.run_handler(plugin, FakeEvent(), self.URL, False)
        self.assertIn("ffmpeg", items[0]["text"])
        self.assertEqual(plugin.service.probe_calls, 0)

    def test_terminate_cleans_tmp_dirs(self):
        plugin = self.make_plugin()
        path = Path(tempfile.mkdtemp(prefix="bili_cleanup_"))
        plugin._tmp_dirs.add(path)
        asyncio.run(plugin.terminate())
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
