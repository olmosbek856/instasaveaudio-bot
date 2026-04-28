import os
import pytest
from pathlib import Path
from unittest.mock import patch
from downloader import (
    is_instagram_url,
    is_supported_url,
    detect_content_type,
    detect_platform,
    _format_for,
)


# --- is_supported_url (legacy alias is_instagram_url) ---

def test_reel_url_is_valid():
    assert is_supported_url("https://www.instagram.com/reel/ABC123/") is True


def test_post_url_is_valid():
    assert is_supported_url("https://www.instagram.com/p/ABC123/") is True


def test_story_url_is_valid():
    assert is_supported_url("https://www.instagram.com/stories/username/12345/") is True


def test_tv_url_is_valid():
    assert is_supported_url("https://www.instagram.com/tv/ABC123/") is True


def test_youtube_url_is_valid():
    assert is_supported_url("https://www.youtube.com/watch?v=abc123") is True


def test_youtube_shorts_url_is_valid():
    assert is_supported_url("https://www.youtube.com/shorts/abc123") is True


def test_youtu_be_url_is_valid():
    assert is_supported_url("https://youtu.be/abc123") is True


def test_tiktok_url_is_valid():
    assert is_supported_url("https://www.tiktok.com/@user/video/123") is True


def test_tiktok_vm_url_is_valid():
    assert is_supported_url("https://vm.tiktok.com/ZMrABC123/") is True


def test_snapchat_spotlight_url_is_valid():
    assert is_supported_url("https://www.snapchat.com/spotlight/W7_EDlXWTBiXAEEniNoMPwAAYdW1pemJxenhsZGNuAYDX2xfXAYDX2xeOAAAAAg") is True


def test_snapchat_story_url_is_valid():
    assert is_supported_url("https://story.snapchat.com/u/username") is True


def test_snapchat_at_username_spotlight_url_is_valid():
    """Snapchat shares URLs as /@username/spotlight/<id> — the regex must accept that form."""
    assert is_supported_url("https://www.snapchat.com/@snapchat/spotlight/W7_ABC") is True


def test_likee_url_is_valid():
    assert is_supported_url("https://likee.video/v/abc123") is True


def test_likee_short_url_is_valid():
    assert is_supported_url("https://l.likee.video/v/abc123") is True


def test_pinterest_pin_url_is_valid():
    assert is_supported_url("https://www.pinterest.com/pin/123456789/") is True


def test_pinterest_short_url_is_valid():
    assert is_supported_url("https://pin.it/abc123") is True


def test_pinterest_country_subdomain_is_valid():
    assert is_supported_url("https://uk.pinterest.com/pin/123456789/") is True


def test_threads_url_is_valid():
    assert is_supported_url("https://www.threads.net/@user/post/abc123") is True


def test_threads_com_url_is_valid():
    assert is_supported_url("https://www.threads.com/@user/post/abc123") is True


def test_random_text_is_invalid():
    assert is_supported_url("hello world") is False


def test_empty_string_is_invalid():
    assert is_supported_url("") is False


def test_google_url_is_invalid():
    assert is_supported_url("https://www.google.com/search?q=cats") is False


def test_legacy_alias_still_works():
    """is_instagram_url is kept as a backwards-compatible alias for is_supported_url."""
    assert is_instagram_url("https://www.tiktok.com/@u/video/1") is True
    assert is_instagram_url("https://google.com") is False


# --- detect_content_type ---

def test_story_url_returns_story():
    assert detect_content_type("https://www.instagram.com/stories/username/12345/") == "story"


def test_reel_url_returns_reel():
    assert detect_content_type("https://www.instagram.com/reel/ABC123/") == "reel"


def test_post_url_returns_post():
    assert detect_content_type("https://www.instagram.com/p/ABC123/") == "post"


def test_tv_url_returns_post():
    assert detect_content_type("https://www.instagram.com/tv/ABC123/") == "post"


def test_youtube_url_returns_youtube():
    assert detect_content_type("https://www.youtube.com/watch?v=abc123") == "youtube"


def test_youtu_be_url_returns_youtube():
    assert detect_content_type("https://youtu.be/abc123") == "youtube"


def test_tiktok_url_returns_tiktok():
    assert detect_content_type("https://www.tiktok.com/@user/video/123") == "tiktok"


def test_snapchat_url_returns_snapchat():
    assert detect_content_type("https://www.snapchat.com/spotlight/abc") == "snapchat"


def test_likee_url_returns_likee():
    assert detect_content_type("https://likee.video/v/abc") == "likee"


def test_pinterest_url_returns_pinterest():
    assert detect_content_type("https://www.pinterest.com/pin/12345/") == "pinterest"


