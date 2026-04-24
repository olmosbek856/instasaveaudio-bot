import pytest
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
