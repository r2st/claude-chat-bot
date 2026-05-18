"""Tests for polls, image_gen, tts, web_search, and link_understanding modules."""
from __future__ import annotations

import asyncio
import base64
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
# polls.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import polls


class TestParsePollCommand:
    def test_empty_text_returns_usage(self):
        result = polls.parse_poll_command("")
        assert isinstance(result, str)
        assert "Usage" in result

    def test_pipe_format_success(self):
        result = polls.parse_poll_command("Fav color? | Red | Blue | Green")
        assert isinstance(result, polls.PollInput)
        assert result.question == "Fav color?"
        assert result.options == ["Red", "Blue", "Green"]

    def test_newline_format_success(self):
        result = polls.parse_poll_command("Best language?\nPython\nRust\nGo")
        assert isinstance(result, polls.PollInput)
        assert result.question == "Best language?"
        assert result.options == ["Python", "Rust", "Go"]

    def test_newline_too_few_lines(self):
        result = polls.parse_poll_command("Question?\nOnly one")
        assert isinstance(result, str)
        assert "at least" in result.lower()

    def test_multi_flag(self):
        result = polls.parse_poll_command("--multi Fav? | A | B | C")
        assert isinstance(result, polls.PollInput)
        assert result.allows_multiple_answers is True

    def test_public_flag(self):
        result = polls.parse_poll_command("--public Fav? | A | B")
        assert isinstance(result, polls.PollInput)
        assert result.is_anonymous is False

    def test_too_few_options(self):
        result = polls.parse_poll_command("Q? | OnlyOne")
        assert isinstance(result, str)
        assert "at least" in result.lower()

    def test_too_many_options(self):
        opts = " | ".join([f"Opt{i}" for i in range(15)])
        result = polls.parse_poll_command(f"Q? | {opts}")
        assert isinstance(result, str)
        assert "Maximum" in result or "maximum" in result

    def test_no_pipes_no_newlines_returns_usage(self):
        result = polls.parse_poll_command("just some text")
        assert isinstance(result, str)
        assert "Usage" in result

    def test_empty_question(self):
        result = polls.parse_poll_command("| A | B")
        assert isinstance(result, str)
        assert "required" in result.lower()

    def test_options_truncated_to_100(self):
        long_opt = "X" * 200
        result = polls.parse_poll_command(f"Q? | {long_opt} | Short")
        assert isinstance(result, polls.PollInput)
        assert len(result.options[0]) == 100

    def test_question_truncated_to_300(self):
        long_q = "Q" * 400
        result = polls.parse_poll_command(f"{long_q} | A | B")
        assert isinstance(result, polls.PollInput)
        assert len(result.question) == 300


class TestExtractPollFromResponse:
    def test_valid_poll_block(self):
        text = "[POLL]\nQ: Fav color?\n- Red\n- Blue\n- Green\n"
        result = polls.extract_poll_from_response(text)
        assert result is not None
        assert result.question == "Fav color?"
        assert len(result.options) == 3

    def test_no_match(self):
        assert polls.extract_poll_from_response("no poll here") is None

    def test_too_few_options(self):
        text = "[POLL]\nQ: Only one?\n- Solo\n"
        assert polls.extract_poll_from_response(text) is None

    def test_options_capped_at_max(self):
        opts = "\n".join([f"- Opt{i}" for i in range(15)])
        text = f"[POLL]\nQ: Many?\n{opts}\n"
        result = polls.extract_poll_from_response(text)
        assert result is not None
        assert len(result.options) <= polls.MAX_OPTIONS

    def test_bullet_options(self):
        text = "[POLL]\nQ: Test?\n• Alpha\n• Beta\n"
        result = polls.extract_poll_from_response(text)
        assert result is not None
        assert "Alpha" in result.options


# ═══════════════════════════════════════════════════════════════════════════════
# image_gen.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import image_gen


