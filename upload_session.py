import os
import tempfile
import shutil
import asyncio
import logging
import json
import math
from datetime import datetime
from pathlib import Path
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from github import Github
from github.GithubException import GithubException

# ==================== تنظیمات از متغیرهای محیطی ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
GH_TOKEN = os.getenv("GH_TOKEN")
REPO_NAME = os.getenv("REPO_NAME")      # "owner/repo"

# راه‌اندازی لاگینگ دقیق
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info("========== شروع جلسه آپلودر ==========")
logger.info(f"BOT_TOKEN: {BOT_TOKEN[:6]}...{BOT_TOKEN[-6:] if BOT_TOKEN else 'None'}")
logger.info(f"OWNER_ID: {OWNER_ID}")
logger.info(f"GH_TOKEN: {GH_TOKEN[:6]}...{GH_TOKEN[-6:] if GH_TOKEN else 'None'}")
logger.info(f"REPO_NAME: {REPO_NAME}")

if not all([BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME]):
    raise ValueError("❌ متغیرهای محیطی کامل نیستند! BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME")

# اتصال به گیت‌هاب
try:
    github = Github(GH_TOKEN)
    repo = github.get_repo(REPO_NAME)
    logger.info("✅ اتصال به گیت‌هاب موفقیت‌آمیز بود")
except Exception as e:
    logger.error(f"❌ خطا در اتصال به گیت‌هاب: {e}")
    raise

# ==================== کلاس جلسه ====================
class Session:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="telegram_upload_")
        self.files = []          # {"name", "size", "local_path", "folder_name"}
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
    """نمایش لیست فایل‌ها با دکمه‌ها"""
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
    """دانلود فایل از تلگرام در دایرکتوری موقت"""
    local_path = os.path.join(session.temp_dir, file_name)
    file = await bot.get_file(file_id)
    await file.download_to_drive(local_path)
    logger.info(f"دانلود شد: {file_name} ({os.path.getsize(local_path)} bytes)")
    return local_path

def split_file(file_path, chunk_size=95*1024*1024):  # 95MB per part
    """تقسیم فایل به قطعات و بازگرداندن لیست مسیر قطعات"""
    file_size = os.path.getsize(file_path)
    num_parts = math.ceil(file_size / chunk_size)
    parts = []
    base_name = os.path.basename(file_path)
    name_without_ext = os.path.splitext(base_name)[0]
    ext = os.path.splitext(base_name)[1]
    with open(file_path, "rb") as f:
        for i in range(num_parts):
            part_data = f.read(chunk_size)
            # نام قطعه: filename.ext.part001
            part_name = f"{base_name}.part{i+1:03d}"
            part_path = os.path.join(os.path.dirname(file_path), part_name)
            with open(part_path, "wb") as part_file:
                part_file.write(part_data)
            parts.append(part_path)
            logger.info(f"قطعه {i+1}/{num_parts} ساخته شد: {part_name}")
    # ساخت فایل متادیتا برای بازسازی
    manifest = {
        "original_name": base_name,
        "original_size": file_size,
        "chunk_size": chunk_size,
        "num_parts": num_parts,
        "parts": [os.path.basename(p) for p in parts],
        "recombine_command": f"cat {base_name}.part* > {base_name}"
    }
    manifest_path = os.path.join(os.path.dirname(file_path), f"{base_name}.manifest.json")
    with open(manifest_path, "w") as mf:
        json.dump(manifest, mf, indent=2)
    return parts, manifest_path

async def upload_to_github(local_file_path, original_name):
    """
    آپلود فایل در گیت‌هاب:
    - برای فایل‌های ≤100MB: مستقیماً آپلود می‌شود.
    - برای بزرگ‌تر: به قطعات 95MB تقسیم و در همان پوشه آپلود می‌شود + manifest.
    """
    # ساخت پوشه اختصاصی برای این فایل (بدون پسوند و با timestamp)
    file_base = os.path.splitext(original_name)[0]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"uploads/{file_base}_{timestamp}/"
    file_size = os.path.getsize(local_file_path)
    
    if file_size <= 100 * 1024 * 1024:
        # فایل کوچک: مستقیماً آپلود
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
        logger.info(f"آپلود شد (کوچک): {remote_path}")
        return [f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_path}"]
    else:
        # فایل بزرگ: تقسیم به قطعات
        parts, manifest_path = split_file(local_file_path)
        uploaded_urls = []
        # آپلود هر قطعه
        for part_path in parts:
            part_name = os.path.basename(part_path)
            remote_part_path = f"{folder_name}{part_name}"
            with open(part_path, "rb") as pf:
                content = pf.read()
            try:
                repo.create_file(remote_part_path, f"Upload part {part_name}", content, branch="main")
                uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_part_path}")
                logger.info(f"آپلود قطعه: {remote_part_path}")
            except Exception as e:
                logger.error(f"خطا در آپلود قطعه {part_name}: {e}")
                raise
        # آپلود فایل manifest
        with open(manifest_path, "rb") as mf:
            manifest_content = mf.read()
        remote_manifest = f"{folder_name}{os.path.basename(manifest_path)}"
        repo.create_file(remote_manifest, "Upload manifest", manifest_content, branch="main")
        uploaded_urls.append(f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_manifest}")
        logger.info(f"آپلود manifest: {remote_manifest}")
        return uploaded_urls  # لیست لینک تمام قطعات + manifest