def test_pinterest_short_url_returns_pinterest():
    assert detect_content_type("https://pin.it/abc") == "pinterest"


def test_threads_url_returns_threads():
    assert detect_content_type("https://www.threads.net/@user/post/abc") == "threads"


# --- detect_platform ---

def test_detect_platform_instagram():
    assert detect_platform("https://www.instagram.com/reel/ABC/") == "instagram"


def test_detect_platform_snapchat():
    assert detect_platform("https://www.snapchat.com/spotlight/abc") == "snapchat"


def test_detect_platform_unknown_returns_none():
    assert detect_platform("https://www.google.com/") is None


# --- Threads og: scraper ---

def test_parse_og_tags_extracts_image_and_video():
    from downloader import _parse_og_tags
    html = '''
    <html><head>
    <meta property="og:title" content="user (@handle) on Threads">
    <meta property="og:description" content="A post body.">
    <meta property="og:image" content="https://cdn/image.jpg">
    <meta property="og:video" content="https://cdn/video.mp4">
    </head></html>
    '''
    tags = _parse_og_tags(html)
    assert tags["title"] == "user (@handle) on Threads"
    assert tags["description"] == "A post body."
    assert tags["image"] == "https://cdn/image.jpg"
    assert tags["video"] == "https://cdn/video.mp4"


def test_parse_og_tags_handles_reverse_attr_order():
    from downloader import _parse_og_tags
    html = '<meta content="https://cdn/x.jpg" property="og:image">'
    assert _parse_og_tags(html)["image"] == "https://cdn/x.jpg"


def test_parse_og_tags_decodes_html_entities():
    from downloader import _parse_og_tags
    html = '<meta property="og:image" content="https://cdn/x.jpg?a=1&amp;b=2">'
    assert _parse_og_tags(html)["image"] == "https://cdn/x.jpg?a=1&b=2"


def test_parse_og_tags_decodes_numeric_entities():
    """Threads uses &#064; for @ in titles like 'User (&#064;handle) on Threads'."""
    from downloader import _parse_og_tags
    html = '<meta property="og:title" content="Bleacher Report (&#064;brfootball) on Threads">'
    assert _parse_og_tags(html)["title"] == "Bleacher Report (@brfootball) on Threads"


def test_extract_threads_video_from_playable_url():
    from downloader import _extract_threads_video_from_json
    html = '...{"playable_url":"https:\\/\\/scontent.cdninstagram.com\\/video.mp4?efg=abc"}...'
    url = _extract_threads_video_from_json(html)
    assert url == "https://scontent.cdninstagram.com/video.mp4?efg=abc"


def test_extract_threads_video_prefers_hd():
    from downloader import _extract_threads_video_from_json
    html = (
        '"playable_url":"https:\\/\\/cdn\\/sd.mp4",'
        '"playable_url_quality_hd":"https:\\/\\/cdn\\/hd.mp4"'
    )
    assert _extract_threads_video_from_json(html) == "https://cdn/hd.mp4"


def test_extract_threads_video_from_video_versions():
    from downloader import _extract_threads_video_from_json
    html = '"video_versions":[{"type":101,"width":1280,"height":720,"url":"https:\\/\\/cdn\\/v.mp4"}]'
    assert _extract_threads_video_from_json(html) == "https://cdn/v.mp4"


def test_extract_threads_video_returns_none_for_no_match():
    from downloader import _extract_threads_video_from_json
    assert _extract_threads_video_from_json("<html>no video here</html>") is None


def test_decode_js_string_handles_unicode_escapes():
    from downloader import _decode_js_string
    assert _decode_js_string("a\\u0026b") == "a&b"
    assert _decode_js_string("path\\/to\\/file") == "path/to/file"


@pytest.mark.asyncio
async def test_fetch_threads_media_returns_video_when_present(monkeypatch):
    from downloader import fetch_threads_media
    fake_html = '''
    <meta property="og:title" content="Transfermarkt (@transfermarkt_official) on Threads">
    <meta property="og:description" content="Real Madrid stat post.">
    <meta property="og:image" content="https://scontent.cdninstagram.com/thumb.jpg">
    <meta property="og:video" content="https://scontent.cdninstagram.com/video.mp4">
    '''

    class FakeResp:
        status = 200
        async def text(self, errors="ignore"): return fake_html
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    class FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def get(self, url, allow_redirects=True): return FakeResp()

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    meta, cdn_items = await fetch_threads_media("https://www.threads.com/@u/post/abc")
    assert cdn_items == [("https://scontent.cdninstagram.com/video.mp4", "mp4")]
    assert meta["uploader"] == "transfermarkt_official"
    assert "Real Madrid" in meta["title"]


