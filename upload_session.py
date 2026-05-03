import os
import sys
import tempfile
import shutil
import asyncio
import logging
import json
import math
from datetime import datetime

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.crypto import AuthKey
    from telethon.tl.types import InputMessagesFilterPhotoVideo, InputMessagesFilterDocument
    import git
except ImportError as e:
    print(f"❌ کتابخانه ناقص: {e}\nلطفاً با دستور زیر نصب کنید:\npip install python-telegram-bot telethon GitPython")
    sys.exit(1)

# ========== متغیرهای محیطی ==========
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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("TG_Uploader")

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
    raise ValueError(f"❌ متغیرهای گم شده: {', '.join(missing)}")

logger.info(f"✅ OWNER: {OWNER_ID} | REPO: {REPO_NAME}")

# ========== کلاس نشست ==========
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
        self.is_active = False
        self.repo_dir = None

session = Session()

def size_str(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

async def update_status(bot):
    if not session.chat_id:
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
        if session.status_msg_id:
            await bot.edit_message_text(text, session.chat_id, session.status_msg_id,
                                        parse_mode="Markdown", reply_markup=keyboard)
        else:
            msg = await bot.send_message(session.chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
            session.status_msg_id = msg.message_id
    except Exception as e:
        logger.error(f"خطا در به‌روزرسانی: {e}")

async def reset_idle():
    if session.idle_task:
        session.idle_task.cancel()
    session.idle_task = asyncio.create_task(idle_timeout())

async def idle_timeout():
    await asyncio.sleep(900)  # 15 دقیقه
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
    session.is_active = False
    if send_message and session.app and OWNER_ID:
        try:
            await session.app.bot.send_message(OWNER_ID, "👋 نشست پایان یافت. /start برای شروع مجدد")
        except Exception as e:
            logger.error(f"خطا: {e}")
    if session.userbot and session.userbot.is_connected():
        await session.userbot.disconnect()
        logger.info("یوزربات قطع شد.")

# ========== دانلود فایل ==========
async def download_small_file(file_id: str, name: str, bot, progress_msg):
    path = os.path.join(session.temp_dir, name)
    file = await bot.get_file(file_id)
    await progress_msg.edit_text(f"📥 در حال دانلود {name}... (Bot API)")
    await file.download_to_drive(path)
    await progress_msg.delete()
    logger.info(f"✅ دانلود کوچک: {name} ({size_str(os.path.getsize(path))})")
    return path

async def download_large_file(name: str, progress_msg):
    if not session.userbot or not session.bot_username:
        raise Exception("یوزربات آماده نیست")
    path = os.path.join(session.temp_dir, name)

    await progress_msg.edit_text(f"🔍 در حال جستجوی فایل {name}...")
    found_media = None
    for filter_type in (InputMessagesFilterPhotoVideo, InputMessagesFilterDocument):
        async for msg in session.userbot.iter_messages(
            session.bot_username,
            from_user=OWNER_ID,
            filter=filter_type,
            limit=1
        ):
            if msg and msg.media:
                found_media = msg
                break
        if found_media:
            break
    if not found_media:
        raise Exception("فایل یافت نشد")

    # شروع دانلود با پیشرفت
    file_size = found_media.file.size
    last_percent = 0

    async def progress_callback(current, total):
        nonlocal last_percent
        percent = int((current / total) * 100)
        if percent > last_percent:
            last_percent = percent
            bar = "█" * (percent // 5) + "░" * (20 - (percent // 5))
            await progress_msg.edit_text(f"📥 دانلود {name}:\n`[{bar}] {percent}%`\n{size_str(current)} / {size_str(total)}",
                                         parse_mode="Markdown")

    await found_media.download_media(file=path, progress_callback=progress_callback)
    await progress_msg.delete()
    logger.info(f"✅ دانلود بزرگ: {name} ({size_str(os.path.getsize(path))})")
    return path

# ========== آپلود با Git ==========
async def upload_to_github_with_git(local_path: str, orig_name: str, caption_text: str = "", progress_msg=None):
    remote_url = f"https://{GH_TOKEN}@github.com/{REPO_NAME}.git"
    
    if session.repo_dir is None:
        session.repo_dir = os.path.join(session.temp_dir, "github_repo")
    
    # Clone یا pull مخزن
    if not os.path.exists(session.repo_dir):
        if progress_msg:
            await progress_msg.edit_text("📥 در حال clone مخزن گیت‌هاب...")
        repo = git.Repo.clone_from(remote_url, session.repo_dir, branch="main", depth=1)
    else:
        repo = git.Repo(session.repo_dir)
        repo.git.reset("--hard")
        repo.git.clean("-fd")
        if progress_msg:
            await progress_msg.edit_text("🔄 به‌روزرسانی مخزن محلی...")
        repo.remotes.origin.pull()

    # ساختار مرتب با تاریخ
    now = datetime.now()
    date_path = now.strftime("%Y/%m/%d")
    base_name = os.path.splitext(orig_name)[0]
    timestamp = now.strftime("%H%M%S")
    final_folder_name = f"{base_name}_{timestamp}"
    
    folder_in_repo = os.path.join(session.repo_dir, "uploads", date_path, final_folder_name)
    os.makedirs(folder_in_repo, exist_ok=True)

    # کپی فایل اصلی
    dest_file = os.path.join(folder_in_repo, orig_name)
    shutil.copy2(local_path, dest_file)

    # کپی کپشن
    if caption_text.strip():
        cap_path = os.path.join(folder_in_repo, f"{orig_name}.caption.txt")
        with open(cap_path, "w", encoding="utf-8") as cp:
            cp.write(caption_text)

    # Commit و Push
    if progress_msg:
        await progress_msg.edit_text(f"📤 در حال commit و push...")
    repo.index.add("*")
    commit_msg = f"Add {orig_name} at {now.strftime('%Y-%m-%d %H:%M:%S')}"
    repo.index.commit(commit_msg)
    
    # تلاش مجدد برای push
    for attempt in range(3):
        try:
            repo.remotes.origin.push()
            break
        except Exception as e:
            if attempt < 2:
                logger.warning(f"⚠️ خطا در push، تلاش مجدد {attempt+2}")
                await asyncio.sleep(3)
            else:
                raise

    relative_path = f"uploads/{date_path}/{final_folder_name}/{orig_name}"
    if progress_msg:
        await progress_msg.delete()
    return relative_path

# ========== هندلرهای ربات ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی غیرمجاز.")
        return
    if session.chat_id is not None:
        await finish(send_message=False)
    session.is_active = True
    if session.temp_dir is None:
        session.temp_dir = tempfile.mkdtemp(prefix="tg_upload_")
    session.chat_id = update.effective_chat.id
    await update.message.reply_text(
        "🚀 **آپلودر تلگرام → گیت‌هاب (ساختار مرتب)**\n\n"
        "✅ فایل خود را ارسال کنید.\n"
        "📂 ساختار ذخیره‌سازی: `uploads/سال/ماه/روز/نام فایل_زمان/`\n"
        "🔹 فایل‌های >۲۰MB با یوزربات دانلود می‌شوند.\n"
        "🔹 آپلود با Git (پایدار و بدون خطای 500)\n"
        "🔹 برای دانلود فایل‌ها، کل مخزن را به صورت ZIP از گیت‌هاب دریافت کنید.\n\n"
        "_فقط شما مجاز هستید._",
        parse_mode="Markdown"
    )
    await reset_idle()

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی غیرمجاز.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("لطفاً در چت خصوصی.")
        return

    if not session.is_active or session.chat_id is None:
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
        await msg.reply_text("❌ حجم بیشتر از ۲ گیگابایت.")
        return

    if session.temp_dir is None:
        session.temp_dir = tempfile.mkdtemp(prefix="tg_upload_")
    session.chat_id = msg.chat_id

    progress_msg = await msg.reply_text("⏳ آماده‌سازی...")

    try:
        if file_size > 20 * 1024 * 1024:
            logger.info(f"📥 فایل بزرگ ({size_str(file_size)}) - یوزربات")
            local_path = await download_large_file(fname, progress_msg)
        else:
            logger.info(f"📥 فایل کوچک ({size_str(file_size)}) - Bot API")
            local_path = await download_small_file(file_obj.file_id, fname, context.bot, progress_msg)
    except Exception as e:
        logger.error(f"❌ خطا در دانلود {fname}: {e}", exc_info=True)
        await progress_msg.edit_text(f"❌ دانلود ناموفق: {str(e)}")
        return

    session.files.append({
        "name": fname,
        "size": file_size,
        "local_path": local_path,
        "caption": caption
    })

    await update_status(context.bot)

    try:
        await msg.react(emoji="📥")
    except:
        pass

    logger.info(f"✅ فایل اضافه شد: {fname} ({size_str(file_size)})")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass

    user = update.effective_user
    if user.id != OWNER_ID:
        await query.edit_message_text("⛔ دسترسی غیرمجاز.")
        return

    await reset_idle()
    data = query.data

    if data == "upload":
        if not session.files:
            await query.edit_message_text("❌ هیچ فایلی برای آپلود نیست.")
            return
        await query.edit_message_text("🔄 در حال آپلود به گیت‌هاب... (با Git)")
        results = []
        for f in session.files:
            progress_msg = await context.bot.send_message(session.chat_id, f"🔄 شروع آپلود {f['name']}...")
            try:
                relative_path = await upload_to_github_with_git(f["local_path"], f["name"], f.get("caption", ""), progress_msg)
                results.append(f"✅ {f['name']} → ذخیره شد در: `{relative_path}`")
                logger.info(f"✅ آپلود موفق: {f['name']} -> {relative_path}")
            except Exception as e:
                logger.error(f"❌ خطا در آپلود {f['name']}: {e}", exc_info=True)
                results.append(f"❌ {f['name']} – خطا: {str(e)}")
                await progress_msg.edit_text(f"❌ خطا در آپلود {f['name']}")
        final = "**نتیجهٔ آپلود:**\n\n" + "\n".join(results) + "\n\n📌 **نحوه دانلود:** به مخزن گیت‌هاب بروید، روی دکمه `Code` کلیک کرده و `Download ZIP` را انتخاب کنید. سپس فایل مورد نظر را از داخل پوشه uploads استخراج کنید."
        await query.edit_message_text(final, parse_mode="Markdown", disable_web_page_preview=True)
        await finish(send_message=True)

    elif data == "cancel":
        await query.edit_message_text("❌ لغو شد. نشست پایان یافت.")
        await finish(send_message=True)

    elif data == "remove_last":
        if session.files:
            removed = session.files.pop()
            if os.path.exists(removed["local_path"]):
                os.remove(removed["local_path"])
            await update_status(context.bot)
        else:
            await query.answer("لیست خالی", show_alert=True)

    elif data == "clear_all":
        for f in session.files:
            if os.path.exists(f["local_path"]):
                os.remove(f["local_path"])
        session.files.clear()
        await update_status(context.bot)
        await query.answer("همه پاک شدند", show_alert=True)

# ========== راه‌اندازی یوزربات ==========
async def post_init(app: Application):
    session.app = app
    logger.info("🔌 راه‌اندازی یوزربات...")
    mem = MemorySession()
    mem.set_dc(DC_ID, '149.154.175.59', 443)
    mem.auth_key = AuthKey(data=bytes.fromhex(AUTH_KEY_HEX))
    mem.user_id = USER_ID

    userbot = TelegramClient(mem, API_ID, API_HASH)
    await userbot.connect()
    session.userbot = userbot

    if not await userbot.is_user_authorized():
        raise ValueError("❌ یوزربات احراز هویت نشد.")
    me = await userbot.get_me()
    logger.info(f"✅ یوزربات متصل: {me.first_name} (@{me.username}) ID: {me.id}")
    if me.id != OWNER_ID:
        logger.warning(f"⚠️ شناسه یوزربات ({me.id}) با OWNER_ID ({OWNER_ID}) متفاوت است!")

    bot_info = await app.bot.get_me()
    if not bot_info.username:
        raise ValueError("❌ ربات یوزرنیم ندارد.")
    session.bot_username = bot_info.username
    logger.info(f"✅ ربات @{session.bot_username} آماده است.")

    await app.bot.send_message(OWNER_ID, "🤖 ربات فعال شد. /start")

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

    logger.info("🚀 ربات در حال اجرا...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
