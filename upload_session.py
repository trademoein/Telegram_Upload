import os
import sys
import tempfile
import shutil
import asyncio
import logging
import json
import math
from datetime import datetime

# ========== 1. کتابخانه‌های خارجی ==========
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
    from github import Github, Auth
    from github.GithubException import GithubException
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.crypto import AuthKey
except ImportError as e:
    print("❌ کتابخانه‌ها نصب نیستند:", e)
    sys.exit(1)

# ========== 2. متغیرهای محیطی ==========
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

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info("========== بررسی متغیرهای محیطی ==========")
logger.info(f"BOT_TOKEN: {'OK' if BOT_TOKEN else 'Missing'}")
logger.info(f"OWNER_ID: {OWNER_ID}")
logger.info(f"GH_TOKEN: {'OK' if GH_TOKEN else 'Missing'}")
logger.info(f"REPO_NAME: {REPO_NAME}")
logger.info(f"API_ID: {API_ID}")
logger.info(f"API_HASH: {'OK' if API_HASH else 'Missing'}")
logger.info(f"DC_ID: {DC_ID}")
logger.info(f"AUTH_KEY_HEX: {'OK' if AUTH_KEY_HEX else 'Missing'}")
logger.info(f"USER_ID: {USER_ID}")

if not all([BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME, API_ID, API_HASH, DC_ID, AUTH_KEY_HEX, USER_ID]):
    raise ValueError("❌ متغیرهای محیطی کامل نیستند!")

# ========== 3. اتصال به گیت‌هاب ==========
try:
    auth = Auth.Token(GH_TOKEN)
    github = Github(auth=auth)
    repo = github.get_repo(REPO_NAME)
    logger.info("✅ Connected to GitHub")
except Exception as e:
    logger.error(f"GitHub connection error: {e}")
    raise

# ========== 4. کلاس جلسه ==========
class Session:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="tg_upload_")
        self.files = []
        self.status_msg_id = None
        self.chat_id = None
        self.last_message_id = None
        self.idle_task = None
        self.app = None
        self.userbot = None

session = Session()

# ========== 5. توابع کمکی ==========
def size_str(size):
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
        text += "هیچ فایلی نداریم."
    else:
        for i, f in enumerate(session.files, 1):
            text += f"{i}. {f['name']} ({size_str(f['size'])})\n"
    text += f"\nتعداد: {len(session.files)}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 آپلود", callback_data="upload")],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel")],
        [InlineKeyboardButton("🗑 حذف آخرین", callback_data="remove_last"),
         InlineKeyboardButton("🧹 پاک کردن همه", callback_data="clear_all")]
    ])
    try:
        await bot.edit_message_text(text, session.chat_id, session.status_msg_id,
                                    parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"update_status error: {e}")

async def download_small_file(file_id, name, bot):
    path = os.path.join(session.temp_dir, name)
    f = await bot.get_file(file_id)
    await f.download_to_drive(path)
    logger.info(f"Bot API downloaded: {name} ({os.path.getsize(path)} B)")
    return path

async def download_large_file(name):
    if not session.userbot:
        raise Exception("Userbot client not initialized")
    path = os.path.join(session.temp_dir, name)
    message = await session.userbot.get_messages(session.chat_id, ids=session.last_message_id)
    if not message:
        raise Exception("Message not found via userbot")
    await message.download_media(file=path)
    logger.info(f"Userbot downloaded: {name} ({os.path.getsize(path)} B)")
    return path

def split_large_file(path, chunk=95*1024*1024):
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
            logger.info(f"Part {i+1}/{num}: {part_name}")
    manifest = {
        "original": base,
        "size": size,
        "chunk": chunk,
        "parts": [os.path.basename(p) for p in parts],
        "recombine": f"cat {base}.part* > {base}"
    }
    man_path = os.path.join(os.path.dirname(path), f"{base}.manifest.json")
    with open(man_path, "w") as mf:
        json.dump(manifest, mf, indent=2)
    return parts, man_path

async def upload_to_github(local_path, orig_name, caption_text=""):
    base = os.path.splitext(orig_name)[0]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = f"uploads/{base}_{ts}/"
    size = os.path.getsize(local_path)

    cap_path = None
    if caption_text.strip():
        cap_path = os.path.join(os.path.dirname(local_path), f"{orig_name}.caption.txt")
        with open(cap_path, "w", encoding="utf-8") as cp:
            cp.write(caption_text)

    urls = []

    if size <= 100*1024*1024:
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
        logger.info(f"Uploaded: {remote}")
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
            logger.info(f"Uploaded part: {remote_part}")
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
        logger.info("Uploaded manifest")

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
        logger.info("Uploaded caption")

    return urls

async def idle_timeout():
    await asyncio.sleep(300)
    logger.warning("Idle timeout reached")
    if session.chat_id and session.app:
        try:
            await session.app.bot.send_message(session.chat_id, "⏰ 5 minutes idle. Goodbye.")
        except:
            pass
    await finish()

