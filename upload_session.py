import os
import tempfile
import shutil
import asyncio
import logging
import json
import math
from datetime import datetime
from pathlib import Path
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, (
    Document, PhotoSize, Video, Audio, Voice, Animation, VideoNote, Sticker
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes
)
from github import Github, Auth
from github.GithubException import GithubException

# ==================== تنظیمات از متغیرهای محیطی ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
GH_TOKEN = os.getenv("GH_TOKEN")
REPO_NAME = os.getenv("REPO_NAME")  # "owner/repo"

# لاگینگ
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info("========== شروع جلسه ==========")
logger.info(f"BOT_TOKEN: {BOT_TOKEN[:5] if BOT_TOKEN else None}...{BOT_TOKEN[-5:] if BOT_TOKEN else None}")
logger.info(f"OWNER_ID: {OWNER_ID}")
logger.info(f"GH_TOKEN: {GH_TOKEN[:5] if GH_TOKEN else None}...{GH_TOKEN[-5:] if GH_TOKEN else None}")
logger.info(f"REPO_NAME: {REPO_NAME}")

if not all([BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME]):
    raise ValueError("❌ متغیرهای محیطی کامل نیستند!")

# اتصال به گیت‌هاب (با Auth جدید)
auth = Auth.Token(GH_TOKEN)
github = Github(auth=auth)
try:
    repo = github.get_repo(REPO_NAME)
    logger.info("✅ اتصال به گیت‌هاب موفق")
except Exception as e:
    logger.error(f"❌ خطا در اتصال: {e}")
    raise

# ==================== کلاس جلسه ====================
class Session:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="telegram_upload_")
        self.files = []          # {"name": str, "size": int, "local_path": str, "caption": str}
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
    logger.info(f"دانلود شد: {file_name} ({os.path.getsize(local_path)} bytes)")
    return local_path

def split_file(file_path, chunk_size=95*1024*1024):
    """تقسیم فایل به قطعات 95 مگابایتی"""
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
            logger.info(f"قطعه {i+1}/{num_parts}: {part_name}")
    # فایل متادیتا
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

async def upload_to_github(local_file_path, original_name, caption=None):
    """آپلود فایل (کوچک مستقیم، بزرگ پارت بندی) در پوشه اختصاصی"""
    # پوشه اختصاصی برای هر فایل: uploads/نام فایل (بدون پسوند)_زمان/
    base_name = os.path.splitext(original_name)[0]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"uploads/{base_name}_{timestamp}/"
    file_size = os.path.getsize(local_file_path)

    # اگر کپشن وجود دارد، یک فایل .caption.txt ذخیره کن
    caption_text = caption or ""
    caption_path = None
    if caption_text.strip():
        caption_path = os.path.join(os.path.dirname(local_file_path), f"{original_name}.caption.txt")
        with open(caption_path, "w", encoding="utf-8") as cf:
            cf.write(caption_text)
        logger.info(f"کپشن ذخیره شد: {caption_path}")

    uploaded_urls = []
    # آپلود فایل اصلی (یا قطعات)
    if file_size <= 100 * 1024 * 1024:
        remote_path = f"{folder_name}{original_name}"
        with open(local_file_path, "rb") as f:
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
        logger.info(f"آپلود شد (کوچک): {remote_path}")
    else:
        parts, manifest_path = split_file(local_file_path)
        # آپلود قطعات
        for part_path in parts:
            part_name = os.path.basename(part_path)
            remote_part = f"{folder_name}{part_name}"
            with open(part_path, "rb") as pf:
                content = pf.read()
            repo.create_file(remote_part, f"Upload {part_name}", content, branch="main")
            uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_part}")
            logger.info(f"آپلود قطعه: {remote_part}")
        # آپلود manifest
        with open(manifest_path, "rb") as mf:
            manifest_content = mf.read()
        remote_manifest = f"{folder_name}{os.path.basename(manifest_path)}"
        repo.create_file(remote_manifest, "Upload manifest", manifest_content, branch="main")
        uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_manifest}")
        logger.info(f"آپلود manifest: {remote_manifest}")

    # آپلود فایل caption اگر وجود داشت
    if caption_path:
        with open(caption_path, "rb") as cf:
            caption_content = cf.read()
        remote_caption = f"{folder_name}{original_name}.caption.txt"
        repo.create_file(remote_caption, "Upload caption", caption_content, branch="main")
        uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_caption}")
        logger.info(f"آپلود کپشن: {remote_caption}")

    return uploaded_urls

