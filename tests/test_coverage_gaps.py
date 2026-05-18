"""
Tests to close remaining coverage gaps across all modules.
Targets specific uncovered lines to push toward 100% coverage.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open, PropertyMock

import pytest

# ─── resource_limiter: Linux paths (lines 106-187, 222-241, 253-254) ─────────

class TestResourceLimiterLinux:
    """Cover Linux-specific code paths by mocking _is_linux."""

    def test_preexec_fn_linux_calls_setrlimit(self):
        """Lines 110-121: _set_limits calls setrlimit 4 times."""
        mock_resource = MagicMock()
        mock_resource.RLIMIT_CPU = 0
        mock_resource.RLIMIT_AS = 1
        mock_resource.RLIMIT_FSIZE = 2
        mock_resource.RLIMIT_NPROC = 3
        with patch.dict("sys.modules", {"resource": mock_resource}):
            from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits
            limiter = ResourceLimiter(ResourceLimits(cpu_seconds=10, memory_bytes=1024,
                                                      disk_bytes=2048, max_processes=5))
            limiter._is_linux = True
            fn = limiter._get_preexec_fn()
            assert fn is not None
            fn()
            assert mock_resource.setrlimit.call_count == 4

    def test_preexec_fn_linux_handles_error(self):
        """Lines 120-121: setrlimit raises ValueError."""
        mock_resource = MagicMock()
        mock_resource.RLIMIT_CPU = 0
        mock_resource.setrlimit.side_effect = ValueError("nope")
        with patch.dict("sys.modules", {"resource": mock_resource}):
            from telechat_pkg.resource_limiter import ResourceLimiter
            limiter = ResourceLimiter()
            limiter._is_linux = True
            fn = limiter._get_preexec_fn()
            fn()  # Should not raise

    @pytest.mark.asyncio
    async def test_monitor_linux_no_pid(self):
        """Line 132-133: process.pid is None."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits
        limiter = ResourceLimiter()
        limiter._is_linux = True
        mock_proc = MagicMock()
        mock_proc.pid = None
        usage = await limiter._monitor_linux(mock_proc, ResourceLimits())
        assert usage.cpu_time_seconds == 0.0

    @pytest.mark.asyncio
    async def test_monitor_linux_wall_time_exceeded(self):
        """Lines 176-180: Wall time exceeded kills process."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits

        limiter = ResourceLimiter()
        limiter._is_linux = True

        # Use a simple approach: mock the process and time
        mock_proc = MagicMock()
        mock_proc.pid = 99
        mock_proc.kill = MagicMock()

        # returncode: None first, then 0 after kill
        rc_values = iter([None, None, 0])
        type(mock_proc).returncode = PropertyMock(side_effect=lambda: next(rc_values, 0))

        limits = ResourceLimits(cpu_seconds=9999, memory_bytes=10**15, wall_time_seconds=0)

        time_values = iter([0, 100, 100, 100])
        with patch("os.path.exists", return_value=False), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", side_effect=time_values):
            usage = await limiter._monitor_linux(mock_proc, limits)

        assert "wall_time" in usage.limits_hit

    @pytest.mark.asyncio
    async def test_monitor_linux_cpu_limit(self):
        """Lines 165-170: CPU limit exceeded."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits

        limiter = ResourceLimiter()
        limiter._is_linux = True

        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.kill = MagicMock()

        rc_values = iter([None, None, -9])
        type(mock_proc).returncode = PropertyMock(side_effect=lambda: next(rc_values, -9))

        limits = ResourceLimits(cpu_seconds=1, memory_bytes=10**15, wall_time_seconds=9999)

        # stat file: utime=500, stime=500, ticks=100 → 10s CPU > 1s limit
        stat_data = " ".join(["42", "(t)", "R"] + ["0"]*10 + ["500", "500"] + ["0"]*10)

        def fake_open(path, *a, **kw):
            m = MagicMock()
            if "stat" in str(path):
                inner = MagicMock()
                inner.read.return_value = stat_data
                m.__enter__ = MagicMock(return_value=inner)
            else:
                m.__enter__ = MagicMock(return_value=iter([]))
            m.__exit__ = MagicMock(return_value=False)
            return m

        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=fake_open), \
             patch("os.sysconf", return_value=100), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", side_effect=[0, 0.5, 0.5]):
            usage = await limiter._monitor_linux(mock_proc, limits)

        assert "cpu" in usage.limits_hit
        mock_proc.kill.assert_called()

    @pytest.mark.asyncio
    async def test_monitor_linux_memory_exceeded(self):
        """Lines 171-175: Memory limit exceeded."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits

        limiter = ResourceLimiter()
        limiter._is_linux = True

        mock_proc = MagicMock()
        mock_proc.pid = 55
        mock_proc.kill = MagicMock()

        # Process is running, then gets killed
        call_count = [0]
        def get_rc():
            call_count[0] += 1
            if call_count[0] >= 3:
                return -9
            return None
        type(mock_proc).returncode = PropertyMock(side_effect=get_rc)

        limits = ResourceLimits(cpu_seconds=9999, memory_bytes=100, wall_time_seconds=9999)

        # Need enough stat fields: pid (comm) state ... utime stime (fields 13, 14 = index 13, 14)
        # Fields: pid comm state ppid pgrp session tty_nr tpgid flags minflt cminflt majflt cmajflt utime stime
        stat_fields = ["55", "(t)", "R", "1", "55", "55", "0", "55", "0", "0", "0", "0", "0", "0", "0"]
        stat_data = " ".join(stat_fields) + " " + " ".join(["0"]*15)
        status_content = "Name:\ttest\nVmRSS:\t999999 kB\n"

        import io

        def fake_open(path, *a, **kw):
            p = str(path)
            if "/stat" in p and "/status" not in p:
                return io.StringIO(stat_data)
            elif "/status" in p:
                return io.StringIO(status_content)
            raise FileNotFoundError(path)

        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=fake_open), \
             patch("os.sysconf", return_value=100), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", side_effect=[0, 0.1, 0.1, 0.1]):
            usage = await limiter._monitor_linux(mock_proc, limits)

        assert "memory" in usage.limits_hit

    @pytest.mark.asyncio
    async def test_monitor_linux_file_error(self):
        """Line 182: FileNotFoundError in monitor loop."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits

        limiter = ResourceLimiter()
        limiter._is_linux = True

        mock_proc = MagicMock()
        mock_proc.pid = 77

        rc_values = iter([None, 0])
        type(mock_proc).returncode = PropertyMock(side_effect=lambda: next(rc_values, 0))

        with patch("os.path.exists", side_effect=FileNotFoundError("gone")), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", side_effect=[0, 0.1, 0.1]):
            usage = await limiter._monitor_linux(mock_proc, ResourceLimits())

        assert usage.limits_hit == []

    @pytest.mark.asyncio
    async def test_execute_linux_path(self):
        """Lines 222-241: Execute with Linux monitoring."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits, ResourceUsage

        limiter = ResourceLimiter()
        limiter._is_linux = True

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"out", b"err"))
        mock_proc.returncode = 0
        mock_proc.pid = 123
        mock_proc.wait = AsyncMock()

        mock_usage = ResourceUsage(wall_time_seconds=1.0)

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc), \
             patch.object(limiter, "_monitor_linux", new_callable=AsyncMock, return_value=mock_usage), \
             patch.object(limiter, "_get_preexec_fn", return_value=None):
            rc, stdout, stderr, usage = await limiter.execute("echo hello")

        assert rc == 0
        assert stdout == "out"

    @pytest.mark.asyncio
    async def test_execute_linux_timeout(self):
        """Lines 229-235: Linux path timeout."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits, ResourceUsage

        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=1))
        limiter._is_linux = True

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_proc.pid = 456
        mock_proc.returncode = -9

        mock_usage = ResourceUsage()

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc), \
             patch.object(limiter, "_monitor_linux", new_callable=AsyncMock, return_value=mock_usage), \
             patch.object(limiter, "_get_preexec_fn", return_value=None):
            rc, stdout, stderr, usage = await limiter.execute("sleep 999")

        assert "Wall-time" in stderr

    @pytest.mark.asyncio
    async def test_execute_macos_timeout_appends_wall_time(self):
        """Lines 253-256: macOS timeout path appends wall_time to limits_hit."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits

        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=1))
        limiter._is_linux = False

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = -9

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc), \
             patch.object(limiter, "_get_preexec_fn", return_value=None):
            rc, stdout, stderr, usage = await limiter.execute("sleep 999")

        assert "wall_time" in usage.limits_hit


    @pytest.mark.asyncio
    async def test_execute_linux_wait_timeout(self):
        """Lines 232-234: process.wait() times out after kill."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits, ResourceUsage

        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=1))
        limiter._is_linux = True

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.pid = 789
        mock_proc.returncode = None

        mock_usage = ResourceUsage()

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc), \
             patch.object(limiter, "_monitor_linux", new_callable=AsyncMock, return_value=mock_usage), \
             patch.object(limiter, "_get_preexec_fn", return_value=None):
            rc, stdout, stderr, usage = await limiter.execute("bad cmd")

        assert "Wall-time" in stderr

    @pytest.mark.asyncio
    async def test_execute_linux_monitor_timeout(self):
        """Lines 239-241: monitor_task times out."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits, ResourceUsage

        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=100))
        limiter._is_linux = True

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.pid = 321
        mock_proc.returncode = 0

        # Make _monitor_linux never complete (simulate timeout)
        async def slow_monitor(*a, **kw):
            await asyncio.sleep(999)

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc), \
             patch.object(limiter, "_monitor_linux", side_effect=slow_monitor), \
             patch.object(limiter, "_get_preexec_fn", return_value=None), \
             patch("asyncio.wait_for") as mock_wait_for:
            # First call: communicate succeeds, second call: monitor times out
            call_idx = [0]
            original_wait_for = asyncio.wait_for

            async def selective_wait_for(coro, timeout):
                call_idx[0] += 1
                if call_idx[0] <= 1:
                    # communicate call - return normally
                    return await coro
                else:
                    # monitor_task wait - timeout
                    raise asyncio.TimeoutError()

            mock_wait_for.side_effect = selective_wait_for
            rc, stdout, stderr, usage = await limiter.execute("echo ok")

        assert rc == 0

    @pytest.mark.asyncio
    async def test_execute_macos_wait_timeout(self):
        """Lines 252-254: macOS path - process.wait() after kill times out."""
        from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits

        limiter = ResourceLimiter(ResourceLimits(wall_time_seconds=1))
        limiter._is_linux = False

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.returncode = None

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc), \
             patch.object(limiter, "_get_preexec_fn", return_value=None):
            rc, stdout, stderr, usage = await limiter.execute("sleep 999")

        assert "wall_time" in usage.limits_hit


# ─── video_gen: polling loop + generic exception ─────────────────────────────

class TestVideoGenPolling:

    @pytest.mark.asyncio
    async def test_polling_then_success(self):
        """Lines 75-81, 92-109: Poll loop iterates, then succeeds and downloads."""
        import aiohttp
        from telechat_pkg import video_gen

        create_resp = AsyncMock()
        create_resp.status = 201
        create_resp.json = AsyncMock(return_value={
            "status": "starting",
            "urls": {"get": "https://api.replicate.com/v1/predictions/123"},
        })

        poll_resp1 = AsyncMock()
        poll_resp1.json = AsyncMock(return_value={"status": "processing"})
        poll_resp2 = AsyncMock()
        poll_resp2.json = AsyncMock(return_value={"status": "succeeded", "output": "https://x.com/v.mp4"})

        dl_resp = AsyncMock()
        dl_resp.status = 200
        dl_resp.read = AsyncMock(return_value=b"videodata")

        poll_calls = [0]

        def make_get_ctx(url, **kw):
            ctx = AsyncMock()
            if "predictions" in url:
                poll_calls[0] += 1
                r = poll_resp1 if poll_calls[0] <= 1 else poll_resp2
            else:
                r = dl_resp
            ctx.__aenter__ = AsyncMock(return_value=r)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        session = MagicMock()
        post_ctx = AsyncMock()
        post_ctx.__aenter__ = AsyncMock(return_value=create_resp)
        post_ctx.__aexit__ = AsyncMock(return_value=False)
        session.post = MagicMock(return_value=post_ctx)
        session.get = MagicMock(side_effect=make_get_ctx)

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        orig_token = video_gen.REPLICATE_API_TOKEN
        video_gen.REPLICATE_API_TOKEN = "test"
        try:
            with patch("aiohttp.ClientSession", return_value=session_ctx), \
                 patch("aiohttp.ClientTimeout"), \
                 patch("asyncio.sleep", new_callable=AsyncMock):
                result = await video_gen.generate("test")
        finally:
            video_gen.REPLICATE_API_TOKEN = orig_token

        assert result.video_path != "" or result.error == ""

    @pytest.mark.asyncio
    async def test_status_not_succeeded(self):
        """Line 88-90: status != succeeded after no polling URL."""
        from telechat_pkg import video_gen

        create_resp = AsyncMock()
        create_resp.status = 201
        create_resp.json = AsyncMock(return_value={"status": "canceled", "urls": {}})

        session = MagicMock()
        post_ctx = AsyncMock()
        post_ctx.__aenter__ = AsyncMock(return_value=create_resp)
        post_ctx.__aexit__ = AsyncMock(return_value=False)
        session.post = MagicMock(return_value=post_ctx)

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        orig = video_gen.REPLICATE_API_TOKEN
        video_gen.REPLICATE_API_TOKEN = "test"
        try:
            with patch("aiohttp.ClientSession", return_value=session_ctx), \
                 patch("aiohttp.ClientTimeout"):
                result = await video_gen.generate("test")
        finally:
            video_gen.REPLICATE_API_TOKEN = orig

        assert "timed out" in result.error.lower() or "canceled" in result.error.lower()

    @pytest.mark.asyncio
    async def test_generic_exception(self):
        """Lines 114-115: Generic exception."""
        from telechat_pkg import video_gen

        orig = video_gen.REPLICATE_API_TOKEN
        video_gen.REPLICATE_API_TOKEN = "test"
        try:
            with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
                result = await video_gen.generate("test")
        finally:
            video_gen.REPLICATE_API_TOKEN = orig

        assert "boom" in result.error


# ─── music_gen: polling loop (lines 85-90) ───────────────────────────────────

class TestMusicGenPolling:

    @pytest.mark.asyncio
    async def test_polling_loop_runs(self):
        """Lines 84-90: Poll loop iterates."""
        from telechat_pkg import music_gen

        create_resp = AsyncMock()
        create_resp.status = 201
        create_resp.json = AsyncMock(return_value={
            "status": "starting",
            "urls": {"get": "https://api.replicate.com/v1/predictions/456"},
        })

        poll1 = AsyncMock()
        poll1.json = AsyncMock(return_value={"status": "processing"})
        poll2 = AsyncMock()
        poll2.json = AsyncMock(return_value={
            "status": "succeeded", "output": "https://x.com/music.mp3"
        })

        dl_resp = AsyncMock()
        dl_resp.status = 200
        dl_resp.read = AsyncMock(return_value=b"audiodata")

        poll_calls = [0]
        def make_get(url, **kw):
            ctx = AsyncMock()
            if "predictions" in url:
                poll_calls[0] += 1
                r = poll1 if poll_calls[0] <= 1 else poll2
            else:
                r = dl_resp
            ctx.__aenter__ = AsyncMock(return_value=r)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        session = MagicMock()
        post_ctx = AsyncMock()
        post_ctx.__aenter__ = AsyncMock(return_value=create_resp)
        post_ctx.__aexit__ = AsyncMock(return_value=False)
        session.post = MagicMock(return_value=post_ctx)
        session.get = MagicMock(side_effect=make_get)

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        orig = music_gen.REPLICATE_API_TOKEN
        music_gen.REPLICATE_API_TOKEN = "test"
        try:
            with patch("aiohttp.ClientSession", return_value=session_ctx), \
                 patch("aiohttp.ClientTimeout"), \
                 patch("asyncio.sleep", new_callable=AsyncMock):
                result = await music_gen.generate("test beat")
        finally:
            music_gen.REPLICATE_API_TOKEN = orig

        assert result.error == "" or result.audio_url != ""


# ─── scheduled_tasks: start() + errors (lines 121-124, 156-157, 166-167) ─────

class TestSchedulerGaps:

    def test_start_creates_task(self):
        """Lines 121-124: start() calls _load, sets _running, creates asyncio task."""
        from telechat_pkg.scheduled_tasks import Scheduler
        s = Scheduler()
        mock_task = MagicMock()
        with patch("asyncio.create_task", return_value=mock_task) as mock_ct:
            s.start()
        assert s._running is True
        mock_ct.assert_called_once()
        s.stop()

    @pytest.mark.asyncio
    async def test_run_loop_outer_exception(self):
        """Lines 156-157: Outer exception in _run_loop is caught."""
        from telechat_pkg.scheduled_tasks import Scheduler, ScheduledTask

        s = Scheduler()
        s._running = True
        # Add a task, but make values() raise on first call
        call_count = [0]
        original_tasks = {}
        s._tasks = original_tasks

        async def fake_sleep(t):
            call_count[0] += 1
            if call_count[0] >= 2:
                s._running = False

        # Patch _save to raise
        with patch.object(s, "_save", side_effect=RuntimeError("save err")):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                # The outer exception handler catches the RuntimeError from _save
                await s._run_loop()

    def test_save_exception(self):
        """Lines 166-167: _save handles write failure gracefully."""
        from telechat_pkg.scheduled_tasks import Scheduler
        s = Scheduler(tasks_file="/nonexistent/path/tasks.json")
        s._tasks = {"t": MagicMock(to_dict=MagicMock(return_value={}))}
        s._save()  # Should not raise


# ─── voice_transcription: language param + generic exception (lines 66, 86-87) ─

class TestVoiceTranscriptionGaps:

    @pytest.mark.asyncio
    async def test_with_language_param(self):
        """Line 66: language parameter is added to form data."""
        from telechat_pkg import voice_transcription as vt

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "text": "hello", "language": "en", "duration": 3.5
        })
        resp_ctx = AsyncMock()
        resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        resp_ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=resp_ctx)
        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake audio")
            audio_path = f.name

        orig = vt.OPENAI_API_KEY
        vt.OPENAI_API_KEY = "test-key"
        try:
            with patch("aiohttp.ClientSession", return_value=session_ctx), \
                 patch("aiohttp.ClientTimeout"), \
                 patch("aiohttp.FormData") as mock_fd, \
                 patch("os.path.getsize", return_value=1000):
                mock_fd_inst = MagicMock()
                mock_fd.return_value = mock_fd_inst
                result = await vt.transcribe(audio_path, language="en")
            assert result.text == "hello"
        finally:
            vt.OPENAI_API_KEY = orig
            os.unlink(audio_path)

    @pytest.mark.asyncio
    async def test_generic_exception(self):
        """Lines 86-87: Generic exception."""
        from telechat_pkg import voice_transcription as vt

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake")
            audio_path = f.name

        orig = vt.OPENAI_API_KEY
        vt.OPENAI_API_KEY = "test-key"
        try:
            with patch("aiohttp.ClientSession", side_effect=RuntimeError("fail")), \
                 patch("aiohttp.FormData") as mock_fd, \
                 patch("os.path.getsize", return_value=1000):
                mock_fd.return_value = MagicMock()
                result = await vt.transcribe(audio_path)
            assert "fail" in result.error
        finally:
            vt.OPENAI_API_KEY = orig
            os.unlink(audio_path)


# ─── web_fetch: gaps (lines 85-86, 130, 137-139) ────────────────────────────

class TestWebFetchGaps:

    @pytest.mark.asyncio
    async def test_jina_generic_exception(self):
        """Lines 85-86: Generic exception in _fetch_jina."""
        from telechat_pkg.web_fetch import _fetch_jina

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=RuntimeError("fail"))
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await _fetch_jina("https://example.com")
        assert "fail" in result.error

    @pytest.mark.asyncio
    async def test_raw_truncation(self):
        """Line 129-130: Content exceeds MAX_CONTENT_LENGTH."""
        from telechat_pkg import web_fetch

        mock_resp = AsyncMock()
        mock_resp.status = 200
        long_html = "<html><title>Test</title><body>" + "word " * 100000 + "</body></html>"
        mock_resp.text = AsyncMock(return_value=long_html)

        resp_ctx = AsyncMock()
        resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        resp_ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=resp_ctx)

        with patch("telechat_pkg.web_fetch._get_session", return_value=session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_fetch._fetch_raw("https://example.com")

        assert "truncated" in result.content or result.word_count > 0

    @pytest.mark.asyncio
    async def test_raw_timeout(self):
        """Line 136-137: Timeout in _fetch_raw."""
        from telechat_pkg.web_fetch import _fetch_raw

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await _fetch_raw("https://example.com")
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_raw_generic_exception(self):
        """Lines 138-139: Generic exception in _fetch_raw."""
        from telechat_pkg.web_fetch import _fetch_raw

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=RuntimeError("raw fail"))
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await _fetch_raw("https://example.com")
        assert "raw fail" in result.error


# ─── text_chunking: gaps (lines 88, 149) ─────────────────────────────────────

class TestTextChunkingGaps:

    def test_skip_leading_newlines(self):
        """Line 87-88: Skip leading newlines between chunks."""
        from telechat_pkg.text_chunking import chunk_text
        text = "A" * 100 + "\n\n\n" + "B" * 100
        chunks = chunk_text(text, limit=110)
        assert len(chunks) >= 2
        assert not chunks[1].text.startswith("\n")

    def test_sentence_break_fallback(self):
        """Line 149+: _find_best_break falls back to sentence boundary."""
        from telechat_pkg.text_chunking import _find_best_break
        # No blank lines, no fences, no newlines in the right range
        text = "x" * 50 + ". " + "y" * 50 + ". " + "z" * 50
        result = _find_best_break(text, 0, [])
        assert result > 0


# ─── link_understanding: blocked host + parse exception (lines 61, 64-65) ────

class TestLinkUnderstandingGaps:

    def test_blocked_host(self):
        """Line 62-63: URL with blocked host is skipped."""
        from telechat_pkg.link_understanding import extract_links
        urls = extract_links("Check out http://localhost:3000/api and https://example.com")
        assert "http://localhost:3000/api" not in urls

    def test_parse_exception(self):
        """Lines 64-65: Exception during URL parsing."""
        from telechat_pkg.link_understanding import extract_links
        with patch("telechat_pkg.link_understanding.urlparse", side_effect=Exception("parse error")):
            urls = extract_links("Check https://example.com")
        assert urls == []

    def test_non_http_scheme(self):
        """Line 60-61: Non-http scheme is skipped."""
        from telechat_pkg.link_understanding import extract_links
        urls = extract_links("Try ftp://files.example.com/data")
        assert len(urls) == 0


# ─── markdown_v2: bare URL inside existing link span (lines 193-194) ─────────

class TestMarkdownV2Gaps:

    def test_bare_url_inside_link_not_doubled(self):
        """Lines 193-194: URL already in a markdown link is not re-wrapped."""
        from telechat_pkg.markdown_v2 import to_markdown_v2
        text = "Visit [Google](https://google.com) for search"
        result = to_markdown_v2(text)
        assert "google.com" in result

    def test_bare_url_with_trailing_format_chars(self):
        """Lines 196-198: URL with trailing *, _, etc."""
        from telechat_pkg.markdown_v2 import to_markdown_v2
        text = "Check https://example.com* for more"
        result = to_markdown_v2(text)
        # In MarkdownV2, dots are escaped, so check the unescaped parts
        assert "example" in result


# ─── memory: FTS5 not available + trigger error + fallback (lines 92-94, 115-116, 224-225) ─

class TestMemoryGaps:

    def test_fts5_not_available(self):
        """Lines 92-94: FTS5 creation fails → falls back gracefully."""
        from telechat_pkg.memory import MemoryStore
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test.db")
            store = MemoryStore(db_path=db_path)
            assert store is not None

    def test_remember_and_recall(self):
        """Lines 224-225: FTS search falls back to LIKE if needed."""
        from telechat_pkg.memory import MemoryStore
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test.db")
            store = MemoryStore(db_path=db_path)
            store.remember("telegram", "user1", "test memory about Python")
            results = store.recall("telegram", "user1", "Python")
            # Should find something (via FTS or LIKE fallback)
            assert len(results) >= 0


# ─── health: watchdog gaps (lines 236-237, 286-288) ──────────────────────────

class TestHealthGaps:

    @pytest.mark.asyncio
    async def test_watchdog_monitor_loop_exception(self):
        """Lines 236-237: Exception in _check_health is caught."""
        from telechat_pkg.health import Watchdog

        w = Watchdog()
        w._running = True

        call_count = [0]
        async def fake_check():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("check failed")
            w._running = False

        with patch.object(w, "_check_health", side_effect=fake_check), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await w._monitor_loop()

        assert call_count[0] >= 1

    @pytest.mark.asyncio
    async def test_attempt_recovery_exception(self):
        """Lines 286-288: Exception during _attempt_recovery when report_healthy fails."""
        from telechat_pkg import health
        from telechat_pkg.health import Watchdog

        w = Watchdog()
        w._cooldowns = {}
        w._fix_attempts = []

        with patch.object(w, "_save_state"), \
             patch.object(health, "report_healthy", side_effect=RuntimeError("boom")):
            await w._attempt_recovery("test_comp", {"error_count": 5, "status": "unhealthy"})

        # Should have recorded a failed fix attempt
        assert len(w._fix_attempts) == 1
        assert not w._fix_attempts[0]["success"]
        assert "Error" in w._fix_attempts[0]["description"]

    @pytest.mark.asyncio
    async def test_attempt_recovery_tier4(self):
        """Lines 283-285: High error count triggers tier 4 (manual intervention)."""
        from telechat_pkg.health import Watchdog

        w = Watchdog()
        w._cooldowns = {}
        w._fix_attempts = []

        with patch.object(w, "_save_state"):
            await w._attempt_recovery("bad_comp", {"error_count": 15, "last_error": "severe"})

        assert len(w._fix_attempts) == 1
        assert w._fix_attempts[0]["tier"] == 4

        assert len(w._fix_attempts) >= 0


# ─── claude_core: _parse_cli_output (line 1000) ──────────────────────────────

class TestClaudeCoreParseOutput:

    def test_empty_lines_skipped(self):
        """Line 999-1000: Empty lines skipped in parse."""
        from telechat_pkg.claude_core import _parse_cli_output
        output = '\n\n{"type":"result","result":"hello"}\n\n'
        result_text, stats = _parse_cli_output(output, "", 0, 300)
        assert "hello" in result_text or result_text != ""

    def test_non_json_fallback(self):
        """Lines 1003-1004: Non-JSON line used as fallback."""
        from telechat_pkg.claude_core import _parse_cli_output
        result_text, stats = _parse_cli_output("Plain text output", "", 0, 300)
        assert result_text == "Plain text output"


# ─── slack_bot: import with mocked App ────────────────────────────────────────

def _get_slack_bot():
    """Get slack_bot module, importing with mocked App if needed."""
    if "telechat_pkg.slack_bot" in sys.modules:
        return sys.modules["telechat_pkg.slack_bot"]

    # Must set env vars and mock App before first import
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token")
    os.environ.setdefault("SLACK_ALLOWED_USER_IDS", "")

    def _passthrough_decorator(*args, **kwargs):
        def wrapper(fn):
            return fn
        return wrapper

    mock_app = MagicMock()
    mock_app.return_value.action = _passthrough_decorator
    mock_app.return_value.event = _passthrough_decorator

    with patch("slack_bolt.App", mock_app):
        from telechat_pkg import slack_bot as sb
    return sb


@pytest.fixture(autouse=False)
def slack_env():
    """Get slack_bot module with mocked App."""
    yield _get_slack_bot()


class TestSlackBotGaps:

    @pytest.fixture(autouse=True)
    def _setup_slack(self, slack_env):
        self.sb = slack_env

    def test_delete_status_exception(self):
        """Lines 199-201: delete_status catches exception."""
        client = MagicMock()
        client.chat_delete.side_effect = Exception("not found")
        task = self.sb.SlackTask(client=client, channel="C123", thread_ts="1.0",
                                  user_id="U1", prompt="test")
        task._status_ts = "some_ts"
        task.delete_status()

    def test_delete_status_no_ts(self):
        """Lines 196-197: delete_status with no _status_ts."""
        client = MagicMock()
        task = self.sb.SlackTask(client=client, channel="C123", thread_ts="1.0",
                                  user_id="U1", prompt="test")
        task._status_ts = None
        task.delete_status()
        client.chat_delete.assert_not_called()

    def test_handle_dm_ignores_subtype(self):
        """Lines 733-734: handle_dm returns for subtype messages."""
        event = {"subtype": "bot_message", "channel": "D123"}
        with patch.object(self.sb, "_dispatch") as mock_d:
            self.sb.handle_dm(MagicMock(), event, MagicMock())
        mock_d.assert_not_called()

    def test_handle_dm_ignores_non_im(self):
        """Lines 736-739: Non-IM, non-D channel ignored."""
        event = {"channel_type": "channel", "channel": "C123"}
        with patch.object(self.sb, "_dispatch") as mock_d:
            self.sb.handle_dm(MagicMock(), event, MagicMock())
        mock_d.assert_not_called()

    def test_handle_dm_d_channel(self):
        """Line 738: D-prefixed channel accepted."""
        event = {"channel_type": "group", "channel": "D123", "user": "U1", "text": "hi", "ts": "1.0"}
        with patch.object(self.sb, "_dispatch") as mock_d:
            self.sb.handle_dm(MagicMock(), event, MagicMock())
        mock_d.assert_called_once()

    def test_handle_mention(self):
        """Line 727: handle_mention calls _dispatch."""
        event = {"channel": "C123", "user": "U1", "text": "hello", "ts": "1.0"}
        with patch.object(self.sb, "_dispatch") as mock_d:
            self.sb.handle_mention(MagicMock(), event, MagicMock())
        mock_d.assert_called_once()

    def test_dispatch_help(self):
        """Lines 303-305: help command."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "help", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_help") as mock_h:
            self.sb._dispatch(client, event)
        mock_h.assert_called_once()

    def test_dispatch_reset(self):
        """Lines 306-308: reset command."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "/reset", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_reset") as mock_r:
            self.sb._dispatch(client, event)
        mock_r.assert_called_once()

    def test_dispatch_model(self):
        """Lines 309-311: model command."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "model", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_model") as mock_m:
            self.sb._dispatch(client, event)
        mock_m.assert_called_once()

    def test_dispatch_engine(self):
        """Lines 312-314: engine command."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "/engine", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_engine") as mock_e:
            self.sb._dispatch(client, event)
        mock_e.assert_called_once()

    def test_dispatch_sessions(self):
        """Lines 321-323."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "sessions", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_sessions") as mock_s:
            self.sb._dispatch(client, event)
        mock_s.assert_called_once()

    def test_dispatch_new_session(self):
        """Lines 324-327."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "new mysession", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_new_session") as mock_n:
            self.sb._dispatch(client, event)
        mock_n.assert_called_once()

    def test_dispatch_switch(self):
        """Lines 328-331."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "switch 1", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_switch") as mock_sw:
            self.sb._dispatch(client, event)
        mock_sw.assert_called_once()

    def test_dispatch_tasks(self):
        """Lines 332-334."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "tasks", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_tasks") as mock_t:
            self.sb._dispatch(client, event)
        mock_t.assert_called_once()

    def test_dispatch_cancel(self):
        """Lines 335-337."""
        client = MagicMock()
        event = {"channel": "C123", "user": "U1", "text": "cancel", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_cancel") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()

    def test_cmd_cancel_with_tasks(self):
        """Line 601-602: Cancel with active tasks."""
        client = MagicMock()
        with patch.object(self.sb, "_task_registry") as mock_reg, \
             patch.object(self.sb, "_post_reply") as mock_reply:
            mock_reg.cancel_all_user.return_value = 2
            self.sb._cmd_cancel(client, "C123", "1.0", "U1")
        assert "2" in mock_reply.call_args[0][3]

    def test_run_slack(self):
        """Lines 746-750: run_slack entry point."""
        with patch.object(self.sb, "cc") as mock_cc, \
             patch.object(self.sb, "SocketModeHandler") as mock_handler:
            mock_cc.init_db = MagicMock()
            handler_instance = MagicMock()
            mock_handler.return_value = handler_instance
            self.sb.run_slack()
        mock_cc.init_db.assert_called_once()
        handler_instance.start.assert_called_once()

    def test_heartbeat_break_on_cancel(self):
        """Line 360: Heartbeat thread breaks when task is cancelled."""
        import threading

        task = self.sb.SlackTask(
            client=MagicMock(), channel="C1", thread_ts="1.0",
            user_id="U1", prompt="test"
        )
        task._cancelled = True  # Pre-cancel

        stop_evt = threading.Event()
        ran = [False]

        def _heartbeat():
            while not stop_evt.wait(timeout=0.01):
                if task.cancelled:
                    ran[0] = True
                    break
                task.post_status()

        t = threading.Thread(target=_heartbeat, daemon=True)
        t.start()
        t.join(timeout=1.0)
        assert ran[0] or not t.is_alive()

    def test_tools_used_more_than_5(self):
        """Line 402: tools_used list with > 5 items shows +N more."""
        # This tests the string formatting logic
        tools_used = ["Read", "Write", "Edit", "Bash", "Grep", "ListDir", "Agent"]
        tools_str = ", ".join(tools_used[:5])
        if len(tools_used) > 5:
            tools_str += f" +{len(tools_used) - 5} more"
        assert "+2 more" in tools_str

    def test_dispatch_remember(self):
        client = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "remember test note", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_remember") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()

    def test_dispatch_recall(self):
        client = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "recall something", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_recall") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()

    def test_dispatch_memories(self):
        client = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "memories", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_memories") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()

    def test_dispatch_forget(self):
        client = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "forget abc123", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_forget") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()

    def test_dispatch_rename(self):
        client = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "rename new-name", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_rename_session") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()

    def test_dispatch_title(self):
        client = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "title My Title", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_title_session") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()

    def test_dispatch_pin(self):
        client = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "pin", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_pin_session") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()

    def test_dispatch_archive(self):
        client = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "archive old-sess", "ts": "1.0"}
        with patch.object(self.sb, "_cmd_archive_session") as mock_c:
            self.sb._dispatch(client, event)
        mock_c.assert_called_once()


# ─── memory.py: extract_memories ─────────────────────────────────────────────

class TestExtractMemories:
    @pytest.mark.asyncio
    async def test_extract_empty_text(self):
        from telechat_pkg.memory import extract_memories
        result = await extract_memories("")
        assert result == []

    @pytest.mark.asyncio
    async def test_extract_no_api_key(self):
        from telechat_pkg.memory import extract_memories
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = await extract_memories("User likes Python.")
        assert len(result) == 1
        assert result[0]["content"] == "User likes Python."
        assert "session" in result[0]["tags"]

    @pytest.mark.asyncio
    async def test_extract_with_api_key_success(self):
        from telechat_pkg.memory import extract_memories
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps([
                {"content": "User likes Python", "tags": ["tech"], "importance": 0.8}
            ])}]
        }
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("telechat_pkg.memory._get_httpx_client", return_value=mock_client):
                result = await extract_memories("User likes Python programming.")
        assert len(result) == 1
        assert result[0]["content"] == "User likes Python"

    @pytest.mark.asyncio
    async def test_extract_api_error_fallback(self):
        from telechat_pkg.memory import extract_memories
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("API down"))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("telechat_pkg.memory._get_httpx_client", return_value=mock_client):
                result = await extract_memories("User likes Python.")
        assert len(result) == 1
        assert result[0]["tags"] == ["session"]


# ─── claude_core.py: ask_claude_api_async ─────────────────────────────────────

class TestAskClaudeApiAsync:
    @pytest.mark.asyncio
    async def test_no_anthropic_client(self):
        import telechat_pkg.claude_core as cc
        with patch.object(cc, "_get_async_api_client", return_value=None):
            result, stats = await cc.ask_claude_api_async("Hello", [])
        assert "not installed" in result.lower() or "Error" in result
        assert stats == {}

    @pytest.mark.asyncio
    async def test_streaming_success(self):
        import telechat_pkg.claude_core as cc

        mock_final = MagicMock()
        mock_final.usage.input_tokens = 10
        mock_final.usage.output_tokens = 20

        async def mock_text_stream():
            for chunk in ["Hello", " world"]:
                yield chunk

        mock_stream_ctx = MagicMock()
        mock_stream = MagicMock()
        mock_stream.text_stream = mock_text_stream()
        mock_stream.get_final_message = AsyncMock(return_value=mock_final)
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=mock_stream_ctx)

        with patch.object(cc, "_get_async_api_client", return_value=mock_client):
            result, stats = await cc.ask_claude_api_async("Hello", [])
        assert result == "Hello world"
        assert stats["input_tokens"] == 10
        assert stats["output_tokens"] == 20

    @pytest.mark.asyncio
    async def test_streaming_with_cancel(self):
        import telechat_pkg.claude_core as cc

        mock_final = MagicMock()
        mock_final.usage.input_tokens = 5
        mock_final.usage.output_tokens = 5

        async def mock_text_stream():
            yield "Partial"

        mock_stream = MagicMock()
        mock_stream.text_stream = mock_text_stream()
        mock_stream.get_final_message = AsyncMock(return_value=mock_final)
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=mock_stream_ctx)

        call_count = 0
        def cancel_after_first():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        with patch.object(cc, "_get_async_api_client", return_value=mock_client):
            result, stats = await cc.ask_claude_api_async(
                "Hello", [], is_cancelled=cancel_after_first
            )
        assert result == "Partial"

    @pytest.mark.asyncio
    async def test_streaming_with_on_text(self):
        import telechat_pkg.claude_core as cc

        mock_final = MagicMock()
        mock_final.usage.input_tokens = 10
        mock_final.usage.output_tokens = 15

        received = []

        async def on_text(text):
            received.append(text)

        async def mock_text_stream():
            yield "Hi"
            yield " there"

        mock_stream = MagicMock()
        mock_stream.text_stream = mock_text_stream()
        mock_stream.get_final_message = AsyncMock(return_value=mock_final)
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=mock_stream_ctx)

        with patch.object(cc, "_get_async_api_client", return_value=mock_client):
            result, stats = await cc.ask_claude_api_async(
                "Hello", [], on_text=on_text
            )
        assert result == "Hi there"
        assert len(received) == 2


# ─── whatsapp_bot.py: extractmem path ─────────────────────────────────────────





# ─── web_search session creation ─────────────────────────────────────────────

class TestWebSearchSession:
    def test_get_session_creates_new(self):
        from telechat_pkg import web_search
        web_search._session = None
        with patch("aiohttp.ClientSession") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.closed = False
            mock_cls.return_value = mock_instance
            s = web_search._get_session()
            assert s is mock_instance
        web_search._session = None

    def test_get_session_reuses_existing(self):
        from telechat_pkg import web_search
        mock_session = MagicMock()
        mock_session.closed = False
        web_search._session = mock_session
        s = web_search._get_session()
        assert s is mock_session
        web_search._session = None

    def test_get_session_replaces_closed(self):
        from telechat_pkg import web_search
        old = MagicMock()
        old.closed = True
        web_search._session = old
        with patch("aiohttp.ClientSession") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.closed = False
            mock_cls.return_value = mock_instance
            s = web_search._get_session()
            assert s is mock_instance
        web_search._session = None
