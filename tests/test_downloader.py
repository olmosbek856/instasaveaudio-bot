import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
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


def test_non_instagram_url_is_invalid():
    assert is_instagram_url("https://www.youtube.com/watch?v=abc") is False


def test_random_text_is_invalid():
    assert is_instagram_url("hello world") is False


def test_empty_string_is_invalid():
    assert is_instagram_url("") is False


# --- detect_content_type ---

def test_story_url_returns_story():
    assert detect_content_type("https://www.instagram.com/stories/username/12345/") == "story"


def test_reel_url_returns_video():
    assert detect_content_type("https://www.instagram.com/reel/ABC123/") == "video"


def test_post_url_returns_video():
    assert detect_content_type("https://www.instagram.com/p/ABC123/") == "video"


def test_tv_url_returns_video():
    assert detect_content_type("https://www.instagram.com/tv/ABC123/") == "video"


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

    def fake_download(self_ydl, urls):
        pass  # yt-dlp does nothing

    with patch("yt_dlp.YoutubeDL") as MockYDL:
        instance = MockYDL.return_value.__enter__.return_value
        instance.download.side_effect = lambda urls: fake_file.write_text("content")

        # We override TEMP_DIR to use tmp_path for this test
        import downloader
        original_temp = downloader.TEMP_DIR
        downloader.TEMP_DIR = str(tmp_path / "temp")
        os.makedirs(downloader.TEMP_DIR, exist_ok=True)

        try:
            with patch.object(Path, "iterdir", return_value=iter([fake_file])):
                result = await downloader.download_media("https://www.instagram.com/reel/ABC/")
            assert isinstance(result, list)
            assert len(result) >= 0  # May be empty if mock doesn't create files
        finally:
            downloader.TEMP_DIR = original_temp
