# Deploy yo'riqnomasi (Docker)

## 1. VPS tanlash (~$4-6/oy)

Tavsiya etilgan provayderlar (kuchsiz yuk uchun arzon variantlari):

| Provayder | Narx | Eslatma |
|-----------|------|---------|
| **Hetzner Cloud** | €4/oy (CX22) | Eng arzon, Yevropa |
| **DigitalOcean** | $6/oy | Yaxshi UI, AQSh/Yevropa |
| **Vultr** | $5/oy | Ko'p region |
| **Contabo** | €4/oy | Resurslar ko'p, biroz sekin tarmoq |

**Minimal spec:** 1 vCPU, 1 GB RAM, 20 GB disk, Ubuntu 22.04/24.04 LTS.

## 2. Server'ga ulanish

VPS yaratganingizdan keyin SSH orqali kiring:

```bash
ssh root@<server_ip>
```

## 3. Docker o'rnatish

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

## 4. Bot kodini yuklash

```bash
git clone <repo_url> /opt/instasavebot
cd /opt/instasavebot
```

Yoki agar git yo'q bo'lsa, lokal kompyuterdan `scp` orqali ko'chiring:

```bash
# Lokal kompyuterda:
scp -r ./instasavebot root@<server_ip>:/opt/
```

## 5. `.env` faylini sozlash

```bash
cd /opt/instasavebot
cp .env.example .env
nano .env
```

`.env` ichiga yozing:

```
BOT_TOKEN=<sizning_yangi_tokeningiz>
```

## 6. (Ixtiyoriy) Cookies — Story uchun

Story yuklab olish uchun Instagram cookies kerak:

1. Browser'da Instagram'ga login qiling
2. Browser kengaytmasi orqali cookies'ni Netscape formatida eksport qiling (masalan, "Get cookies.txt LOCALLY")
3. `cookies.txt` faylini `/opt/instasavebot/cookies.txt` ga ko'chiring

Cookies'siz reels, posts va YouTube ishlaydi — faqat stories ishlamaydi.

```bash
# Bo'sh fayl (cookies'siz):
touch cookies.txt
```

## 7. Volume fayllarini tayyorlash

Docker bind mount uchun fayllar oldindan mavjud bo'lishi kerak:

```bash
touch cookies.txt user_langs.json
```

## 8. Botni ishga tushirish

```bash
docker compose up -d --build
```

## 9. Loglarni tekshirish

```bash
docker compose logs -f
```

`Ctrl+C` — log tomoshani to'xtatadi (bot ishlashda davom etadi).

## 10. Boshqaruv buyruqlari

```bash
docker compose stop          # to'xtatish
docker compose start         # ishga tushirish
docker compose restart       # qayta ishga tushirish
docker compose logs -f       # loglar
docker compose down          # to'xtatish + container o'chirish
docker compose up -d --build # qayta build qilib ishga tushirish (kod o'zgarganida)
```

## 11. Yangilash (kodni o'zgartirgandan keyin)

```bash
cd /opt/instasavebot
git pull                              # yoki scp orqali yangilash
docker compose up -d --build          # qayta build + restart
```

---

## Railway.app deploy

Railway Dockerfile orqali build qiladi. `cookies.txt` git'ga commit qilinmagan
(va qilinmasligi kerak — bu maxfiy fayl), shuning uchun **environment variable**
orqali yuklanadi:

1. Railway dashboard → Variables tab
2. `BOT_TOKEN` — Telegram bot token
3. `INSTAGRAM_COOKIES_TXT` — `cookies.txt` faylining **butun matni**
   (Browser kengaytmasi orqali export qilingan Netscape format)

Bot ishga tushganda env var o'qib `cookies.txt` fayliga yozadi. Cookies'siz
Instagram reels/stories deyarli har doim "Download failed" qaytaradi —
Instagram anonim so'rovlarni tezda 401/403 bilan to'xtatadi.

Cookies'ni yangilash kerak bo'lganda Railway Variables'ni yangilab redeploy qiling.

---

## Xavfsizlik tavsiyalari

- `.env` git'ga commit qilinmagan (`.gitignore`'da)
- `cookies.txt` git'ga commit qilinmagan
- Docker container'da bot **non-root** user (`botuser`) sifatida ishlaydi
- Server uchun `ufw` firewall yoqing va faqat 22 (SSH) portni oching:
  ```bash
  ufw allow 22 && ufw enable
  ```
- SSH key autentifikatsiyasini yoqing, parolni o'chiring (xavfsizlik)

## Resurs monitoringi

```bash
docker stats instasavebot   # CPU, RAM, network
df -h                       # disk to'liq emasligini tekshirish
```
