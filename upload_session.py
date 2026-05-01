import os
import tempfile
import shutil
import asyncio
import logging
from datetime import datetime
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from github import Github
from github.GithubException import GithubException

# ==================== تنظیمات از متغیرهای محیطی ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
GH_TOKEN = os.getenv("GH_TOKEN")
REPO_NAME = os.getenv("REPO_NAME")

# لاگینگ دقیق
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info("========== شروع جلسه ==========")
logger.info(f"BOT_TOKEN: {BOT_TOKEN[:5]}...{BOT_TOKEN[-5:] if BOT_TOKEN else 'None'}")
logger.info(f"OWNER_ID: {OWNER_ID}")
logger.info(f"GH_TOKEN: {GH_TOKEN[:5]}...{GH_TOKEN[-5:] if GH_TOKEN else 'None'}")
logger.info(f"REPO_NAME: {REPO_NAME}")

if not all([BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME]):
    raise ValueError("لطفاً تمام متغیرهای محیطی را تنظیم کنید")

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
        self.files = []          # {"name", "file_id", "size", "local_path"}
        self.status_message_id = None
        self.chat_id = None
        self.folder_name = datetime.now().strftime("uploads/%Y-%m-%d_%H-%M-%S/")
        logger.info(f"📁 پوشه جلسه: {self.folder_name}")

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
    """نمایش لیست فایل‌ها و دکمه‌ها"""
    if not session.status_message_id or not session.chat_id:
        logger.warning("status_message_id یا chat_id وجود ندارد")
        return
    text = "📂 **لیست فایل‌های آماده آپلود**\n"
    text += f"🗂 پوشه مقصد: `{session.folder_name}`\n\n"
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
    logger.info(f"دانلود شد: {file_name} -> {local_path} ({os.path.getsize(local_path)} bytes)")
    return local_path

async def upload_to_github(file_path, file_name):
    """آپلود فایل در پوشه جلسه (فقط commit، بدون Release)"""
    remote_path = f"{session.folder_name}{file_name}"
    with open(file_path, "rb") as f:
        content = f.read()
    try:
        # بررسی وجود فایل قبلی
        contents = repo.get_contents(remote_path)
        repo.update_file(contents.path, f"Update {file_name}", content, contents.sha, branch="main")
        logger.info(f"به‌روزرسانی: {remote_path}")
    except GithubException as e:
        if e.status == 404:
            repo.create_file(remote_path, f"Upload {file_name}", content, branch="main")
            logger.info(f"ایجاد: {remote_path}")
        else:
            raise
    return f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_path}"

# ==================== هندلرها ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("شما مجاز نیستید.")
        logger.warning(f"کاربر {update.effective_user.id} تلاش به /start کرد")
        return
    await update.message.reply_text(
        "👋 به ربات آپلودر تلگرام به گیت‌هاب خوش آمدید!\n\n"
        "📌 نحوه کار:\n"
        "1. فایل(های) خود را (حداکثر ۱۰۰ مگابایت) ارسال کنید.\n"
        "2. لیست فایل‌ها با دکمه‌ها نمایش داده می‌شود.\n"
        "3. می‌توانید آخرین فایل را حذف یا همه را پاک کنید.\n"
        "4. روی «آپلود به گیت‌هاب» کلیک کنید تا در مخزن ذخیره شوند.\n"
        "5. فایل‌ها در پوشه `uploads/YYYY-MM-DD_HH-MM-SS/` ذخیره می‌شوند.\n\n"
        "⚠️ توجه: فقط فایل‌های ≤ ۱۰۰ مگابایت پذیرفته می‌شوند."
    )
    logger.info("دستور /start اجرا شد")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("شما مجاز نیستید.")
        logger.warning(f"کاربر {update.effective_user.id} فایل ارسال کرد و رد شد")
        return

    doc = update.message.document
    logger.info(f"فایل دریافتی: {doc.file_name} (حجم: {doc.file_size} bytes)")

    # محدودیت 100 مگابایت
    MAX_SIZE = 100 * 1024 * 1024  # 100 MB
    if doc.file_size > MAX_SIZE:
        await update.message.reply_text(
            f"❌ حجم فایل ({get_file_size_str(doc.file_size)}) بیشتر از ۱۰۰ مگابایت است.\n"
            "لطفاً فایل کوچک‌تری ارسال کنید یا از Git LFS استفاده نمایید."
        )
        logger.warning(f"فایل {doc.file_name} به دلیل حجم زیاد رد شد")
        return

    # دانلود فایل
    local_path = await download_file(doc.file_id, doc.file_name)
    session.files.append({
        "name": doc.file_name,
        "file_id": doc.file_id,
        "size": doc.file_size,
        "local_path": local_path
    })

    # اگر پیام وضعیت وجود ندارد، بساز
    if not session.status_message_id:
        session.chat_id = update.effective_chat.id
        msg = await update.message.reply_text("⚙️ در حال آماده‌سازی...")
        session.status_message_id = msg.message_id
        await update_status_message()
        logger.info("پیام وضعیت اولیه ساخته شد")
    else:
        await update_status_message()

    await update.message.delete()
    logger.info(f"فایل {doc.file_name} به لیست اضافه شد. تعداد کل: {len(session.files)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != OWNER_ID:
        await query.edit_message_text("شما مجاز نیستید.")
        logger.warning(f"کاربر {update.effective_user.id} دکمه زد")
        return

    data = query.data
    logger.info(f"دکمه فشرده شد: {data}")

    if data == "upload":
        if not session.files:
            await query.edit_message_text("هیچ فایلی برای آپلود وجود ندارد.")
            return
        await query.edit_message_text("🔄 در حال آپلود به گیت‌هاب...")
        results = []
        for f in session.files:
            try:
                url = await upload_to_github(f["local_path"], f["name"])
                results.append(f"✅ {f['name']} → [مشاهده فایل]({url})")
            except Exception as e:
                logger.error(f"خطا در آپلود {f['name']}: {e}")
                results.append(f"❌ {f['name']} → خطا: {str(e)}")
        result_text = "**نتیجه آپلود:**\n" + "\n".join(results)
        await query.edit_message_text(result_text, parse_mode="Markdown", disable_web_page_preview=True)
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
    logger.info("پایان جلسه - پاکسازی")
    shutil.rmtree(session.temp_dir, ignore_errors=True)
    if session.chat_id:
        await bot.send_message(chat_id=session.chat_id, text="👋 جلسه پایان یافت. ربات خاموش می‌شود.")
    os._exit(0)

async def idle_timeout():
    await asyncio.sleep(300)
    logger.warning("تایم‌اوت ۵ دقیقه - خاتمه جلسه")
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
    logger.info("✅ ربات راه‌اندازی شد. در انتظار پیام...")

    # ارسال پیام خوشامدگویی به OWNER_ID
    try:
        await bot.send_message(chat_id=OWNER_ID, text="🤖 ربات آپلودر فعال شد.\nفایل‌های خود را ارسال کنید یا /start را بزنید.")
        logger.info("پیام خوشامدگویی ارسال شد")
    except Exception as e:
        logger.error(f"خطا در ارسال پیام خوشامدگویی: {e}")

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
