import os
import sys
import tempfile
import shutil
import asyncio
import logging
import json
import math
from datetime import datetime

# =========================== کتابخانه‌ها ===========================
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
    from github import Github, Auth
    from github.GithubException import GithubException
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.crypto import AuthKey
    from telethon.tl.types import InputMessagesFilterPhotoVideo, InputMessagesFilterDocument
except ImportError as e:
    print(f"❌ کتابخانه ناقص: {e}\nلطفاً با دستور زیر نصب کنید:\npip install python-telegram-bot PyGithub telethon")
    sys.exit(1)

# =========================== متغیرهای محیطی ===========================
BOT_TOKEN       = os.getenv("BOT_TOKEN")
OWNER_ID        = os.getenv("OWNER_ID")
GH_TOKEN        = os.getenv("GH_TOKEN")
REPO_NAME       = os.getenv("REPO_NAME")
API_ID          = os.getenv("API_ID")
API_HASH        = os.getenv("API_HASH")
DC_ID           = os.getenv("DC_ID")
AUTH_KEY_HEX    = os.getenv("AUTH_KEY_HEX")
USER_ID         = os.getenv("USER_ID")

try:
    OWNER_ID = int(OWNER_ID) if OWNER_ID else 0
    API_ID = int(API_ID) if API_ID else 0
    DC_ID = int(DC_ID) if DC_ID else 0
    USER_ID = int(USER_ID) if USER_ID else 0
except ValueError:
    OWNER_ID = API_ID = DC_ID = USER_ID = 0

# =========================== لاگینگ حرفه‌ای ===========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("TG_Uploader")

# بررسی متغیرهای اجباری
missing = []
if not BOT_TOKEN: missing.append("BOT_TOKEN")
if not OWNER_ID: missing.append("OWNER_ID")
if not GH_TOKEN: missing.append("GH_TOKEN")
if not REPO_NAME: missing.append("REPO_NAME")
if not API_ID: missing.append("API_ID")
if not API_HASH: missing.append("API_HASH")
if not DC_ID: missing.append("DC_ID")
if not AUTH_KEY_HEX: missing.append("AUTH_KEY_HEX")
if not USER_ID: missing.append("USER_ID")
if missing:
    raise ValueError(f"❌ متغیرهای محیطی گم شده: {', '.join(missing)}")

logger.info(f"✅ متغیرها تأیید شدند | OWNER: {OWNER_ID} | REPO: {REPO_NAME} | DC: {DC_ID} | USER: {USER_ID}")

# =========================== اتصال به گیت‌هاب ===========================
try:
    auth = Auth.Token(GH_TOKEN)
    github = Github(auth=auth)
    repo = github.get_repo(REPO_NAME)
    logger.info(f"✅ متصل به مخزن گیت‌هاب: {REPO_NAME}")
except Exception as e:
    logger.error(f"❌ خطای اتصال به گیت‌هاب: {e}")
    raise

# =========================== کلاس مدیریت نشست ===========================
class Session:
    def __init__(self):
        self.temp_dir = None
        self.files = []
        self.status_msg_id = None
        self.chat_id = None
        self.idle_task = None
        self.app = None
        self.userbot = None
        self.bot_username = None

session = Session()

# =========================== توابع کمکی ===========================
def size_str(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

async def update_status(bot):
    if not session.status_msg_id or not session.chat_id:
        return
    text = "📂 **فایل‌های آماده**\n\n"
    if not session.files:
        text += "هیچ فایلی اضافه نشده است."
    else:
        for i, f in enumerate(session.files, 1):
            text += f"{i}. {f['name']} ({size_str(f['size'])})\n"
    text += f"\nتعداد: {len(session.files)}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 آپلود همه", callback_data="upload")],
        [InlineKeyboardButton("❌ لغو و پایان", callback_data="cancel")],
        [InlineKeyboardButton("🗑 حذف آخرین", callback_data="remove_last"),
         InlineKeyboardButton("🧹 پاک کردن همه", callback_data="clear_all")]
    ])
    try:
        await bot.edit_message_text(text, session.chat_id, session.status_msg_id,
                                    parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"خطا در به‌روزرسانی وضعیت: {e}")

async def reset_idle():
    if session.idle_task:
        session.idle_task.cancel()
    session.idle_task = asyncio.create_task(idle_timeout())

async def idle_timeout():
    await asyncio.sleep(300)
    logger.warning("⏰ تایم‌اوت بی‌کاری - پایان نشست")
    await finish(send_message=True)

