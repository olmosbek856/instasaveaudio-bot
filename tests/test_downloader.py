import os
import pytest
from pathlib import Path
from unittest.mock import patch
from downloader import is_instagram_url, detect_content_type


# --- is_instagram_url ---

def test_reel_url_is_valid():
    assert is_instagram_url("https://www.instagram.com/reel/ABC123/") is True


def test_post_url_is_valid():
    assert is_instagram_url("https://www.instagram.com/p/ABC123/") is True


def test_story_url_is_valid():
    assert is_instagram_url("https://www.instagram.com/stories/username/12345/") is True


def test_tv_url_is_valid():
    assert is_instagram_url("https://www.instagram.com/tv/ABC123/") is True


def test_youtube_url_is_valid():
    assert is_instagram_url("https://www.youtube.com/watch?v=abc123") is True


def test_youtube_shorts_url_is_valid():
    assert is_instagram_url("https://www.youtube.com/shorts/abc123") is True


def test_youtu_be_url_is_valid():
    assert is_instagram_url("https://youtu.be/abc123") is True


def test_tiktok_url_is_valid():
    assert is_instagram_url("https://www.tiktok.com/@user/video/123") is True


def test_tiktok_vm_url_is_valid():
    assert is_instagram_url("https://vm.tiktok.com/ZMrABC123/") is True


def test_random_text_is_invalid():
    assert is_instagram_url("hello world") is False


def test_empty_string_is_invalid():
    assert is_instagram_url("") is False


def test_google_url_is_invalid():
    assert is_instagram_url("https://www.google.com/search?q=cats") is False


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