@pytest.mark.asyncio
async def test_fetch_threads_media_falls_back_to_image(monkeypatch):
    from downloader import fetch_threads_media
    fake_html = '''
    <meta property="og:title" content="user on Threads">
    <meta property="og:image" content="https://cdn/photo.jpg">
    '''

    class FakeResp:
        status = 200
        async def text(self, errors="ignore"): return fake_html
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    class FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def get(self, url, allow_redirects=True): return FakeResp()

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    meta, cdn_items = await fetch_threads_media("https://www.threads.com/@u/post/abc")
    assert cdn_items == [("https://cdn/photo.jpg", "jpg")]
    assert meta["uploader"] == "user"


@pytest.mark.asyncio
async def test_fetch_threads_media_returns_empty_on_http_error(monkeypatch):
    from downloader import fetch_threads_media

    class FakeResp:
        status = 404
        async def text(self, errors="ignore"): return ""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    class FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def get(self, url, allow_redirects=True): return FakeResp()

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    meta, cdn_items = await fetch_threads_media("https://www.threads.com/@u/post/abc")
    assert cdn_items == []
    assert meta == {}


# --- _format_for (quality selection) ---

def test_format_for_specific_height_uses_filter():
    fmt = _format_for("youtube", 1080, "https://youtube.com/watch?v=x")
    assert "height<=1080" in fmt


def test_format_for_4k_uses_2160_filter():
    fmt = _format_for("youtube", 2160, "https://youtube.com/watch?v=x")
    assert "height<=2160" in fmt


def test_format_for_no_height_youtube_defaults_to_720():
    fmt = _format_for("youtube", None, "https://youtube.com/watch?v=x")
    assert "height<=720" in fmt


def test_format_for_no_height_instagram_uses_http_mp4():
    fmt = _format_for("reel", None, "https://www.instagram.com/reel/ABC/")
    assert "protocol^=http" in fmt


def test_format_for_no_height_tiktok_uses_best():
    fmt = _format_for("tiktok", None, "https://www.tiktok.com/@u/video/1")
    assert fmt == "best"


def test_format_for_no_height_snapchat_uses_best():
    fmt = _format_for("snapchat", None, "https://www.snapchat.com/spotlight/x")
    assert fmt == "best"


# --- cleanup ---

def test_cleanup_removes_parent_directory(tmp_path):
    subdir = tmp_path / "some-uuid"
    subdir.mkdir()
    test_file = subdir / "video.mp4"
    test_file.write_text("fake content")

    from downloader import cleanup
    cleanup(str(test_file))

    assert not subdir.exists()


def test_cleanup_ignores_missing_directory():
    from downloader import cleanup
    cleanup("/tmp/nonexistent-uuid/video.mp4")  # Must not raise


# --- download_media (mocked yt-dlp) ---

@pytest.mark.asyncio
async def test_download_media_returns_file_list(tmp_path):
    fake_file = tmp_path / "000_video.mp4"
    fake_file.write_text("fake video")

    with patch("yt_dlp.YoutubeDL") as MockYDL:
        instance = MockYDL.return_value.__enter__.return_value
        instance.download.side_effect = lambda urls: fake_file.write_text("content")

        import downloader
        original_temp = downloader.TEMP_DIR
        downloader.TEMP_DIR = str(tmp_path / "temp")
        os.makedirs(downloader.TEMP_DIR, exist_ok=True)

        try:
            with patch.object(Path, "iterdir", return_value=iter([fake_file])):
                result = await downloader.download_media("https://www.instagram.com/reel/ABC/")
            assert isinstance(result, list)
            assert len(result) == 1
        finally:
            downloader.TEMP_DIR = original_temp


@pytest.mark.asyncio
async def test_download_media_passes_height_to_format(tmp_path):
    """When height is specified, the format string must include the height filter."""
    fake_file = tmp_path / "000_video.mp4"
    fake_file.write_text("fake")
    captured: dict = {}

    class FakeYDL:
        def __init__(self, opts):
            captured["opts"] = opts
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def download(self, urls):
            fake_file.write_text("x")

    with patch("yt_dlp.YoutubeDL", FakeYDL):
        import downloader
        original_temp = downloader.TEMP_DIR
        downloader.TEMP_DIR = str(tmp_path / "temp")
        os.makedirs(downloader.TEMP_DIR, exist_ok=True)
        try:
            with patch.object(Path, "iterdir", return_value=iter([fake_file])):
                await downloader.download_media(
                    "https://www.youtube.com/watch?v=x", height=1080,
                )
            assert "height<=1080" in captured["opts"]["format"]
        finally:
            downloader.TEMP_DIR = original_temp
