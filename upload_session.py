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
REPO_NAME = os.getenv("REPO_NAME")        # به صورت "owner/repo"

# راه‌اندازی لاگینگ با فرمت مناسب
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# بررسی و لاگ کردن اطلاعات دریافتی (بدون فاش کردن کامل توکن)
logger.info("========== شروع جلسه ==========")
if BOT_TOKEN:
    logger.info(f"✅ BOT_TOKEN دریافت شد (با شروع: {BOT_TOKEN[:4]}...{BOT_TOKEN[-4:]})")
else:
    logger.error("❌ BOT_TOKEN پیدا نشد!")
if OWNER_ID:
    logger.info(f"✅ OWNER_ID دریافت شد: {OWNER_ID}")
else:
    logger.error("❌ OWNER_ID پیدا نشد!")
if GH_TOKEN:
    logger.info(f"✅ GH_TOKEN دریافت شد (با شروع: {GH_TOKEN[:4]}...{GH_TOKEN[-4:]})")
else:
    logger.error("❌ GH_TOKEN پیدا نشد!")
if REPO_NAME:
    logger.info(f"✅ REPO_NAME دریافت شد: {REPO_NAME}")
else:
    logger.error("❌ REPO_NAME پیدا نشد!")

if not all([BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME]):
    raise ValueError("لطفاً تمام متغیرهای محیطی را تنظیم کنید: BOT_TOKEN, OWNER_ID, GH_TOKEN, REPO_NAME")

# ==================== اتصال به گیت‌هاب ====================
try:
    github = Github(GH_TOKEN)
    repo = github.get_repo(REPO_NAME)
    logger.info(f"✅ اتصال به گیت‌هاب موفقیت‌آمیز بود. مخزن: {REPO_NAME}")
except Exception as e:
    logger.error(f"❌ خطا در اتصال به گیت‌هاب: {e}")
    raise

# ==================== کلاس جلسه ====================
class Session:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="telegram_upload_")
        self.files = []          # هر عنصر: {"name": str, "file_id": str, "size": int, "local_path": str}
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
    """ویرایش پیام وضعیت با لیست فایل‌ها و دکمه‌ها"""
    if not session.status_message_id or not session.chat_id:
        logger.warning("update_status_message: status_message_id یا chat_id وجود ندارد.")
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
        logger.debug("پیام وضعیت بروزرسانی شد.")
    except Exception as e:
        logger.error(f"خطا در بروزرسانی پیام وضعیت: {e}")

async def download_file(file_id, file_name):
    local_path = os.path.join(session.temp_dir, file_name)
    file = await bot.get_file(file_id)
    await file.download_to_drive(local_path)
    logger.info(f"فایل {file_name} دانلود شد در {local_path} (حجم: {os.path.getsize(local_path)} bytes)")
    return local_path

async def upload_to_github_commit(file_path, file_name):
    remote_path = f"{session.folder_name}{file_name}"
    with open(file_path, "rb") as f:
        content = f.read()
    try:
        contents = repo.get_contents(remote_path)
        repo.update_file(contents.path, f"Update {file_name}", content, contents.sha, branch="main")
        logger.info(f"به‌روزرسانی فایل در گیت‌هاب: {remote_path}")
    except GithubException as e:
        if e.status == 404:
            repo.create_file(remote_path, f"Upload {file_name}", content, branch="main")
            logger.info(f"ایجاد فایل جدید در گیت‌هاب: {remote_path}")
        else:
            raise
    return f"https://raw.githubusercontent.com/{REPO_NAME}/main/{remote_path}"

