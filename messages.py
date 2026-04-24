MESSAGES = {
    "uz": {
        "start": (
            "Salom! 👋 Men <b>InstaSaveBot</b>man.\n\n"
            "Instagram dan video, reel, story va audio yuklab beraman.\n\n"
            "📌 Foydalanish: Instagram linkini yuboring."
        ),
        "downloading": "Yuklanmoqda... ⏳",
        "done_video": "✅ Mana!",
        "done_audio": "🎵 Audio tayyor!",
        "error": "😔 Yuklab bo'lmadi. Keyinroq urinib ko'ring.",
        "invalid_url": (
            "❌ Bu Instagram havolasi emas.\n\n"
            "Iltimos, to'g'ri Instagram linkini yuboring."
        ),
        "too_large": "😔 Fayl hajmi juda katta (50MB dan oshadi). Telegram cheklovi.",
        "audio_btn": "🎵 Audiosi",
    },
    "ru": {
        "start": (
            "Привет! 👋 Я <b>InstaSaveBot</b>.\n\n"
            "Скачиваю видео, reel, stories и аудио из Instagram.\n\n"
            "📌 Как пользоваться: отправьте ссылку из Instagram."
        ),
        "downloading": "Загружаю... ⏳",
        "done_video": "✅ Готово!",
        "done_audio": "🎵 Аудио готово!",
        "error": "😔 Не удалось загрузить. Попробуйте позже.",
        "invalid_url": (
            "❌ Это не ссылка Instagram.\n\n"
            "Пожалуйста, отправьте правильную ссылку."
        ),
        "too_large": "😔 Файл слишком большой (больше 50MB). Ограничение Telegram.",
        "audio_btn": "🎵 Аудио",
    },
}


def get_message(lang_code: str | None, key: str) -> str:
    lang = "ru" if lang_code and lang_code.startswith("ru") else "uz"
    return MESSAGES[lang].get(key, MESSAGES["uz"].get(key, key))