async def finish(send_message: bool = True):
    if session.idle_task:
        session.idle_task.cancel()
    if session.temp_dir and os.path.exists(session.temp_dir):
        shutil.rmtree(session.temp_dir, ignore_errors=True)
        session.temp_dir = None
    session.files.clear()
    session.status_msg_id = None
    session.chat_id = None
    if send_message and session.app and OWNER_ID:
        try:
            await session.app.bot.send_message(OWNER_ID, "👋 نشست پایان یافت. برای شروع مجدد /start را بزنید.")
        except Exception as e:
            logger.error(f"خطا در ارسال پیام خاتمه: {e}")
    if session.userbot:
        await session.userbot.disconnect()
        logger.info("یوزربات قطع شد.")

# =========================== دانلود فایل‌ها ===========================
async def download_small_file(file_id: str, name: str, bot):
    path = os.path.join(session.temp_dir, name)
    file = await bot.get_file(file_id)
    await file.download_to_drive(path)
    logger.info(f"✅ دانلود با Bot API: {name} ({size_str(os.path.getsize(path))})")
    return path

async def download_large_file(name: str):
    """
    دریافت آخرین فایل ارسالی توسط خود کاربر (اکانت اصلی) به ربات
    با استفاده از Telethon - بدون وابستگی به message_id
    """
    if not session.userbot or not session.bot_username:
        raise Exception("یوزربات یا نام کاربری ربات مقداردهی نشده است.")
    path = os.path.join(session.temp_dir, name)

    try:
        # ترکیب دو فیلتر برای پشتیبانی از عکس، ویدیو و اسناد
        found_media = None
        for filter_type in (InputMessagesFilterPhotoVideo, InputMessagesFilterDocument):
            async for msg in session.userbot.iter_messages(
                session.bot_username,
                from_user='me',
                filter=filter_type,
                limit=1
            ):
                if msg and msg.media:
                    found_media = msg
                    break
            if found_media:
                break

        # Fallback: اگر چیزی پیدا نشد، ۵ پیام آخر را دستی بررسی کن
        if not found_media:
            async for msg in session.userbot.iter_messages(
                session.bot_username,
                from_user='me',
                limit=5
            ):
                if msg and msg.media:
                    found_media = msg
                    break

        if not found_media:
            raise Exception("هیچ فایل رسانه‌ای از شما در چت با ربات یافت نشد. لطفاً ابتدا فایل را ارسال کنید.")

        await found_media.download_media(file=path)
        logger.info(f"✅ دانلود با یوزربات موفق: {name} ({size_str(os.path.getsize(path))})")
        return path

    except Exception as e:
        logger.error(f"❌ خطا در دانلود با یوزربات: {e}", exc_info=True)
        raise

# =========================== آپلود در گیت‌هاب (با تقسیم فایل‌های بزرگ) ===========================
def split_large_file(path: str, chunk: int = 95 * 1024 * 1024):
    size = os.path.getsize(path)
    num = math.ceil(size / chunk)
    parts = []
    base = os.path.basename(path)
    with open(path, "rb") as src:
        for i in range(num):
            part_name = f"{base}.part{i+1:03d}"
            part_path = os.path.join(os.path.dirname(path), part_name)
            with open(part_path, "wb") as dst:
                dst.write(src.read(chunk))
            parts.append(part_path)
    manifest = {
        "original": base,
        "size": size,
        "chunk": chunk,
        "parts": [os.path.basename(p) for p in parts],
        "recombine_linux": f"cat {base}.part* > {base}",
        "recombine_windows": f"copy /b {base}.part* {base}"
    }
    man_path = os.path.join(os.path.dirname(path), f"{base}.manifest.json")
    with open(man_path, "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2)
    return parts, man_path

async def upload_to_github(local_path: str, orig_name: str, caption_text: str = ""):
    base = os.path.splitext(orig_name)[0]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = f"uploads/{base}_{ts}/"
    size = os.path.getsize(local_path)
    urls = []

    cap_path = None
    if caption_text.strip():
        cap_path = os.path.join(os.path.dirname(local_path), f"{orig_name}.caption.txt")
        with open(cap_path, "w", encoding="utf-8") as cp:
            cp.write(caption_text)

    if size <= 100 * 1024 * 1024:
        remote = f"{folder}{orig_name}"
        with open(local_path, "rb") as f:
            data = f.read()
        try:
            repo.create_file(remote, f"Upload {orig_name}", data, branch="main")
        except GithubException as e:
            if e.status == 409:
                contents = repo.get_contents(remote)
                repo.update_file(remote, f"Update {orig_name}", data, contents.sha, branch="main")
            else:
                raise
        urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote}")
        logger.info(f"✅ آپلود کامل: {remote}")
    else:
        parts, man_path = split_large_file(local_path)
        for part in parts:
            pname = os.path.basename(part)
            remote_part = f"{folder}{pname}"
            with open(part, "rb") as pf:
                data = pf.read()
            try:
                repo.create_file(remote_part, f"Upload {pname}", data, branch="main")
            except GithubException as e:
                if e.status == 409:
                    contents = repo.get_contents(remote_part)
                    repo.update_file(remote_part, f"Update {pname}", data, contents.sha, branch="main")
                else:
                    raise
            urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_part}")
            logger.info(f"✅ آپلود قطعه: {remote_part}")
        with open(man_path, "rb") as mf:
            man_data = mf.read()
        remote_man = f"{folder}{os.path.basename(man_path)}"
        try:
            repo.create_file(remote_man, "Upload manifest", man_data, branch="main")
        except GithubException as e:
            if e.status == 409:
                contents = repo.get_contents(remote_man)
                repo.update_file(remote_man, "Update manifest", man_data, contents.sha, branch="main")
            else:
                raise
        urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_man}")
        logger.info("✅ منیفست آپلود شد")

    if cap_path:
        remote_cap = f"{folder}{orig_name}.caption.txt"
        with open(cap_path, "rb") as cf:
            cap_data = cf.read()
        try:
            repo.create_file(remote_cap, "Upload caption", cap_data, branch="main")
        except GithubException as e:
            if e.status == 409:
                contents = repo.get_contents(remote_cap)
                repo.update_file(remote_cap, "Update caption", cap_data, contents.sha, branch="main")
            else:
                raise
        urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_cap}")
        logger.info("✅ کپشن آپلود شد")

    return urls

