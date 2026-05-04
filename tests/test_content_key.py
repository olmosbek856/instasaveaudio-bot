"""Unit tests for downloader.content_cache_key — pure function, no I/O."""
import pytest

from downloader import content_cache_key


# ── YouTube ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, expected_id", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc123", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/abc_123-XYZ", "abc_123-XYZ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?list=PLfoo&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
])
def test_youtube_video_id_extracted(url, expected_id):
    key = content_cache_key(url, "video", 720)
    assert key == f"yt:{expected_id}:video:720"


def test_youtube_audio_key_independent_of_video():
    video_key = content_cache_key("https://youtu.be/dQw4w9WgXcQ", "video", 720)
    audio_key = content_cache_key("https://youtu.be/dQw4w9WgXcQ", "audio")
    assert video_key != audio_key
    assert "audio" in audio_key


def test_youtube_quality_differentiates_keys():
    base = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert content_cache_key(base, "video", 480) != content_cache_key(base, "video", 720)
    assert content_cache_key(base, "video", 1080) != content_cache_key(base, "video", 2160)


# ── Instagram ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, expected_shortcode", [
    ("https://www.instagram.com/p/CxAbc123/", "CxAbc123"),
    ("https://instagram.com/reel/CxAbc123/", "CxAbc123"),
    ("https://www.instagram.com/tv/CxAbc123/", "CxAbc123"),
    ("https://www.instagram.com/p/CxAbc-_123/?igshid=foo", "CxAbc-_123"),
])
def test_instagram_post_reel_shortcode(url, expected_shortcode):
    key = content_cache_key(url, "video", 720)
    assert key == f"ig:{expected_shortcode}:video:720"


def test_instagram_story_uses_story_id():
    url = "https://www.instagram.com/stories/someuser/3215843726432143000/"
    key = content_cache_key(url, "video", 720)
    assert key == "ig:story:3215843726432143000:video:720"


def test_instagram_story_distinct_from_post():
    story_key = content_cache_key(
        "https://www.instagram.com/stories/u/3215843726432143000/", "video", 720,
    )
    post_key = content_cache_key(
        "https://www.instagram.com/p/CxAbc123/", "video", 720,
    )
    assert story_key != post_key
    assert story_key.startswith("ig:story:")
    assert post_key.startswith("ig:CxAbc")


# ── TikTok ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, expected_id", [
    ("https://www.tiktok.com/@user/video/7234567890123456789", "7234567890123456789"),
    ("https://tiktok.com/@user/video/7234567890123456789", "7234567890123456789"),
    ("https://www.tiktok.com/@user/photo/7234567890123456789", "7234567890123456789"),
])
def test_tiktok_video_id_extracted(url, expected_id):
    key = content_cache_key(url, "video", 720)
    assert key == f"tt:{expected_id}:video:720"


def test_tiktok_short_url_returns_none():
    # Short URLs (vm.tiktok.com/abc, vt.tiktok.com/abc) need redirect resolution.
    # We don't resolve them in content_cache_key; they should return None.
    assert content_cache_key("https://vm.tiktok.com/abc123", "video", 720) is None
    assert content_cache_key("https://vt.tiktok.com/xyz789", "video", 720) is None


# ── Threads ────────────────────────────────────────────────────────────────

def test_threads_post_id_extracted():
    url = "https://www.threads.net/@user/post/CxAbc123"
    key = content_cache_key(url, "video", 720)
    assert key == "th:CxAbc123:video:720"


# ── Pinterest ──────────────────────────────────────────────────────────────

def test_pinterest_pin_id_extracted():
    url = "https://www.pinterest.com/pin/123456789012345678/"
    key = content_cache_key(url, "video", 720)
    assert key == "pn:123456789012345678:video:720"


def test_pinterest_short_url_extracted():
    url = "https://pin.it/abc123XYZ"
    key = content_cache_key(url, "video", 720)
    assert key == "pn:abc123XYZ:video:720"


# ── Unsupported / edge cases ───────────────────────────────────────────────

def test_unsupported_url_returns_none():
    assert content_cache_key("https://example.com/something", "video", 720) is None


def test_empty_url_returns_none():
    assert content_cache_key("", "video", 720) is None


def test_default_quality_when_none():
    key = content_cache_key("https://youtu.be/dQw4w9WgXcQ", "video", None)
    assert key is not None
    assert "default" in key