# ==================== تابع کمکی برای دریافت فایل از انواع مختلف ====================
async def get_file_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش هر نوع فایل (Document, Photo, Video, Audio, Voice, Animation, VideoNote, Sticker)"""
    user_id = update.effective_user.id
    logger.info(f"دریافت فایل از کاربر {user_id}")
    if user_id != OWNER_ID:
        await update.message.reply_text("⛔ شما اجازه آپلود ندارید.")
        logger.warning(f"کاربر {user_id} بدون مجوز فایل فرستاد (OWNER_ID={OWNER_ID})")
        return

    message = update.message
    caption = message.caption or ""

    # تشخیص نوع فایل
    file_obj = None
    file_name = None
    if message.document:
        file_obj = message.document
        file_name = file_obj.file_name
    elif message.video:
        file_obj = message.video
        file_name = f"{file_obj.file_unique_id}.mp4"
    elif message.audio:
        file_obj = message.audio
        file_name = file_obj.file_name or f"{file_obj.file_unique_id}.mp3"
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
    elif message.photo:
        # بزرگترین سایز عکس را بگیر
        file_obj = message.photo[-1]
        file_name = f"photo_{file_obj.file_unique_id}.jpg"
    else:
        await message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود.")
        return

    if not file_obj:
        return

    file_id = file_obj.file_id
    file_size = file_obj.file_size or 0
    logger.info(f"نوع: {type(file_obj).__name__}, نام: {file_name}, حجم: {file_size}")

    # محدودیت 2 گیگ (سقف API گیت‌هاب برای commit نیست، برای دانلود تلگرام)
    if file_size > 2 * 1024 * 1024 * 1024:
        await message.reply_text("❌ حجم فایل بیش از ۲ گیگابایت است (محدودیت تلگرام برای دانلود).")
        return

    # دانلود
    try:
        local_path = await download_file(file_id, file_name)
    except Exception as e:
        logger.error(f"خطا در دانلود: {e}")
        await message.reply_text(f"❌ خطا در دریافت فایل: {str(e)}")
        return

    session.files.append({
        "name": file_name,
        "size": file_size,
        "local_path": local_path,
        "caption": caption
    })

    # ساختن پیام وضعیت اگر لازم باشد
    if not session.status_message_id:
        session.chat_id = update.effective_chat.id
        msg = await message.reply_text("⚙️ در حال آماده‌سازی...")
        session.status_message_id = msg.message_id
        await update_status_message()
    else:
        await update_status_message()

    await message.delete()
    logger.info(f"✅ فایل اضافه شد. تعداد کل: {len(session.files)}")

# ==================== هندلرها ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return
    await update.message.reply_text(
        "🚀 **ربات آپلودر به گیت‌هاب**\n\n"
        "📌 **نحوه کار:**\n"
        "• هر فایل (عکس، ویدیو، صدا، سند، استیکر و...) را ارسال کنید.\n"
        "• کپشن فایل هم ذخیره می‌شود.\n"
        "• فایل‌های بزرگ‌تر از ۱۰۰ مگابایت **به قطعات ۹۵ مگابایتی** تقسیم می‌شوند.\n"
        "• برای هر فایل یک پوشه جداگانه در `uploads/` ساخته می‌شود.\n"
        "• با دکمه‌های زیر می‌توانید لیست را مدیریت کنید.\n\n"
        "⚠️ فقط خودتان اجازه دارید. پس از پایان، ربات خاموش می‌شود.",
        parse_mode="Markdown"
    )
    logger.info(f"/start از OWNER_ID {OWNER_ID}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != OWNER_ID:
        await query.edit_message_text("شما مجاز نیستید.")
        return

    data = query.data
    logger.info(f"دکمه: {data}")

    if data == "upload":
        if not session.files:
            await query.edit_message_text("هیچ فایلی برای آپلود نیست.")
            return
        await query.edit_message_text("🔄 آپلود به گیت‌هاب... (زمان متغیر)")
        all_results = []
        for idx, f in enumerate(session.files, 1):
            try:
                logger.info(f"آپلود {idx}/{len(session.files)}: {f['name']}")
                urls = await upload_to_github(f["local_path"], f["name"], f.get("caption", ""))
                size_str = get_file_size_str(f['size'])
                if len(urls) == 1:
                    all_results.append(f"✅ {f['name']} ({size_str})\n   🔗 [دانلود]({urls[0]})")
                else:
                    # فایل بزرگ + manifest
                    all_results.append(
                        f"✅ {f['name']} ({size_str}) - به {len(urls)-1} قطعه تقسیم شد.\n"
                        f"   📄 [manifest]({urls[-1]}) برای بازسازی"
                    )
            except Exception as e:
                logger.error(f"خطا در {f['name']}: {e}")
                all_results.append(f"❌ {f['name']} → {str(e)}")
        final = "**نتیجه آپلود:**\n\n" + "\n".join(all_results)
        await query.edit_message_text(final, parse_mode="Markdown", disable_web_page_preview=True)
        await finish_session()

    elif data == "cancel":
        await query.edit_message_text("❌ لغو شد. جلسه پایان یافت.")
        await finish_session()

    elif data == "remove_last":
        if session.files:
            removed = session.files.pop()
            try:
                os.remove(removed["local_path"])
                logger.info(f"حذف {removed['name']}")
            except:
                pass
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
        await query.answer("همه پاک شدند")

async def finish_session():
    logger.info("پایان جلسه - پاکسازی")
    shutil.rmtree(session.temp_dir, ignore_errors=True)
    if session.chat_id:
        await bot.send_message(chat_id=session.chat_id, text="👋 جلسه پایان یافت. ربات خاموش می‌شود.")
    await asyncio.sleep(1)
    # خروج از برنامه (برای GitHub Actions)
    os._exit(0)

async def idle_timeout():
    await asyncio.sleep(300)  # 5 دقیقه
    logger.warning("تایم‌اوت 5 دقیقه - خاتمه جلسه")
    if session.chat_id:
        await bot.send_message(chat_id=session.chat_id, text="⏰ عدم فعالیت به مدت ۵ دقیقه. جلسه خاتمه یافت.")
    await finish_session()

# ==================== اصلی ====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    # هندلر برای همه نوع فایل
    file_handler = MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE |
        filters.Document.ALL | filters.ANIMATION | filters.VIDEO_NOTE | filters.Sticker.ALL,
        get_file_info
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(file_handler)

    # تایم اوت در پس‌زمینه
    loop = asyncio.get_event_loop()
    loop.create_task(idle_timeout())

    # پیام خوشامدگویی خودکار به OWNER_ID
    async def send_startup_message():
        try:
            await app.bot.send_message(chat_id=OWNER_ID, text="🤖 ربات آپلودر فعال شد.\nفایل ارسال کنید یا /start")
        except Exception as e:
            logger.error(f"خطا در ارسال پیام اولیه: {e}")
    loop.create_task(send_startup_message())

    logger.info("شروع polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