class TestImageGenAvailable:
    def test_not_available_when_disabled(self, monkeypatch):
        monkeypatch.setattr(image_gen, "IMAGE_GEN_ENABLED", False)
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "key")
        assert image_gen.is_available() is False

    def test_not_available_when_no_key(self, monkeypatch):
        monkeypatch.setattr(image_gen, "IMAGE_GEN_ENABLED", True)
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "")
        assert image_gen.is_available() is False

    def test_available_when_both_set(self, monkeypatch):
        monkeypatch.setattr(image_gen, "IMAGE_GEN_ENABLED", True)
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "test-key")
        assert image_gen.is_available() is True


class TestImageGenGenerate:
    @pytest.mark.asyncio
    async def test_no_api_key(self, monkeypatch):
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "")
        result = await image_gen.generate("a cat")
        assert result.error == "OPENAI_API_KEY not set"

    @pytest.mark.asyncio
    async def test_invalid_size_uses_default(self, monkeypatch):
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "key")
        monkeypatch.setattr(image_gen, "IMAGE_SIZE", "1024x1024")
        with patch.dict(sys.modules, {"aiohttp": MagicMock()}):
            mock_resp = AsyncMock()
            mock_resp.status = 200
            b64_data = base64.b64encode(b"fake-png-data").decode()
            mock_resp.json = AsyncMock(return_value={
                "data": [{"b64_json": b64_data, "revised_prompt": "a cat"}]
            })
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_session = MagicMock()
            mock_session.post = MagicMock(return_value=mock_resp)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)

            mock_aiohttp = sys.modules["aiohttp"]
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = MagicMock()

            result = await image_gen.generate("a cat", size="invalid")
            assert result.error is None
            assert result.size == "1024x1024"

    @pytest.mark.asyncio
    async def test_aiohttp_import_error(self, monkeypatch):
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "key")

        real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def fail_aiohttp(name, *args, **kwargs):
            if name == "aiohttp":
                raise ImportError("no aiohttp")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_aiohttp):
            result = await image_gen.generate("a cat")
        assert result.error and "aiohttp" in result.error

    @pytest.mark.asyncio
    async def test_http_error(self, monkeypatch):
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "key")

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Server Error")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await image_gen.generate("a cat")
        assert "500" in result.error

    @pytest.mark.asyncio
    async def test_empty_b64(self, monkeypatch):
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "key")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "data": [{"b64_json": "", "revised_prompt": "a cat"}]
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await image_gen.generate("a cat")
        assert result.error == "Empty image data"

    @pytest.mark.asyncio
    async def test_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "key")

        b64_data = base64.b64encode(b"fake-png-data").decode()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "data": [{"b64_json": b64_data, "revised_prompt": "a nice cat"}]
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await image_gen.generate("a cat")
        assert result.error is None
        assert result.image_path
        assert result.revised_prompt == "a nice cat"

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch):
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "key")

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=asyncio.TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await image_gen.generate("a cat")
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_generic_exception(self, monkeypatch):
        monkeypatch.setattr(image_gen, "OPENAI_API_KEY", "key")

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=RuntimeError("network down"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await image_gen.generate("a cat")
        assert "network down" in result.error


# ═══════════════════════════════════════════════════════════════════════════════
# tts.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import tts


class TestTtsAvailable:
    def test_not_available_when_disabled(self, monkeypatch):
        monkeypatch.setattr(tts, "TTS_ENABLED", False)
        assert tts.is_available() is False

    def test_not_available_no_key(self, monkeypatch):
        monkeypatch.setattr(tts, "TTS_ENABLED", True)
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "")
        assert tts.is_available() is False

    def test_available(self, monkeypatch):
        monkeypatch.setattr(tts, "TTS_ENABLED", True)
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "key")
        assert tts.is_available() is True


