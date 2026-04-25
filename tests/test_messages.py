import pytest
from messages import get_message, MESSAGES


def test_uzbek_is_default():
    assert get_message("uz", "downloading") == "⏳"


def test_english_has_own_block():
    assert get_message("en", "downloading") == "⏳"


def test_none_lang_falls_back_to_uzbek():
    assert get_message(None, "downloading") == "⏳"


def test_russian_returns_russian():
    assert get_message("ru", "downloading") == "⏳"


def test_ru_RU_locale_returns_russian():
    assert get_message("ru-RU", "downloading") == "⏳"


def test_all_keys_exist_in_all_three_languages():
    uz_keys = set(MESSAGES["uz"].keys())
    ru_keys = set(MESSAGES["ru"].keys())
    en_keys = set(MESSAGES["en"].keys())
    assert uz_keys == ru_keys == en_keys


def test_downloading_reel_key_uz():
    assert get_message("uz", "downloading_reel") == "⏳"


def test_downloading_reel_key_ru():
    assert get_message("ru", "downloading_reel") == "⏳"


def test_downloading_reel_key_en():
    assert get_message("en", "downloading_reel") == "⏳"


def test_downloading_youtube_key_en():
    assert get_message("en", "downloading_youtube") == "⏳"


def test_downloading_tiktok_key_en():
    assert get_message("en", "downloading_tiktok") == "⏳"


def test_choose_lang_key_exists():
    assert get_message("uz", "choose_lang") != "choose_lang"
    assert get_message("ru", "choose_lang") != "choose_lang"
    assert get_message("en", "choose_lang") != "choose_lang"


def test_lang_set_key_exists():
    assert get_message("uz", "lang_set") == "✅ Til saqlandi."
    assert get_message("ru", "lang_set") == "✅ Язык сохранён."
    assert get_message("en", "lang_set") == "✅ Language saved."


def test_help_key_exists():
    for lang in ("uz", "ru", "en"):
        msg = get_message(lang, "help")
        assert "InstaSaveBot" in msg
        assert "50" in msg


def test_choose_format_key_exists():
    assert get_message("uz", "choose_format") == "Yuklab olish formatlari ↓"
    assert get_message("en", "choose_format") == "Download formats ↓"


def test_btn_video_key_exists():
    assert get_message("uz", "btn_video") == "📼 Video"
    assert get_message("en", "btn_video") == "📼 Video"


def test_btn_audio_key_exists():
    assert get_message("uz", "btn_audio") == "🎧 Audio"
    assert get_message("en", "btn_audio") == "🎧 Audio"


def test_invalid_key_returns_key_name():
    assert get_message("uz", "nonexistent_key_xyz") == "nonexistent_key_xyz"