# =========================== هندلرهای ربات ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی غیرمجاز.")
        return
    if session.chat_id is not None:
        await finish(send_message=False)
    if session.temp_dir is None:
        session.temp_dir = tempfile.mkdtemp(prefix="tg_upload_")
    session.chat_id = update.effective_chat.id
    await update.message.reply_text(
        "🚀 **آپلودر تلگرام → گیت‌هاب**\n\n"
        "✅ فایل خود را ارسال کنید (عکس، ویدیو، سند، صدا، استیکر و...)\n"
        "🔹 فایل‌های بزرگتر از ۲۰ مگابایت با **یوزربات** دانلود می‌شوند.\n"
        "🔹 فایل‌های بزرگتر از ۱۰۰ مگابایت به قطعات ۹۵ مگابایتی تقسیم می‌شوند.\n"
        "🔹 از دکمه‌های زیر برای مدیریت و آپلود استفاده کنید.\n\n"
        "_فقط شما مجاز به استفاده هستید._",
        parse_mode="Markdown"
    )
    await reset_idle()

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی غیرمجاز.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("لطفاً در چت خصوصی از ربات استفاده کنید.")
        return

    if session.chat_id is None or session.temp_dir is None:
        await start(update, context)
        await asyncio.sleep(0.5)

    await reset_idle()

    msg = update.message
    caption = msg.caption or ""
    file_obj = None
    fname = None

    if msg.document:
        file_obj = msg.document
        fname = file_obj.file_name or f"doc_{file_obj.file_unique_id}.bin"
    elif msg.photo:
        file_obj = msg.photo[-1]
        fname = f"photo_{file_obj.file_unique_id}.jpg"
    elif msg.video:
        file_obj = msg.video
        fname = file_obj.file_name or f"video_{file_obj.file_unique_id}.mp4"
    elif msg.audio:
        file_obj = msg.audio
        fname = file_obj.file_name or f"audio_{file_obj.file_unique_id}.mp3"
    elif msg.voice:
        file_obj = msg.voice
        fname = f"voice_{file_obj.file_unique_id}.ogg"
    elif msg.animation:
        file_obj = msg.animation
        fname = f"anim_{file_obj.file_unique_id}.mp4"
    elif msg.video_note:
        file_obj = msg.video_note
        fname = f"videonote_{file_obj.file_unique_id}.mp4"
    elif msg.sticker:
        file_obj = msg.sticker
        fname = f"sticker_{file_obj.file_unique_id}.webp"
    else:
        await msg.reply_text("❌ نوع فایل پشتیبانی نمی‌شود.")
        return

    file_size = file_obj.file_size or 0
    if file_size > 2 * 1024 * 1024 * 1024:
        await msg.reply_text("❌ حجم فایل بیشتر از ۲ گیگابایت است. پشتیبانی نمی‌شود.")
        return

    if session.temp_dir is None:
        session.temp_dir = tempfile.mkdtemp(prefix="tg_upload_")
    session.chat_id = msg.chat_id

    try:
        if file_size > 20 * 1024 * 1024:
            logger.info(f"📥 فایل بزرگ ({size_str(file_size)}) - دانلود با یوزربات...")
            local_path = await download_large_file(fname)
        else:
            logger.info(f"📥 فایل کوچک ({size_str(file_size)}) - دانلود با Bot API...")
            local_path = await download_small_file(file_obj.file_id, fname, context.bot)
    except Exception as e:
        logger.error(f"❌ خطا در دانلود {fname}: {e}", exc_info=True)
        await msg.reply_text(f"❌ دانلود ناموفق: {str(e)}")
        return

    session.files.append({
        "name": fname,
        "size": file_size,
        "local_path": local_path,
        "caption": caption
    })

    if not session.status_msg_id:
        status_msg = await msg.reply_text("⚙️ در حال آماده‌سازی...")
        session.status_msg_id = status_msg.message_id
        await update_status(context.bot)
    else:
        await update_status(context.bot)

    try:
        await msg.react(emoji="📥")
    except:
        pass

    logger.info(f"✅ فایل اضافه شد: {fname} ({size_str(file_size)})")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # پاسخ به callback_query با مدیریت خطای منقضی شدن
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Callback answer timeout/expired: {e}")

    user = update.effective_user
    if user.id != OWNER_ID:
        await query.edit_message_text("⛔ دسترسی غیرمجاز.")
        return

    await reset_idle()
    data = query.data

    if data == "upload":
        if not session.files:
            await query.edit_message_text("❌ هیچ فایلی برای آپلود وجود ندارد.")
            return
        await query.edit_message_text("🔄 در حال آپلود به گیت‌هاب... (ممکن است چند لحظه طول بکشد)")
        results = []
        for f in session.files:
            try:
                urls = await upload_to_github(f["local_path"], f["name"], f.get("caption", ""))
                if len(urls) == 1:
                    results.append(f"✅ {f['name']} → [دانلود فایل]({urls[0]})")
                else:
                    results.append(f"✅ {f['name']} → به {len(urls)-1} قطعه تقسیم شد. [منیفست]({urls[-1]})")
                logger.info(f"✅ آپلود موفق: {f['name']}")
            except Exception as e:
                logger.error(f"❌ خطا در آپلود {f['name']}: {e}", exc_info=True)
                results.append(f"❌ {f['name']} – خطا: {str(e)}")
        final = "**نتیجهٔ آپلود:**\n\n" + "\n".join(results)
        await query.edit_message_text(final, parse_mode="Markdown", disable_web_page_preview=True)
        await finish(send_message=True)

    elif data == "cancel":
        await query.edit_message_text("❌ عملیات لغو شد. نشست پایان یافت.")
        await finish(send_message=True)

    elif data == "remove_last":
        if session.files:
            removed = session.files.pop()
            if os.path.exists(removed["local_path"]):
                os.remove(removed["local_path"])
            await update_status(context.bot)
        else:
            await query.answer("لیست فایل‌ها خالی است", show_alert=True)

    elif data == "clear_all":
        for f in session.files:
            if os.path.exists(f["local_path"]):
                os.remove(f["local_path"])
        session.files.clear()
        await update_status(context.bot)
        await query.answer("همهٔ فایل‌ها حذف شدند", show_alert=True)

