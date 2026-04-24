import pytest
from messages import get_message


def test_uzbek_is_default():
    assert get_message("uz", "downloading") == "Yuklanmoqda... ⏳"


def test_english_falls_back_to_uzbek():
    assert get_message("en", "downloading") == "Yuklanmoqda... ⏳"


def test_none_lang_falls_back_to_uzbek():
    assert get_message(None, "downloading") == "Yuklanmoqda... ⏳"


def test_russian_returns_russian():
    assert get_message("ru", "downloading") == "Загружаю... ⏳"


def test_ru_RU_locale_returns_russian():
    assert get_message("ru-RU", "downloading") == "Загружаю... ⏳"


def test_all_keys_exist_in_both_languages():
    from messages import MESSAGES
    assert set(MESSAGES["uz"].keys()) == set(MESSAGES["ru"].keys())