class TestTtsSynthesize:
    @pytest.mark.asyncio
    async def test_no_api_key(self, monkeypatch):
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "")
        result = await tts.synthesize("hello")
        assert result.error == "OPENAI_API_KEY not set"

    @pytest.mark.asyncio
    async def test_invalid_voice(self, monkeypatch):
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "key")
        monkeypatch.setattr(tts, "TTS_VOICE", "alloy")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"audio-bytes")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await tts.synthesize("hello", voice="badvoice")
        assert result.voice == "alloy"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_text_truncation(self, monkeypatch):
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "key")
        monkeypatch.setattr(tts, "TTS_MAX_LENGTH", 10)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"audio")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await tts.synthesize("A" * 100)
        assert result.text_length == 10

    @pytest.mark.asyncio
    async def test_aiohttp_import_error(self, monkeypatch):
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "key")

        real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def fail_aiohttp(name, *args, **kwargs):
            if name == "aiohttp":
                raise ImportError("no aiohttp")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_aiohttp):
            result = await tts.synthesize("hello")
        assert "aiohttp" in result.error

    @pytest.mark.asyncio
    async def test_http_error(self, monkeypatch):
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "key")

        mock_resp = AsyncMock()
        mock_resp.status = 429
        mock_resp.text = AsyncMock(return_value="Rate limited")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await tts.synthesize("hello")
        assert "429" in result.error

    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "key")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"opus-audio-data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await tts.synthesize("hello")
        assert result.error is None
        assert result.audio_path

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch):
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "key")

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=asyncio.TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await tts.synthesize("hello")
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_generic_exception(self, monkeypatch):
        monkeypatch.setattr(tts, "OPENAI_API_KEY", "key")

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=RuntimeError("oops"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await tts.synthesize("hello")
        assert "oops" in result.error


# ═══════════════════════════════════════════════════════════════════════════════
# web_search.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import web_search


class TestWebSearchAvailable:
    def test_disabled(self, monkeypatch):
        monkeypatch.setattr(web_search, "WEB_SEARCH_ENABLED", False)
        assert web_search.is_available() is False

    def test_no_keys(self, monkeypatch):
        monkeypatch.setattr(web_search, "WEB_SEARCH_ENABLED", True)
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "")
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "auto")
        assert web_search.is_available() is False

    def test_with_brave_key(self, monkeypatch):
        monkeypatch.setattr(web_search, "WEB_SEARCH_ENABLED", True)
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "auto")
        assert web_search.is_available() is True


