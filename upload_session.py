import os
import tempfile
import shutil
import asyncio
import logging
import json
import math
from datetime import datetime
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from github import Github, Auth
from github.GithubException import GithubException

# ==================== متغیرهای محیطی ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
GH_TOKEN = os.getenv("GH_TOKEN")
REPO_NAME = os.getenv("REPO_NAME")

# لاگینگ
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info("========== راه‌اندازی ربات ==========")
logger.info(f"BOT_TOKEN: {BOT_TOKEN[:5] if BOT_TOKEN else 'None'}...{BOT_TOKEN[-5:] if BOT_TOKEN else ''}")
logger.info(f"OWNER_ID: {OWNER_ID}")
logger.info(f"GH_TOKEN: {GH_TOKEN[:5] if GH_TOKEN else 'None'}...{GH_TOKEN[-5:] if GH_TOKEN else ''}")
logger.info(f"REPO_NAME: {REPO_NAME}")

if not all([BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME]):
    raise ValueError("❌ متغیرهای محیطی کامل نیستند!")

# ==================== اتصال به گیت‌هاب ====================
auth = Auth.Token(GH_TOKEN)
github = Github(auth=auth)
try:
    repo = github.get_repo(REPO_NAME)
    logger.info("✅ اتصال به گیت‌هاب موفقیت‌آمیز")
except Exception as e:
    logger.error(f"❌ خطا در اتصال به گیت‌هاب: {e}")
    raise

# ==================== کلاس جلسه ====================
class Session:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="telegram_upload_")
        self.files = []          # {"name", "size", "local_path", "caption"}
        self.status_message_id = None
        self.chat_id = None

session = Session()
bot = Bot(token=BOT_TOKEN)

# ==================== توابع کمکی ====================
def get_file_size_str(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

async def update_status_message():
    if not session.status_message_id or not session.chat_id:
        return
    text = "📂 **لیست فایل‌های آماده آپلود**\n\n"
    if not session.files:
        text += "هیچ فایلی ارسال نشده است.\n"
    else:
        for idx, f in enumerate(session.files, 1):
            text += f"{idx}. {f['name']} ({get_file_size_str(f['size'])})\n"
    text += f"\n📌 تعداد کل: {len(session.files)}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 آپلود به گیت‌هاب", callback_data="upload")],
        [InlineKeyboardButton("❌ لغو و خاموش شدن", callback_data="cancel")],
        [InlineKeyboardButton("🗑 حذف آخرین فایل", callback_data="remove_last"),
         InlineKeyboardButton("🧹 پاک کردن همه", callback_data="clear_all")]
    ])
    try:
        await bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=session.status_message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"خطا در بروزرسانی پیام: {e}")

async def download_file(file_id, file_name):
    local_path = os.path.join(session.temp_dir, file_name)
    file = await bot.get_file(file_id)
    await file.download_to_drive(local_path)
    logger.info(f"✅ دانلود شد: {file_name} ({os.path.getsize(local_path)} bytes)")
    return local_path

def split_file(file_path, chunk_size=95*1024*1024):
    file_size = os.path.getsize(file_path)
    num_parts = math.ceil(file_size / chunk_size)
    parts = []
    base_name = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        for i in range(num_parts):
            part_data = f.read(chunk_size)
            part_name = f"{base_name}.part{i+1:03d}"
            part_path = os.path.join(os.path.dirname(file_path), part_name)
            with open(part_path, "wb") as pf:
                pf.write(part_data)
            parts.append(part_path)
            logger.info(f"📦 قطعه {i+1}/{num_parts}: {part_name}")
    manifest = {
        "original_name": base_name,
        "original_size": file_size,
        "chunk_size": chunk_size,
        "num_parts": num_parts,
        "parts": [os.path.basename(p) for p in parts],
        "recombine_linux": f"cat {base_name}.part* > {base_name}",
        "recombine_windows": f"copy /b {base_name}.part001+{base_name}.part002 {base_name}"
    }
    manifest_path = os.path.join(os.path.dirname(file_path), f"{base_name}.manifest.json")
    with open(manifest_path, "w") as mf:
        json.dump(manifest, mf, indent=2)
    return parts, manifest_path

