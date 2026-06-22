import os
import asyncio
import logging
import sqlite3
import random
import string
from datetime import datetime, timedelta
from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.extensions import html as tg_html
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import Conflict, NetworkError, TimedOut

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ["BOT_TOKEN"]
API_ID         = int(os.environ["TELEGRAM_API_ID"])
API_HASH       = os.environ["TELEGRAM_API_HASH"]
PHONE          = os.environ.get("USERBOT_PHONE", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
ADMIN_IDS      = [7629364269]
FLUORITE_BOT   = "FluoriteResetKeyBot"
BOT_DIR        = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE   = os.path.join(BOT_DIR, "userbot_session")
DB_PATH        = os.path.join(BOT_DIR, "keys.db")
PORT           = int(os.environ.get("PORT", 3000))

userbot_client: TelegramClient = None
relay_map:     dict[int, asyncio.Future] = {}
relay_initial: dict[int, str] = {}

# ── DATABASE ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS keys (
        key TEXT PRIMARY KEY,
        created_at TEXT,
        expires_at TEXT,
        used_by INTEGER,
        is_used INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS activated_users (
        user_id INTEGER PRIMARY KEY,
        key_used TEXT,
        activated_at TEXT,
        expires_at TEXT
    )""")
    conn.commit()
    conn.close()

def add_key(key: str, days: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO keys (key,created_at,expires_at,used_by,is_used) VALUES (?,?,?,NULL,0)",
        (key, datetime.now().isoformat(), (datetime.now() + timedelta(days=days)).isoformat())
    )
    conn.commit(); conn.close()

def delete_key(key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM keys WHERE key=?", (key,))
    ok = conn.total_changes > 0
    conn.commit(); conn.close()
    return ok

def get_key(key: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT key,expires_at,is_used,used_by FROM keys WHERE key=?", (key,)
    ).fetchone()
    conn.close(); return row

def list_keys():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT key,expires_at,is_used,used_by FROM keys ORDER BY created_at DESC"
    ).fetchall()
    conn.close(); return rows

def activate_user(user_id: int, key: str, expires_at: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT is_used FROM keys WHERE key=?", (key,)).fetchone()
        if not row or row[0]:
            return False
        conn.execute("UPDATE keys SET is_used=1, used_by=? WHERE key=?", (user_id, key))
        conn.execute(
            "INSERT OR REPLACE INTO activated_users (user_id,key_used,activated_at,expires_at) VALUES (?,?,?,?)",
            (user_id, key, datetime.now().isoformat(), expires_at)
        )
        conn.commit(); return True
    finally:
        conn.close()

def is_user_activated(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT expires_at FROM activated_users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    if not row: return False
    if row[0] and datetime.fromisoformat(row[0]) < datetime.now(): return False
    return True

def rand_key(n=16):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

def is_admin(uid): return uid in ADMIN_IDS

# ── FORMATTING ────────────────────────────────────────────────────────────────

def extract_html(event) -> str:
    msg = event.message
    raw = msg.text or msg.message or ""
    if not raw: return ""
    try:
        return tg_html.unparse(raw, msg.entities or [])
    except Exception as e:
        logger.warning(f"[format] lỗi: {e}")
        return raw

# ── RELAY ─────────────────────────────────────────────────────────────────────

async def send_via_userbot(text: str, user_id: int) -> str:
    global userbot_client
    if userbot_client is None or not userbot_client.is_connected():
        return "⚠️ Userbot đang kết nối lại, thử sau 10 giây."
    loop = asyncio.get_running_loop()
    fut  = loop.create_future()
    relay_map[user_id] = fut
    try:
        await userbot_client.send_message(FLUORITE_BOT, text)
        response = await asyncio.wait_for(fut, timeout=25.0)
        return response
    except asyncio.TimeoutError:
        relay_map.pop(user_id, None); relay_initial.pop(user_id, None)
        return "⏱ @FluoriteResetKeyBot không phản hồi."
    except Exception as e:
        relay_map.pop(user_id, None); relay_initial.pop(user_id, None)
        logger.error(f"[relay] {e}")
        return f"⚠️ Lỗi relay: {e}"

async def _fallback_resolve(user_id: int, delay: float):
    await asyncio.sleep(delay)
    if user_id in relay_map and user_id in relay_initial:
        text = relay_initial.pop(user_id, "")
        fut  = relay_map.pop(user_id, None)
        if fut and not fut.done():
            fut.set_result(text)

async def on_fluorite_new(event):
    html_text = extract_html(event)
    if not html_text or not relay_map: return
    user_id = next(iter(relay_map))
    relay_initial[user_id] = html_text
    asyncio.ensure_future(_fallback_resolve(user_id, 12.0))

async def on_fluorite_edit(event):
    html_text = extract_html(event)
    if not html_text or not relay_map: return
    user_id = next(iter(relay_map))
    relay_initial.pop(user_id, None)
    fut = relay_map.pop(user_id, None)
    if fut and not fut.done():
        fut.set_result(html_text)

# ── BOT COMMANDS ──────────────────────────────────────────────────────────────

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id) or is_user_activated(user_id):
        await update.message.reply_text(
            "✅ <b>BẠN CÓ THỂ SỬ DỤNG ĐƯỢC TÍNH NĂNG CỦA BOT</b>\n\n"
            "Gửi bất kỳ tin nhắn nào để reset key qua @FluoriteResetKeyBot.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "🔐 <b>VUI LÒNG NHẬP KEY DO ADMIN @duyanh0509 CẤP ĐỂ SÀI</b>",
            parse_mode="HTML"
        )

async def createkey_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Không có quyền.")
    args = list(ctx.args); amount = 1; days = 30; custom_key = None
    if "amount" in args:
        idx = args.index("amount")
        try:
            amount = int(args[idx+1]); args = args[:idx] + args[idx+2:]
        except (IndexError, ValueError):
            return await update.message.reply_text("❌ Cú pháp: /createkey [prefix] [ngày] [amount N]")
    if len(args) >= 2:
        try:
            days = int(args[-1]); custom_key = " ".join(args[:-1]) or None
        except ValueError:
            custom_key = " ".join(args)
    elif len(args) == 1:
        try: days = int(args[0])
        except ValueError: custom_key = args[0]
    keys = []
    for _ in range(amount):
        k = custom_key if (custom_key and amount == 1) else (f"{custom_key}-{rand_key(6)}" if custom_key else rand_key())
        add_key(k, days); keys.append(k)
    if amount == 1:
        await update.message.reply_text(
            f"✅ <b>Tạo key thành công!</b>\n\n🔑 Key: <code>{keys[0]}</code>\n⏳ Hạn: <b>{days} ngày</b>",
            parse_mode="HTML"
        )
    else:
        body = "\n".join(f"<code>{k}</code>" for k in keys)
        await update.message.reply_text(
            f"✅ <b>Tạo {amount} key ({days} ngày):</b>\n\n{body}", parse_mode="HTML"
        )

async def deletekey_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Không có quyền.")
    if not ctx.args:
        return await update.message.reply_text("❌ /deletekey [key]")
    key = " ".join(ctx.args)
    msg = f"✅ Đã xóa <code>{key}</code>" if delete_key(key) else f"❌ Không tìm thấy <code>{key}</code>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def listkeys_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Không có quyền.")
    rows = list_keys()
    if not rows:
        return await update.message.reply_text("📭 Chưa có key nào.")
    lines = []
    for k, exp, used, used_by in rows[:30]:
        status = f"❌ Đã dùng (UID:{used_by})" if used else "✅ Chưa dùng"
        lines.append(f"<code>{k}</code>\n  ↳ {status} | {(exp or '')[:10]}")
    await update.message.reply_text(
        f"🔑 <b>Keys ({len(rows)}):</b>\n\n" + "\n\n".join(lines), parse_mode="HTML"
    )

async def checkkey_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("❌ /checkkey [key]")
    key = " ".join(ctx.args); row = get_key(key)
    if not row:
        return await update.message.reply_text(f"❌ Key không tồn tại: <code>{key}</code>", parse_mode="HTML")
    _, exp, used, used_by = row
    status = f"❌ Đã dùng (UID: {used_by})" if used else "✅ Chưa dùng"
    await update.message.reply_text(
        f"🔑 <b>Key:</b> <code>{key}</code>\n{status}\n📅 HSD: {(exp or '—')[:10]}",
        parse_mode="HTML"
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text: return
    user_id = update.effective_user.id

    if not is_admin(user_id) and not is_user_activated(user_id):
        row = get_key(text.strip())
        if not row:
            return await update.message.reply_text(
                "❌ <b>KEY SAI VUI LÒNG KIỂM TRA VÀ NHẬP LẠI</b>", parse_mode="HTML"
            )
        key_val, expires_at, is_used, _ = row
        if is_used:
            return await update.message.reply_text(
                "❌ <b>KEY SAI VUI LÒNG KIỂM TRA VÀ NHẬP LẠI</b>", parse_mode="HTML"
            )
        if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
            return await update.message.reply_text(
                "❌ <b>KEY SAI VUI LÒNG KIỂM TRA VÀ NHẬP LẠI</b>", parse_mode="HTML"
            )
        if activate_user(user_id, key_val, expires_at or ""):
            return await update.message.reply_text(
                "✅ <b>BẠN CÓ THỂ SỬ DỤNG ĐƯỢC TÍNH NĂNG CỦA BOT</b>", parse_mode="HTML"
            )
        return await update.message.reply_text(
            "❌ <b>KEY SAI VUI LÒNG KIỂM TRA VÀ NHẬP LẠI</b>", parse_mode="HTML"
        )

    msg = await update.message.reply_text("⏳ Đang xử lý...")
    response = await send_via_userbot(text, user_id)
    try:
        await msg.edit_text(response, parse_mode="HTML")
    except Exception:
        try:
            await msg.edit_text(response)
        except Exception as e:
            logger.error(f"edit_text lỗi: {e}")

# ── RUNNERS ───────────────────────────────────────────────────────────────────

async def run_http_server():
    async def health(r): return web.Response(text="OK")
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"HTTP server :{PORT}")

def _make_userbot():
    """Tạo TelegramClient từ StringSession hoặc file session."""
    global userbot_client
    sess_str = SESSION_STRING.strip() if SESSION_STRING else ""
    if sess_str:
        try:
            StringSession(sess_str)   # validate
            logger.info("Dùng StringSession")
            userbot_client = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
            return True
        except Exception as e:
            logger.warning(f"StringSession lỗi ({e}), dùng file session")
    logger.info("Dùng file session")
    userbot_client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    return False

async def run_userbot():
    """Chạy userbot với auto-reconnect vô hạn."""
    global userbot_client
    retry = 0
    while True:
        try:
            used_string = _make_userbot()
            await userbot_client.start(phone=PHONE if not used_string else None)
            me = await userbot_client.get_me()
            logger.info(f"Userbot OK: {me.first_name} (@{me.username})")
            retry = 0  # reset sau khi kết nối thành công

            @userbot_client.on(events.NewMessage(from_users=FLUORITE_BOT))
            async def _new(event): await on_fluorite_new(event)

            @userbot_client.on(events.MessageEdited(from_users=FLUORITE_BOT))
            async def _edit(event): await on_fluorite_edit(event)

            await userbot_client.run_until_disconnected()
            logger.warning("Userbot bị disconnect — reconnect...")

        except Exception as e:
            retry += 1
            wait = min(30, 5 * retry)
            logger.error(f"Userbot lỗi (lần {retry}): {e} — thử lại sau {wait}s")
            try:
                await userbot_client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(wait)

async def run_main_bot():
    """Chạy main bot polling với auto-retry."""
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",     start_cmd))
    app.add_handler(CommandHandler("createkey", createkey_cmd))
    app.add_handler(CommandHandler("deletekey", deletekey_cmd))
    app.add_handler(CommandHandler("listkeys",  listkeys_cmd))
    app.add_handler(CommandHandler("checkkey",  checkkey_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.bot.delete_webhook(drop_pending_updates=True)

    # Polling loop với retry khi Conflict/network lỗi
    while True:
        try:
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
            )
            logger.info("Polling ✅")
            break
        except Conflict:
            logger.warning("Conflict — thử lại sau 15s (xóa bot cũ đi!)")
            await asyncio.sleep(15)
        except (NetworkError, TimedOut) as e:
            logger.warning(f"Network lỗi: {e} — thử lại sau 10s")
            await asyncio.sleep(10)

    await asyncio.Event().wait()

async def main():
    logger.info("=== Bot khởi động ===")
    await asyncio.gather(
        run_http_server(),
        run_userbot(),
        run_main_bot(),
    )

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("Dừng bot.")
            break
        except Exception as e:
            logger.critical(f"Main loop crash: {e} — restart sau 10s")
            import time; time.sleep(10)
