# InstaSaveBot — Design Spec

**Date:** 2026-04-24  
**Author:** Olmosbek Rustamov  

---

## Context

Foydalanuvchilar Instagram dan video, reel, story va audio yuklab olishni xohlashadi. Telegram bot orqali link yuborish bilan darhol yuklab olish imkoniyati kerak. Bot public — istalgan foydalanuvchi ishlatishi mumkin.

---

## Goals

- Instagram video va reel larni yuklab olish
- Instagram story larni yuklab olish (faqat public)
- Video/reel dan audioni (mp3) ajratib olish
- O'zbek va rus tilida avtomatik javob berish
- Fayllar yuborilgandan keyin serverda qolmasin

---

## Architecture

```
instasavebot/
├── bot.py           # Telegram handlers (aiogram 3.x)
├── downloader.py    # yt-dlp wrapper
├── messages.py      # O'zbek va rus xabarlari
├── config.py        # Token, settings
├── requirements.txt
├── .env             # BOT_TOKEN (git ga kirmaydi)
└── .env.example     # Template
```

**Dependencies:**
- `aiogram==3.x` — Telegram Bot framework
- `yt-dlp` — Instagram content downloader
- `python-dotenv` — .env faylni o'qish
- `ffmpeg` — audio ajratish (system package)

---

## User Flow

```
Foydalanuvchi → Instagram link yuboradi
     ↓
Bot → link Instagram ekanlini tekshiradi
     ↓
Bot → "Yuklanmoqda... ⏳" xabari
     ↓
downloader.py → yt-dlp orqali temp/ ga yuklaydi
     ↓
Content turi?
  ├── Video/Reel → Video yuboradi + inline tugma [🎵 Audiosi]
  ├── Story      → Media yuboradi
  └── Image      → Rasm yuboradi
     ↓
Temp fayl o'chiriladi
     ↓
[Agar user "🎵 Audiosi" tugmasini bossa]
  → Callback data dan URL olinadi
  → yt-dlp audio-only rejimida qayta yuklab oladi (temp/ ga)
  → mp3 yuboriladi → temp o'chiriladi
```

---

## Components

### bot.py

**Handlers:**
- `/start` — qisqa tanitish xabari
- `message_handler(url)` — Instagram linkini qayta ishlash
- `callback_handler("audio:{url}")` — URL qayta yuklab audio sifatida yuborish

**Language detection:**
```python
lang = message.from_user.language_code  # "ru", "uz", "en", ...
# "ru" -> rus tili, boshqa -> o'zbek tili
```

### downloader.py

**Functions:**
```python
async def download_video(url: str, output_dir: str) -> str
    # Returns: downloaded file path

async def download_audio(url: str, output_dir: str) -> str
    # Returns: mp3 file path

def detect_content_type(url: str) -> Literal["video", "story", "post"]
    # Regex based: /reel/, /stories/, /p/
```

**yt-dlp options:**
- `format`: best video <= 50MB
- `outtmpl`: `temp/{uuid}/%(title)s.%(ext)s`
- Stories: public only, no login required

### messages.py

Ikki til uchun barcha xabarlar dictionary da:
```python
MESSAGES = {
    "uz": {
        "start": "...",
        "downloading": "Yuklanmoqda... ⏳",
        "done": "Mana! ✅",
        "error": "Yuklab bo'lmadi 😔",
        "invalid_url": "Bu Instagram havolasi emas",
        "too_large": "Fayl 50MB dan katta",
        "audio_btn": "🎵 Audiosi",
    },
    "ru": {
        "start": "...",  # Rus tilidagi versiya
        "downloading": "Загружаю... ⏳",
        "done": "Готово! ✅",
        "error": "Не удалось загрузить 😔",
        "invalid_url": "Это не ссылка Instagram",
        "too_large": "Файл больше 50MB",
        "audio_btn": "🎵 Аудио",
    }
}
```

### config.py

```python
BOT_TOKEN = os.getenv("BOT_TOKEN")
TEMP_DIR = "./temp"
MAX_FILE_SIZE_MB = 50
```

---

## Error Handling

| Xatolik | Javob |
|---------|-------|
| Instagram bo'lmagan link | "Bu Instagram havolasi emas" |
| Yuklab bo'lmadi | "Yuklab bo'lmadi, keyinroq urinib ko'ring" |
| Fayl >50MB | "Fayl hajmi juda katta (50MB dan oshadi)" |
| Network error | "Internetda muammo, qaytadan urining" |

---

## File Size Limits

Telegram bots uchun:
- Video/fayl yuborish: max **50 MB**
- yt-dlp `format` parametri orqali eng yaxshi sifatli lekin 50MB dan kichik versiya tanlanadi

---

## Security

- `BOT_TOKEN` faqat `.env` da, git ga kirmaydi
- Foydalanuvchi kiritgan URL faqat regex tekshiruvidan o'tadi, shell ga uzatilmaydi
- Temp fayllar har safar UUID papkada, bir-biriga aralashmaydi

---

## Verification (Test Plani)

1. Bot ishga tushirish: `python bot.py`
2. Telegram da `/start` yuboring → salom xabari kelishi kerak
3. Instagram reel linki yuboring → video kelishi kerak
4. "🎵 Audiosi" tugmasini bosing → mp3 kelishi kerak
5. Instagram story linki yuboring → media kelishi kerak
6. Noto'g'ri link yuboring → xato xabari kelishi kerak
7. Rus tilidagi foydalanuvchi → rus tilida javob kelishi kerak