async def upload_to_github_release(file_path, file_name):
    release_tag = f"upload-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    release_name = f"Upload session {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    release = repo.create_git_release(
        tag=release_tag,
        name=release_name,
        message=f"فایل‌های بزرگ آپلود شده در تاریخ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    with open(file_path, "rb") as f:
        asset = release.upload_asset(path=file_name, content=f, content_type="application/octet-stream")
    logger.info(f"فایل بزرگ {file_name} در Release {release_tag} آپلود شد.")
    return asset.browser_download_url

# ==================== هندلرها ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /start - فقط برای صاحب بات"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("شما اجازه استفاده از این بات را ندارید.")
        logger.warning(f"کاربر ناشناس {update.effective_user.id} سعی در استفاده از /start کرد.")
        return
    await update.message.reply_text(
        "👋 به ربات آپلودر تلگرام به گیت‌هاب خوش آمدید!\n\n"
        "📌 نحوه کار:\n"
        "1. فایل(های) خود را برای من ارسال کنید.\n"
        "2. هر فایل به لیست اضافه می‌شود.\n"
        "3. با دکمه‌های زیر می‌توانید آخرین فایل را حذف یا همه را پاک کنید.\n"
        "4. روی دکمه «آپلود به گیت‌هاب» کلیک کنید.\n"
        "5. فایل‌های کوچک (≤100MB) در پوشه تاریخ‌دار commit می‌شوند.\n"
        "6. فایل‌های بزرگ (>100MB) در Release آپلود می‌شوند.\n\n"
        "⏱ پس از ۵ دقیقه عدم فعالیت، جلسه خودکار پایان می‌یابد.\n\n"
        "❗️ توجه: فقط خودتان می‌توانید از این بات استفاده کنید."
    )
    logger.info(f"دستور /start از طرف OWNER_ID ({OWNER_ID}) اجرا شد.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("شما مجاز به استفاده از این بات نیستید.")
        logger.warning(f"کاربر ناشناس {update.effective_user.id} فایل ارسال کرد و رد شد.")
        return
    doc = update.message.document
    logger.info(f"دریافت فایل: {doc.file_name} (حجم: {doc.file_size} bytes) از OWNER_ID")
    if doc.file_size > 2 * 1024 * 1024 * 1024:
        await update.message.reply_text("❌ حجم فایل بیشتر از ۲ گیگابایت است و قابل آپلود به گیت‌هاب نیست.")
        logger.warning(f"فایل {doc.file_name} به دلیل حجم بالا رد شد.")
        return
    local_path = await download_file(doc.file_id, doc.file_name)
    session.files.append({
        "name": doc.file_name,
        "file_id": doc.file_id,
        "size": doc.file_size,
        "local_path": local_path
    })
    if not session.status_message_id:
        session.chat_id = update.effective_chat.id
        msg = await update.message.reply_text("در حال آماده‌سازی...")
        session.status_message_id = msg.message_id
        await update_status_message()
        logger.info("پیام وضعیت اولیه ایجاد شد.")
    else:
        await update_status_message()
    await update.message.delete()
    logger.info(f"فایل {doc.file_name} به لیست اضافه شد. تعداد کل: {len(session.files)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != OWNER_ID:
        await query.edit_message_text("شما مجاز نیستید.")
        logger.warning(f"کاربر ناشناس {update.effective_user.id} دکمه زد.")
        return
    data = query.data
    logger.info(f"دکمه فشرده شد: {data}")

    if data == "upload":
        if not session.files:
            await query.edit_message_text("هیچ فایلی برای آپلود وجود ندارد.")
            return
        await query.edit_message_text("🔄 در حال آپلود فایل‌ها به گیت‌هاب...")
        results = []
        for f in session.files:
            try:
                if f["size"] > 100 * 1024 * 1024:
                    url = await upload_to_github_release(f["local_path"], f["name"])
                    results.append(f"✅ {f['name']} (بزرگ) → آپلود شد در Release: {url}")
                else:
                    url = await upload_to_github_commit(f["local_path"], f["name"])
                    results.append(f"✅ {f['name']} → commit شد: {url}")
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
                logger.info(f"فایل {removed['name']} از دیسک حذف شد.")
            except OSError as e:
                logger.error(f"خطا در حذف فایل {removed['name']}: {e}")
            await update_status_message()
        else:
            await query.answer("لیست خالی است!")

    elif data == "clear_all":
        for f in session.files:
            try:
                os.remove(f["local_path"])
                logger.info(f"فایل {f['name']} از دیسک حذف شد.")
            except OSError as e:
                logger.error(f"خطا در حذف فایل {f['name']}: {e}")
        session.files.clear()
        await update_status_message()
        await query.answer("همه فایل‌ها پاک شدند.")

async def finish_session():
    logger.info("پایان جلسه - پاکسازی دایرکتوری موقت و خروج")
    shutil.rmtree(session.temp_dir, ignore_errors=True)
    if session.chat_id:
        try:
            await bot.send_message(chat_id=session.chat_id, text="👋 جلسه پایان یافت. ربات خاموش می‌شود.")
        except:
            pass
    os._exit(0)

async def idle_timeout():
    await asyncio.sleep(300)  # 5 دقیقه
    logger.warning("تایم‌اوت ۵ دقیقه - پایان خودکار جلسه")
    if session.chat_id:
        try:
            await bot.send_message(chat_id=session.chat_id, text="⏰ عدم فعالیت به مدت ۵ دقیقه. جلسه خاتمه یافت.")
        except:
            pass
    await finish_session()

# ==================== تابع اصلی ====================
async def main():
    logger.info("راه‌اندازی اپلیکیشن تلگرام...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("✅ ربات شروع به کار کرد. در حال انتظار برای پیام‌ها...")

    # ارسال پیام خوشامدگویی خودکار به OWNER_ID (اختیاری - مکمل /start)
    try:
        await bot.send_message(chat_id=OWNER_ID, text="🤖 ربات آپلودر با موفقیت راه‌اندازی شد.\nلطفاً فایل‌های خود را ارسال کنید یا از دستور /start استفاده کنید.")
        logger.info(f"پیام خوشامدگویی به OWNER_ID ({OWNER_ID}) ارسال شد.")
    except Exception as e:
        logger.error(f"ارسال پیام خوشامدگویی به OWNER_ID ممکن نبود: {e}")

    asyncio.create_task(idle_timeout())

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("دریافت سیگنال قطع، در حال خروج...")
    finally:
        shutil.rmtree(session.temp_dir, ignore_errors=True)