# =========================== راه‌اندازی یوزربات ===========================
async def post_init(app: Application):
    session.app = app
    logger.info("🔌 راه‌اندازی یوزربات Telethon...")
    mem = MemorySession()
    mem.set_dc(DC_ID, '149.154.175.59', 443)
    mem.auth_key = AuthKey(data=bytes.fromhex(AUTH_KEY_HEX))
    mem.user_id = USER_ID

    userbot = TelegramClient(mem, API_ID, API_HASH)
    await userbot.connect()
    session.userbot = userbot

    if not await userbot.is_user_authorized():
        raise ValueError("❌ یوزربات احراز هویت نشد. AUTH_KEY_HEX نامعتبر است.")
    me = await userbot.get_me()
    logger.info(f"✅ یوزربات متصل شد: {me.first_name} (@{me.username}) با شناسه {me.id}")
    if me.id != OWNER_ID:
        logger.warning(f"⚠️ توجه: شناسه یوزربات ({me.id}) با OWNER_ID ({OWNER_ID}) متفاوت است. مطمئن شوید از یک اکانت استفاده می‌کنید.")

    bot_info = await app.bot.get_me()
    if not bot_info.username:
        raise ValueError("❌ ربات یوزرنیم ندارد. لطفاً با @BotFather یک یوزرنیم تنظیم کنید.")
    session.bot_username = bot_info.username
    logger.info(f"✅ نام کاربری ربات: @{session.bot_username}")

    await app.bot.send_message(OWNER_ID, "🤖 ربات با موفقیت فعال شد.\nاز /start برای شروع استفاده کنید.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE |
        filters.Document.ALL | filters.ANIMATION | filters.VIDEO_NOTE |
        filters.Sticker.ALL,
        handle_file
    ))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("🚀 ربات در حال اجرا (Polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