class TestResolveProvider:
    def test_brave_explicit(self, monkeypatch):
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "brave")
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "k")
        assert web_search._resolve_provider() == "brave"

    def test_brave_explicit_no_key(self, monkeypatch):
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "brave")
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "")
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "")
        assert web_search._resolve_provider() is None

    def test_tavily_explicit(self, monkeypatch):
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "tavily")
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "k")
        assert web_search._resolve_provider() == "tavily"

    def test_auto_brave_first(self, monkeypatch):
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "auto")
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "k")
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "k2")
        assert web_search._resolve_provider() == "brave"

    def test_auto_tavily_fallback(self, monkeypatch):
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "auto")
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "")
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "k")
        assert web_search._resolve_provider() == "tavily"

    def test_auto_no_keys(self, monkeypatch):
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "auto")
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "")
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "")
        assert web_search._resolve_provider() is None


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_no_provider(self, monkeypatch):
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "")
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "auto")
        result = await web_search.search("test")
        assert result.error and "No search API" in result.error

    @pytest.mark.asyncio
    async def test_unknown_provider(self, monkeypatch):
        monkeypatch.setattr(web_search, "_resolve_provider", lambda: "unknown")
        result = await web_search.search("test")
        assert "Unknown provider" in result.error

    @pytest.mark.asyncio
    async def test_brave_success(self, monkeypatch):
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "brave")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "web": {"results": [
                {"title": "Test", "url": "https://test.com", "description": "A test"},
            ]}
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("telechat_pkg.web_search._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_search.search("test")
        assert len(result.results) == 1
        assert result.results[0].title == "Test"

    @pytest.mark.asyncio
    async def test_brave_http_error(self, monkeypatch):
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "brave")

        mock_resp = AsyncMock()
        mock_resp.status = 403
        mock_resp.text = AsyncMock(return_value="Forbidden")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("telechat_pkg.web_search._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_search.search("test")
        assert "403" in result.error

    @pytest.mark.asyncio
    async def test_brave_timeout(self, monkeypatch):
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "brave")

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())

        with patch("telechat_pkg.web_search._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_search.search("test")
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_brave_exception(self, monkeypatch):
        monkeypatch.setattr(web_search, "BRAVE_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "brave")

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=RuntimeError("bad"))

        with patch("telechat_pkg.web_search._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_search.search("test")
        assert "bad" in result.error

    @pytest.mark.asyncio
    async def test_tavily_success(self, monkeypatch):
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "tavily")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "results": [
                {"title": "Tav", "url": "https://tav.com", "content": "result"},
            ]
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch("telechat_pkg.web_search._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_search.search("test")
        assert len(result.results) == 1
        assert result.results[0].snippet == "result"

    @pytest.mark.asyncio
    async def test_tavily_http_error(self, monkeypatch):
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "tavily")

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch("telechat_pkg.web_search._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_search.search("test")
        assert "500" in result.error

    @pytest.mark.asyncio
    async def test_tavily_timeout(self, monkeypatch):
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "tavily")

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=asyncio.TimeoutError())

        with patch("telechat_pkg.web_search._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_search.search("test")
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_tavily_exception(self, monkeypatch):
        monkeypatch.setattr(web_search, "TAVILY_API_KEY", "key")
        monkeypatch.setattr(web_search, "SEARCH_PROVIDER", "tavily")

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=RuntimeError("fail"))

        with patch("telechat_pkg.web_search._get_session", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await web_search.search("test")
        assert "fail" in result.error


class TestFormatResults:
    def test_with_error(self):
        resp = web_search.SearchResponse(query="q", error="bad")
        assert "Search error" in web_search.format_results(resp)

    def test_empty_results(self):
        resp = web_search.SearchResponse(query="q", results=[])
        assert "No results" in web_search.format_results(resp)

    def test_with_results(self):
        resp = web_search.SearchResponse(query="q", results=[
            web_search.SearchResult(title="T1", url="https://t.co", snippet="s1"),
        ])
        out = web_search.format_results(resp)
        assert "T1" in out
        assert "https://t.co" in out


# ═══════════════════════════════════════════════════════════════════════════════
# link_understanding.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import link_understanding as lu


class TestIsBlockedHost:
    def test_localhost(self):
        assert lu._is_blocked_host("localhost") is True

    def test_zero_addr(self):
        assert lu._is_blocked_host("0.0.0.0") is True

    def test_private_ip(self):
        assert lu._is_blocked_host("192.168.1.1") is True

    def test_loopback(self):
        assert lu._is_blocked_host("127.0.0.1") is True

    def test_normal_host(self):
        assert lu._is_blocked_host("example.com") is False

    def test_invalid_value_not_blocked(self):
        assert lu._is_blocked_host("not-an-ip") is False


class TestExtractLinks:
    def test_empty_message(self):
        assert lu.extract_links("") == []
        assert lu.extract_links("   ") == []

    def test_normal_urls(self):
        msg = "Check https://example.com and https://test.org"
        links = lu.extract_links(msg)
        assert len(links) == 2

    def test_markdown_links_stripped(self):
        msg = "See [link](https://md.com) and also https://bare.com"
        links = lu.extract_links(msg)
        assert len(links) == 1
        assert "bare.com" in links[0]

    def test_duplicate_urls(self):
        msg = "https://dup.com https://dup.com"
        links = lu.extract_links(msg)
        assert len(links) == 1

    def test_max_links_cap(self):
        urls = " ".join([f"https://site{i}.com" for i in range(10)])
        links = lu.extract_links(urls, max_links=2)
        assert len(links) == 2

    def test_blocked_host_filtered(self):
        msg = "https://localhost/admin https://example.com"
        links = lu.extract_links(msg)
        assert len(links) == 1
        assert "example.com" in links[0]

    def test_non_http_scheme(self):
        msg = "ftp://files.com https://real.com"
        links = lu.extract_links(msg)
        assert len(links) == 1

    def test_trailing_punctuation_stripped(self):
        links = lu.extract_links("Visit https://test.com.")
        assert links[0] == "https://test.com"


class TestFetchLinkContent:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.url = "https://example.com"
        mock_resp.content_type = "text/html"
        mock_resp.content = AsyncMock()
        mock_resp.content.read = AsyncMock(return_value=b"<html>Hello</html>")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await lu.fetch_link_content("https://example.com")
        assert result.content
        assert result.error is None

    @pytest.mark.asyncio
    async def test_http_error(self):
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.url = "https://example.com"
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await lu.fetch_link_content("https://example.com")
        assert "404" in result.error

    @pytest.mark.asyncio
    async def test_non_text_content(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.url = "https://example.com/img.png"
        mock_resp.content_type = "image/png"
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await lu.fetch_link_content("https://example.com/img.png")
        assert "Non-text" in result.error

    @pytest.mark.asyncio
    async def test_empty_response(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.url = "https://example.com"
        mock_resp.content_type = "text/html"
        mock_resp.content = AsyncMock()
        mock_resp.content.read = AsyncMock(return_value=b"   ")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await lu.fetch_link_content("https://example.com")
        assert "Empty" in result.error

    @pytest.mark.asyncio
    async def test_timeout(self):
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await lu.fetch_link_content("https://example.com")
        assert result.error == "Timeout"

    @pytest.mark.asyncio
    async def test_exception(self):
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=RuntimeError("dns fail"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("aiohttp.ClientTimeout"):
            result = await lu.fetch_link_content("https://example.com")
        assert "dns fail" in result.error


class TestStripHtml:
    def test_removes_scripts_and_styles(self):
        html = "<script>alert(1)</script><style>body{}</style><p>Hello</p>"
        assert "alert" not in lu._strip_html(html)
        assert "body{}" not in lu._strip_html(html)
        assert "Hello" in lu._strip_html(html)

    def test_removes_tags(self):
        assert lu._strip_html("<b>bold</b> <i>italic</i>") == "bold italic"

    def test_collapses_whitespace(self):
        result = lu._strip_html("  lots   of    spaces  ")
        assert "  " not in result


class TestUnderstandLinks:
    @pytest.mark.asyncio
    async def test_disabled(self, monkeypatch):
        monkeypatch.setattr(lu, "ENABLED", False)
        assert await lu.understand_links("https://test.com") is None

    @pytest.mark.asyncio
    async def test_no_urls(self, monkeypatch):
        monkeypatch.setattr(lu, "ENABLED", True)
        assert await lu.understand_links("no links here") is None

    @pytest.mark.asyncio
    async def test_html_content_stripped(self, monkeypatch):
        monkeypatch.setattr(lu, "ENABLED", True)

        async def fake_fetch(url):
            return lu.LinkResult(url=url, content="<html><body><p>Hello world</p></body></html>",
                                final_url=url)

        with patch.object(lu, "fetch_link_content", side_effect=fake_fetch):
            result = await lu.understand_links("https://example.com")
        assert result is not None
        assert "Hello world" in result
        assert "<html>" not in result

    @pytest.mark.asyncio
    async def test_content_truncation(self, monkeypatch):
        monkeypatch.setattr(lu, "ENABLED", True)

        async def fake_fetch(url):
            return lu.LinkResult(url=url, content="A" * 5000, final_url=url)

        with patch.object(lu, "fetch_link_content", side_effect=fake_fetch):
            result = await lu.understand_links("https://example.com")
        assert result is not None
        assert "…" in result

    @pytest.mark.asyncio
    async def test_all_errors_returns_none(self, monkeypatch):
        monkeypatch.setattr(lu, "ENABLED", True)

        async def fake_fetch(url):
            return lu.LinkResult(url=url, content="", final_url=url, error="fail")

        with patch.object(lu, "fetch_link_content", side_effect=fake_fetch):
            result = await lu.understand_links("https://example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_in_gather(self, monkeypatch):
        monkeypatch.setattr(lu, "ENABLED", True)

        async def fake_fetch(url):
            raise RuntimeError("boom")

        with patch.object(lu, "fetch_link_content", side_effect=fake_fetch):
            result = await lu.understand_links("https://example.com")
        assert result is None