# ==================== هندلرها ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ شما اجازه استفاده ندارید.")
        logger.warning(f"کاربر ناشناس {update.effective_user.id} دستور start داد")
        return
    await update.message.reply_text(
        "🚀 **ربات آپلودر به گیت‌هاب**\n\n"
        "📌 **نحوه کار:**\n"
        "1. فایل(های) خود را ارسال کنید.\n"
        "2. هر فایل در یک پوشه جداگانه ذخیره می‌شود.\n"
        "3. اگر فایل بزرگ‌تر از ۱۰۰ مگابایت باشد، **به قطعات ۹۵ مگابایتی** تقسیم و آپلود می‌شود.\n"
        "4. یک فایل `manifest.json` همراه با قطعات، دستور بازسازی را ارائه می‌دهد.\n"
        "5. با دکمه‌های زیر می‌توانید فایل‌ها را مدیریت و در نهایت آپلود کنید.\n\n"
        "⚠️ **توجه:** فقط شما مجاز هستید. حداکثر حجم کل جلسه به فضای موقت Actions محدود است (حدود ۱۴ گیگ).",
        parse_mode="Markdown"
    )
    logger.info(f"دستور start از OWNER_ID {OWNER_ID} اجرا شد")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"دریافت فایل از کاربر {user_id}")
    if user_id != OWNER_ID:
        await update.message.reply_text("⛔ شما اجازه آپلود ندارید.")
        logger.warning(f"کاربر {user_id} تلاش به آپلود کرد (OWNER_ID={OWNER_ID})")
        return

    doc = update.message.document
    logger.info(f"فایل دریافتی: {doc.file_name}, حجم: {doc.file_size} bytes")

    # دانلود فایل
    try:
        local_path = await download_file(doc.file_id, doc.file_name)
    except Exception as e:
        logger.error(f"خطا در دانلود: {e}")
        await update.message.reply_text(f"❌ خطا در دریافت فایل: {str(e)}")
        return

    # اضافه به لیست جلسه
    session.files.append({
        "name": doc.file_name,
        "size": doc.file_size,
        "local_path": local_path
    })

    # ایجاد پیام وضعیت اگر وجود ندارد
    if not session.status_message_id:
        session.chat_id = update.effective_chat.id
        msg = await update.message.reply_text("⚙️ در حال آماده‌سازی...")
        session.status_message_id = msg.message_id
        await update_status_message()
        logger.info("پیام وضعیت ساخته شد")
    else:
        await update_status_message()

    # حذف پیام اصلی فایل برای تمیزی
    await update.message.delete()
    logger.info(f"فایل {doc.file_name} به لیست اضافه شد. تعداد کل: {len(session.files)}")

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
            await query.edit_message_text("هیچ فایلی برای آپلود وجود ندارد.")
            return

        await query.edit_message_text("🔄 در حال آپلود به گیت‌هاب... (ممکن است برای فایل‌های بزرگ چند دقیقه طول بکشد)")
        all_results = []
        for idx, f in enumerate(session.files, 1):
            try:
                logger.info(f"شروع آپلود فایل {idx}/{len(session.files)}: {f['name']}")
                urls = await upload_to_github(f["local_path"], f["name"])
                result_msg = f"✅ {f['name']} ({get_file_size_str(f['size'])})\n"
                if len(urls) == 1:
                    result_msg += f"   🔗 [دانلود مستقیم]({urls[0]})\n"
                else:
                    result_msg += f"   📦 به {len(urls)-1} قطعه + manifest تقسیم شد.\n"
                    result_msg += f"   🔗 اولین قطعه: [part001]({urls[0]})\n"
                    result_msg += f"   📄 [manifest]({urls[-1]})\n"
                all_results.append(result_msg)
            except Exception as e:
                logger.error(f"خطا در آپلود {f['name']}: {e}")
                all_results.append(f"❌ {f['name']} → خطا: {str(e)}")
        final_text = "**نتیجه آپلود:**\n\n" + "\n".join(all_results)
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
                logger.info(f"حذف فایل {removed['name']} از دیسک")
            except OSError as e:
                logger.error(f"خطا در حذف {removed['name']}: {e}")
            await update_status_message()
        else:
            await query.answer("لیست خالی است!")

    elif data == "clear_all":
        for f in session.files:
            try:
                os.remove(f["local_path"])
            except OSError:
                pass
        session.files.clear()
        await update_status_message()
        await query.answer("همه فایل‌ها پاک شدند.")
        logger.info("همه فایل‌ها پاک شدند")

async def finish_session():
    logger.info("پایان جلسه - پاکسازی دایرکتوری موقت")
    shutil.rmtree(session.temp_dir, ignore_errors=True)
    if session.chat_id:
        await bot.send_message(chat_id=session.chat_id, text="👋 جلسه پایان یافت. ربات خاموش می‌شود.")
    os._exit(0)

async def idle_timeout():
    await asyncio.sleep(300)  # 5 دقیقه
    logger.warning("تایم‌اوت 5 دقیقه - خاتمه جلسه")
    if session.chat_id:
        await bot.send_message(chat_id=session.chat_id, text="⏰ عدم فعالیت به مدت ۵ دقیقه. جلسه خاتمه یافت.")
    await finish_session()

async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("✅ ربات راه‌اندازی شد، در انتظار پیام...")

    # ارسال پیام فعال بودن به OWNER_ID
    try:
        await bot.send_message(chat_id=OWNER_ID, text="🤖 ربات آپلودر فعال شد.\nفایل‌های خود را ارسال کنید یا /start را بزنید.")
    except Exception as e:
        logger.error(f"خطا در ارسال پیام خوشامد: {e}")

    asyncio.create_task(idle_timeout())

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(session.temp_dir, ignore_errors=True)
