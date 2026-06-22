# BOT-TELE — Telegram Key Activation Bot

Bot relay tin nhắn qua @FluoriteResetKeyBot với hệ thống kích hoạt key.

## Tính năng
- User nhập key do admin @duyanh0509 cấp mới dùng được
- Relay giữ nguyên bold/italic/code formatting
- Admin: /createkey /deletekey /listkeys /checkkey

## Deploy Railway — Env Vars
| Tên | Mô tả |
|---|---|
| BOT_TOKEN | Token bot Telegram |
| TELEGRAM_API_ID | API ID từ my.telegram.org |
| TELEGRAM_API_HASH | API Hash |
| USERBOT_PHONE | Số điện thoại userbot (+84...) |
| SESSION_STRING | Telethon StringSession |