async def upload_to_github(local_path, original_name, caption=""):
    base_name = os.path.splitext(original_name)[0]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"uploads/{base_name}_{timestamp}/"
    file_size = os.path.getsize(local_path)

    if caption.strip():
        caption_file = os.path.join(os.path.dirname(local_path), f"{original_name}.caption.txt")
        with open(caption_file, "w", encoding="utf-8") as cf:
            cf.write(caption)

    uploaded_urls = []

    if file_size <= 100 * 1024 * 1024:
        remote_path = f"{folder_name}{original_name}"
        with open(local_path, "rb") as f:
            content = f.read()
        try:
            contents = repo.get_contents(remote_path)
            repo.update_file(contents.path, f"Update {original_name}", content, contents.sha, branch="main")
        except GithubException as e:
            if e.status == 404:
                repo.create_file(remote_path, f"Upload {original_name}", content, branch="main")
            else:
                raise
        uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_path}")
        logger.info(f"✅ آپلود شد (کوچک): {remote_path}")
    else:
        parts, manifest_path = split_file(local_path)
        for part_path in parts:
            part_name = os.path.basename(part_path)
            remote_part = f"{folder_name}{part_name}"
            with open(part_path, "rb") as pf:
                content = pf.read()
            repo.create_file(remote_part, f"Upload {part_name}", content, branch="main")
            uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_part}")
            logger.info(f"✅ آپلود قطعه: {remote_part}")
        with open(manifest_path, "rb") as mf:
            manifest_content = mf.read()
        remote_manifest = f"{folder_name}{os.path.basename(manifest_path)}"
        repo.create_file(remote_manifest, "Upload manifest", manifest_content, branch="main")
        uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_manifest}")
        logger.info(f"✅ آپلود manifest: {remote_manifest}")

    if caption.strip():
        remote_caption = f"{folder_name}{original_name}.caption.txt"
        with open(caption_file, "rb") as cf:
            caption_content = cf.read()
        repo.create_file(remote_caption, "Upload caption", caption_content, branch="main")
        uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_caption}")
        logger.info(f"✅ آپلود کپشن: {remote_caption}")

    return uploaded_urls

# ==================== هندلر اصلی برای دریافت هر نوع فایل ====================
async def handle_any_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"📩 دریافت پیام از کاربر {user.id} (نام: {user.first_name})")

    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ شما اجازه استفاده از این بات را ندارید.")
        logger.warning(f"کاربر {user.id} بدون مجوز فایل فرستاد")
        return

    message = update.message
    caption = message.caption or ""

    file_obj = None
    file_name = None

    if message.document:
        file_obj = message.document
        file_name = file_obj.file_name or f"document_{file_obj.file_unique_id}.bin"
    elif message.photo:
        file_obj = message.photo[-1]
        file_name = f"photo_{file_obj.file_unique_id}.jpg"
    elif message.video:
        file_obj = message.video
        file_name = file_obj.file_name or f"video_{file_obj.file_unique_id}.mp4"
    elif message.audio:
        file_obj = message.audio
        file_name = file_obj.file_name or f"audio_{file_obj.file_unique_id}.mp3"
    elif message.voice:
        file_obj = message.voice
        file_name = f"voice_{file_obj.file_unique_id}.ogg"
    elif message.animation:
        file_obj = message.animation
        file_name = f"animation_{file_obj.file_unique_id}.mp4"
    elif message.video_note:
        file_obj = message.video_note
        file_name = f"video_note_{file_obj.file_unique_id}.mp4"
    elif message.sticker:
        file_obj = message.sticker
        file_name = f"sticker_{file_obj.file_unique_id}.webp"
    else:
        await message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود.")
        return

    if not file_obj:
        return

    file_id = file_obj.file_id
    file_size = file_obj.file_size or 0
    logger.info(f"📁 نوع: {type(file_obj).__name__}, نام: {file_name}, حجم: {file_size} bytes")

    if file_size > 2 * 1024 * 1024 * 1024:
        await message.reply_text("❌ حجم فایل بیش از ۲ گیگابایت است (محدودیت سرورهای تلگرام).")
        return

    try:
        local_path = await download_file(file_id, file_name)
    except Exception as e:
        logger.error(f"❌ خطا در دانلود: {e}")
        await message.reply_text(f"❌ خطا در دریافت فایل: {str(e)}")
        return

    session.files.append({
        "name": file_name,
        "size": file_size,
        "local_path": local_path,
        "caption": caption
    })

    if not session.status_message_id:
        session.chat_id = update.effective_chat.id
        status_msg = await message.reply_text("⚙️ در حال آماده‌سازی...")
        session.status_message_id = status_msg.message_id
        await update_status_message()
        logger.info("✅ پیام وضعیت ساخته شد")
    else:
        await update_status_message()

    await message.delete()
    logger.info(f"✅ فایل {file_name} به لیست اضافه شد. تعداد کل: {len(session.files)}")