async def reset_idle_timer():
    if session.idle_task:
        session.idle_task.cancel()
    session.idle_task = asyncio.create_task(idle_timeout())

async def finish():
    logger.info("Finishing session")
    if session.idle_task:
        session.idle_task.cancel()
    shutil.rmtree(session.temp_dir, ignore_errors=True)
    if session.chat_id and session.app:
        try:
            await session.app.bot.send_message(session.chat_id, "👋 Session ended. Bot stopped.")
        except:
            pass
    if session.userbot:
        await session.userbot.disconnect()
    if session.app:
        await session.app.stop()

# ========== 6. هندلرها ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "🚀 **Telegram → GitHub Uploader**\n\n"
        "Send me any file (photo, video, document, audio, sticker...).\n"
        "Files >100MB will be split into 95MB parts.\n"
        "Large files (>20MB) are downloaded via userbot (Telethon).\n\n"
        "Use the buttons below to manage and upload.\n"
        "_Only you can use this bot._",
        parse_mode="Markdown"
    )
    if not session.idle_task:
        await reset_idle_timer()

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ Access denied.")
        return

    await reset_idle_timer()

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
        await msg.reply_text("Unsupported file type.")
        return

    if not file_obj:
        return

    file_size = file_obj.file_size or 0
    if file_size > 2*1024*1024*1024:
        await msg.reply_text("File >2GB not supported.")
        return

    session.chat_id = msg.chat_id
    session.last_message_id = msg.message_id

    if file_size > 20 * 1024 * 1024:
        logger.info(f"Large file ({size_str(file_size)}), using userbot")
        try:
            local_path = await download_large_file(fname)
        except Exception as e:
            logger.error(f"Userbot download error: {e}")
            await msg.reply_text(f"❌ Download failed (large file): {e}")
            return
    else:
        try:
            local_path = await download_small_file(file_obj.file_id, fname, context.bot)
        except Exception as e:
            logger.error(f"Bot API download error: {e}")
            await msg.reply_text(f"❌ Download failed: {e}")
            return

    session.files.append({
        "name": fname,
        "size": file_size,
        "local_path": local_path,
        "caption": caption
    })

    if not session.status_msg_id:
        status_msg = await msg.reply_text("⚙️ Preparing...")
        session.status_msg_id = status_msg.message_id
        await update_status(context.bot)
    else:
        await update_status(context.bot)

    try:
        await msg.react(emoji="📥")
    except:
        pass

    logger.info(f"Added file: {fname}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user.id != OWNER_ID:
        await query.edit_message_text("Access denied.")
        return

    await reset_idle_timer()
    data = query.data

    if data == "upload":
        if not session.files:
            await query.edit_message_text("No files to upload.")
            return
        await query.edit_message_text("🔄 Uploading to GitHub... (this may take a while)")
        results = []
        for f in session.files:
            try:
                urls = await upload_to_github(f["local_path"], f["name"], f.get("caption", ""))
                if len(urls) == 1:
                    results.append(f"✅ {f['name']} → [file]({urls[0]})")
                else:
                    results.append(f"✅ {f['name']} → split into {len(urls)-1} parts. [manifest]({urls[-1]})")
            except Exception as e:
                logger.error(f"Upload error: {f['name']} – {e}")
                results.append(f"❌ {f['name']} – {str(e)}")
        final = "**Result:**\n\n" + "\n".join(results)
        await query.edit_message_text(final, parse_mode="Markdown", disable_web_page_preview=True)
        await finish()

    elif data == "cancel":
        await query.edit_message_text("Cancelled.")
        await finish()

    elif data == "remove_last":
        if session.files:
            removed = session.files.pop()
            try:
                os.remove(removed["local_path"])
            except:
                pass
            await update_status(context.bot)
        else:
            await query.answer("List empty")

    elif data == "clear_all":
        for f in session.files:
            try:
                os.remove(f["local_path"])
            except:
                pass
        session.files.clear()
        await update_status(context.bot)
        await query.answer("All cleared")

# ========== 7. راه‌اندازی یوزربات با MemorySession و AuthKey ==========
async def post_init(app: Application):
    await app.bot.send_message(OWNER_ID, "🤖 Bot is active. Send me files or use /start")

async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    session.app = app

    # MemorySession + AuthKey (اصلاح‌شده)
    mem = MemorySession()
    mem.set_dc(DC_ID, '149.154.175.59', 443)
    mem.auth_key = AuthKey(data=bytes.fromhex(AUTH_KEY_HEX))
    mem.user_id = USER_ID

    userbot = TelegramClient(mem, API_ID, API_HASH)
    await userbot.connect()
    session.userbot = userbot
    logger.info("✅ Telethon userbot started (MemorySession + AuthKey)")

    app.post_init = post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE |
        filters.Document.ALL | filters.ANIMATION | filters.VIDEO_NOTE |
        filters.Sticker.ALL,
        handle_file
    ))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Starting polling...")
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