# ==================== هندلر دکمه‌ها ====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    if user.id != OWNER_ID:
        await query.edit_message_text("شما مجاز نیستید.")
        return

    data = query.data
    logger.info(f"🔘 دکمه فشرده شد: {data}")

    if data == "upload":
        if not session.files:
            await query.edit_message_text("هیچ فایلی برای آپلود وجود ندارد.")
            return
        await query.edit_message_text("🔄 در حال آپلود به گیت‌هاب... (ممکن است چند دقیقه طول بکشد)")
        results = []
        for idx, f in enumerate(session.files, 1):
            try:
                logger.info(f"⬆️ شروع آپلود {idx}/{len(session.files)}: {f['name']}")
                urls = await upload_to_github(f["local_path"], f["name"], f.get("caption", ""))
                if len(urls) == 1:
                    results.append(f"✅ {f['name']} → [دانلود]({urls[0]})")
                else:
                    results.append(f"✅ {f['name']} → به {len(urls)-1} قطعه تقسیم شد. [manifest]({urls[-1]})")
            except Exception as e:
                logger.error(f"❌ خطا در {f['name']}: {e}")
                results.append(f"❌ {f['name']} → {str(e)}")
        final_text = "**نتیجه آپلود:**\n\n" + "\n".join(results)
        await query.edit_message_text(final_text, parse_mode="Markdown", disable_web_page_preview=True)
        await finish_session()

    elif data == "cancel":
        await query.edit_message_text("❌ عملیات لغو شد. جلسه خاتمه یافت.")
        await finish_session()

    elif data == "remove_last":
        if session.files:
            removed = session.files.pop()
            try:
                os.remove(removed["local_path"])
                logger.info(f"🗑 حذف شد: {removed['name']}")
            except Exception as e:
                logger.error(f"خطا در حذف: {e}")
            await update_status_message()
        else:
            await query.answer("لیست خالی است")

    elif data == "clear_all":
        for f in session.files:
            try:
                os.remove(f["local_path"])
            except:
                pass
        session.files.clear()
        await update_status_message()
        await query.answer("همه فایل‌ها پاک شدند")
        logger.info("🧹 همه فایل‌ها پاک شدند")

# ==================== دستور /start ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return
    await update.message.reply_text(
        "🚀 **ربات آپلودر به گیت‌هاب**\n\n"
        "📌 **نحوه کار:**\n"
        "• هر فایلی (عکس، ویدیو، سند، صدا، استیکر، ویدئونوت و...) ارسال کنید.\n"
        "• کپشن فایل هم ذخیره می‌شود.\n"
        "• فایل‌های بزرگ >100MB به قطعات 95MB تقسیم می‌شوند.\n"
        "• برای هر فایل یک پوشه جداگانه در `uploads/` ساخته می‌شود.\n"
        "• با دکمه‌های زیر می‌توانید لیست را مدیریت کنید.\n\n"
        "⚠️ فقط شما اجازه دارید. پس از آپلود یا لغو، ربات خاموش می‌شود.",
        parse_mode="Markdown"
    )
    logger.info(f"📢 دستور /start از OWNER_ID {OWNER_ID}")

# ==================== پایان جلسه و تایم‌اوت ====================
async def finish_session():
    logger.info("🏁 پایان جلسه - پاکسازی فایل‌های موقت")
    shutil.rmtree(session.temp_dir, ignore_errors=True)
    if session.chat_id:
        try:
            await bot.send_message(chat_id=session.chat_id, text="👋 جلسه پایان یافت. ربات خاموش می‌شود.")
        except:
            pass
    os._exit(0)

async def idle_timeout():
    await asyncio.sleep(300)
    logger.warning("⏰ تایم‌اوت 5 دقیقه - خاتمه جلسه")
    if session.chat_id:
        try:
            await bot.send_message(chat_id=session.chat_id, text="⏰ عدم فعالیت به مدت ۵ دقیقه. جلسه خاتمه یافت.")
        except:
            pass
    await finish_session()

# ==================== تابع اصلی ====================
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE |
        filters.Document.ALL | filters.ANIMATION | filters.VIDEO_NOTE |
        filters.Sticker.ALL,
        handle_any_file
    ))
    app.add_handler(CallbackQueryHandler(button_callback))

    asyncio.create_task(idle_timeout())

    try:
        await app.bot.send_message(chat_id=OWNER_ID, text="🤖 ربات آپلودر فعال شد.\nفایل ارسال کنید یا /start را بزنید.")
        logger.info("✅ پیام فعال شدن به OWNER_ID ارسال شد")
    except Exception as e:
        logger.error(f"❌ خطا در ارسال پیام فعال شدن: {e}")

    logger.info("🚀 شروع polling...")
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
