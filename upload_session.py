import os
import sys
import tempfile
import shutil
import asyncio
import logging
import json
from datetime import datetime

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.crypto import AuthKey
    import git
    from git.exc import GitCommandError
except ImportError as e:
    print(f"❌ کتابخانه ناقص: {e}\nلطفاً با دستور زیر نصب کنید:\npip install python-telegram-bot telethon GitPython")
    sys.exit(1)

# ========== تنظیمات ==========
SPLIT_SIZE = 95 * 1024 * 1024  # 95 مگابایت
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
GH_TOKEN = os.getenv("GH_TOKEN")
REPO_NAME = os.getenv("REPO_NAME")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
DC_ID = os.getenv("DC_ID")
AUTH_KEY_HEX = os.getenv("AUTH_KEY_HEX")
USER_ID = os.getenv("USER_ID")

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

logger.info(f"✅ OWNER: {OWNER_ID} | REPO: {REPO_NAME} | SPLIT SIZE: {SPLIT_SIZE//(1024*1024)} MB")

# ========== کلاس نشست ==========
class Session:
    def __init__(self):
        self.temp_dir = None
        self.files = []
        self.status_msg_id = None
        self.chat_id = None
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
            split_tag = " (تقسیم شده)" if f.get('is_split', False) else ""
            text += f"{i}. {f['name']} ({size_str(f['size'])}{split_tag})\n"
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

async def finish(send_message: bool = True):
    if session.temp_dir and os.path.exists(session.temp_dir):
        shutil.rmtree(session.temp_dir, ignore_errors=True)
        session.temp_dir = None
    
    for f in session.files:
        local_path = f.get("local_path")
        if local_path and os.path.exists(local_path):
            try:
                if os.path.isdir(local_path):
                    shutil.rmtree(local_path, ignore_errors=True)
                else:
                    os.remove(local_path)
            except:
                pass
    
    session.files.clear()
    session.status_msg_id = None
    session.chat_id = None
    session.is_active = False
    
    if send_message and session.app and OWNER_ID:
        try:
            await session.app.bot.send_message(OWNER_ID, "👋 نشست پایان یافت. /start برای شروع مجدد")
        except Exception as e:
            logger.error(f"خطا: {e}")

async def ensure_userbot_connected():
    if session.userbot is None:
        raise Exception("یوزربات راه‌اندازی نشده است")
    if not session.userbot.is_connected():
        logger.warning("یوزربات قطع شده بود، دوباره وصل می‌شود...")
        await session.userbot.connect()
        if not await session.userbot.is_user_authorized():
            raise Exception("یوزربات پس از اتصال مجدد احراز هویت نشد")
        logger.info("یوزربات مجدداً متصل شد")

# ========== دانلود فایل (بدون حذف پیام) ==========
async def download_small_file(file_id: str, name: str, bot, progress_msg):
    path = os.path.join(session.temp_dir, name)
    file = await bot.get_file(file_id)
    await progress_msg.edit_text(f"📥 در حال دانلود {name}... (Bot API)")
    await file.download_to_drive(path)
    await progress_msg.edit_text(f"✅ دانلود {name} کامل شد.")
    logger.info(f"✅ دانلود کوچک: {name} ({size_str(os.path.getsize(path))})")
    return path

async def download_large_file(name: str, progress_msg):
    await ensure_userbot_connected()
    if not session.bot_username:
        raise Exception("نام کاربری ربات مشخص نیست")

    path = os.path.join(session.temp_dir, name)
    await progress_msg.edit_text(f"🔍 در حال جستجوی فایل {name}...")

    found_media = None
    async for msg in session.userbot.iter_messages(
        session.bot_username,
        from_user=OWNER_ID,
        limit=5
    ):
        if msg and msg.media:
            found_media = msg
            break
    
    if not found_media:
        raise Exception("فایل یافت نشد. لطفاً فایل را دوباره ارسال کنید.")

    file_size = found_media.file.size if found_media.file else 0
    last_percent = 0

    async def progress_callback(current, total):
        nonlocal last_percent
        if total == 0:
            return
        percent = int((current / total) * 100)
        if percent > last_percent:
            last_percent = percent
            bar = "█" * (percent // 5) + "░" * (20 - (percent // 5))
            try:
                await progress_msg.edit_text(f"📥 دانلود {name}:\n`[{bar}] {percent}%`\n{size_str(current)} / {size_str(total)}",
                                             parse_mode="Markdown")
            except:
                pass

    await found_media.download_media(file=path, progress_callback=progress_callback)
    await progress_msg.edit_text(f"✅ دانلود {name} کامل شد.")
    logger.info(f"✅ دانلود بزرگ: {name} ({size_str(os.path.getsize(path))})")
    return path

# ========== عملیات Split فایل ==========
def split_file(input_path: str, output_dir: str, part_size: int = SPLIT_SIZE):
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.basename(input_path)
    part_prefix = os.path.join(output_dir, base_name + ".part")
    
    part_num = 1
    with open(input_path, 'rb') as f:
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            part_path = f"{part_prefix}{part_num:03d}"
            with open(part_path, 'wb') as p:
                p.write(chunk)
            part_num += 1
    
    total_parts = part_num - 1
    meta = {
        "original_name": base_name,
        "original_size": os.path.getsize(input_path),
        "part_size": part_size,
        "total_parts": total_parts,
        "parts": [f"{base_name}.part{i:03d}" for i in range(1, total_parts+1)]
    }
    with open(os.path.join(output_dir, "_split_meta.json"), 'w', encoding='utf-8') as mf:
        json.dump(meta, mf, indent=2)
    
    reassemble_sh = f"""#!/bin/bash
# بازسازی فایل اصلی از قطعات
# فایل اصلی: {base_name}
cat {base_name}.part* > {base_name}
echo "✅ فایل {base_name} بازسازی شد. حجم: $(du -h {base_name} | cut -f1)"
"""
    with open(os.path.join(output_dir, "reassemble.sh"), 'w', encoding='utf-8') as sh:
        sh.write(reassemble_sh)
    os.chmod(os.path.join(output_dir, "reassemble.sh"), 0o755)
    
    reassemble_bat = f"""@echo off
REM بازسازی فایل اصلی از قطعات (ویندوز)
copy /b {base_name}.part* {base_name}
echo ✅ فایل {base_name} بازسازی شد.
"""
    with open(os.path.join(output_dir, "reassemble.bat"), 'w', encoding='utf-8') as bat:
        bat.write(reassemble_bat)
    
    logger.info(f"✅ فایل {base_name} به {total_parts} قطعه تقسیم شد در {output_dir}")
    return output_dir, total_parts

# ========== آپلود با گیت ==========
async def upload_to_github_with_git(local_path_or_dir: str, orig_name: str, caption_text: str = "", is_split: bool = False, progress_msg=None):
    remote_url = f"https://{GH_TOKEN}@github.com/{REPO_NAME}.git"
    
    if session.repo_dir is None:
        session.repo_dir = os.path.join(session.temp_dir, "github_repo")
    
    if not os.path.exists(session.repo_dir):
        if progress_msg:
            await progress_msg.edit_text("📥 در حال clone مخزن گیت‌هاب...")
        repo = git.Repo.clone_from(remote_url, session.repo_dir, branch="main")
    else:
        repo = git.Repo(session.repo_dir)
        if progress_msg:
            await progress_msg.edit_text("🔄 در حال همگام‌سازی با مخزن...")
        repo.git.reset("--hard")
        repo.git.clean("-fd")
        try:
            repo.remotes.origin.pull(rebase=True)
        except GitCommandError as e:
            logger.warning(f"خطا در pull: {e}. تلاش با reset --hard origin/main")
            repo.git.fetch()
            repo.git.reset("--hard", "origin/main")
    
    now = datetime.now()
    date_path = now.strftime("%Y/%m/%d")
    base_name = os.path.splitext(orig_name)[0]
    timestamp = now.strftime("%H%M%S")
    folder_name = f"{base_name}_{timestamp}"
    dest_dir = os.path.join(session.repo_dir, "uploads", date_path, folder_name)
    os.makedirs(dest_dir, exist_ok=True)
    
    if is_split:
        for item in os.listdir(local_path_or_dir):
            src = os.path.join(local_path_or_dir, item)
            dst = os.path.join(dest_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        if caption_text.strip():
            cap_path = os.path.join(dest_dir, f"{orig_name}.caption.txt")
            with open(cap_path, "w", encoding="utf-8") as cp:
                cp.write(caption_text)
    else:
        shutil.copy2(local_path_or_dir, os.path.join(dest_dir, orig_name))
        if caption_text.strip():
            cap_path = os.path.join(dest_dir, f"{orig_name}.caption.txt")
            with open(cap_path, "w", encoding="utf-8") as cp:
                cp.write(caption_text)
    
    if progress_msg:
        await progress_msg.edit_text("📝 در حال commit...")
    repo.index.add("*")
    commit_msg = f"{'Split: ' if is_split else 'Add '}{orig_name} at {now.strftime('%Y-%m-%d %H:%M:%S')}"
    repo.index.commit(commit_msg)
    
    push_success = False
    last_error = None
    for attempt in range(5):
        try:
            if progress_msg:
                await progress_msg.edit_text(f"📤 در حال push (تلاش {attempt+1}/5)...")
            if attempt > 0:
                repo.remotes.origin.pull(rebase=True)
            push_result = repo.remotes.origin.push()
            success = True
            for info in push_result:
                if info.flags & git.remote.PushInfo.ERROR:
                    success = False
                    last_error = info.summary
                    logger.error(f"خطا در push: {info.summary}")
                    break
            if success:
                push_success = True
                break
            else:
                raise GitCommandError('push', 'non-fast-forward or rejected')
        except GitCommandError as e:
            last_error = str(e)
            logger.warning(f"⚠️ خطا در push (تلاش {attempt+1}/5): {e}")
            if attempt < 4:
                await asyncio.sleep(3 * (attempt + 1))
            else:
                if "non-fast-forward" in str(e) or "rejected" in str(e):
                    if progress_msg:
                        await progress_msg.edit_text("⚠️ مخزن به‌روز نیست، تلاش با force push...")
                    try:
                        repo.remotes.origin.push(force=True)
                        push_success = True
                        logger.warning("از force push استفاده شد.")
                        break
                    except GitCommandError as e2:
                        last_error = str(e2)
                        logger.error(f"force push نیز ناموفق: {e2}")
                raise Exception(f"پس از ۵ بار تلاش، push ناموفق: {last_error}")
    
    if not push_success:
        raise Exception(f"push با خطا مواجه شد: {last_error}")
    
    relative_path = f"uploads/{date_path}/{folder_name}/"
    if not is_split:
        relative_path += orig_name
    else:
        relative_path += f"(split into {len([f for f in os.listdir(dest_dir) if f.startswith(orig_name+'.part')])} parts)"
    
    if progress_msg:
        await progress_msg.delete()
    return relative_path

# ========== هندلر فایل (اصلاح شده با مدیریت صحیح پیام) ==========
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
            downloaded_path = await download_large_file(fname, progress_msg)
        else:
            logger.info(f"📥 فایل کوچک ({size_str(file_size)}) - Bot API")
            downloaded_path = await download_small_file(file_obj.file_id, fname, context.bot, progress_msg)
        
        # بررسی نیاز به split
        if file_size > SPLIT_SIZE:
            await progress_msg.edit_text(f"✂️ فایل {fname} بزرگتر از {SPLIT_SIZE//(1024*1024)} MB است، در حال تقسیم...")
            split_dir = tempfile.mkdtemp(dir=session.temp_dir, prefix="split_")
            split_output_dir, total_parts = split_file(downloaded_path, split_dir, SPLIT_SIZE)
            os.remove(downloaded_path)  # حذف فایل اصلی
            session.files.append({
                "name": fname,
                "size": file_size,
                "local_path": split_output_dir,
                "caption": caption,
                "is_split": True,
                "total_parts": total_parts
            })
            await progress_msg.edit_text(f"✂️ فایل {fname} به {total_parts} قطعه تقسیم شد و آماده آپلود است.")
            await asyncio.sleep(2)
            await progress_msg.delete()
        else:
            session.files.append({
                "name": fname,
                "size": file_size,
                "local_path": downloaded_path,
                "caption": caption,
                "is_split": False
            })
            await progress_msg.delete()
    except Exception as e:
        logger.error(f"❌ خطا در پردازش {fname}: {e}", exc_info=True)
        try:
            await progress_msg.edit_text(f"❌ خطا: {str(e)}")
        except:
            await msg.reply_text(f"❌ خطا: {str(e)}")
        return

    await update_status(context.bot)

    try:
        await msg.react(emoji="📥")
    except:
        pass

    logger.info(f"✅ فایل اضافه شد: {fname} ({size_str(file_size)})")

# ========== سایر هندلرها (بدون تغییر) ==========
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
        "🚀 **آپلودر تلگرام → گیت‌هاب (نسخه پایدار با Split)**\n\n"
        "✅ فایل خود را ارسال کنید.\n"
        f"📦 فایل‌های بالای {SPLIT_SIZE//(1024*1024)} مگابایت خودکار تقسیم می‌شوند.\n"
        "📂 ساختار: `uploads/سال/ماه/روز/نام فایل_زمان/`\n"
        "🔹 فایل‌های >۲۰MB با یوزربات دانلود می‌شوند.\n"
        "🔹 برای بازسازی فایل اصلی، داخل هر پوشه فایل `reassemble.sh` (لینوکس) یا `reassemble.bat` (ویندوز) موجود است.\n"
        "🔹 ربات هرگز خودکار خاموش نمی‌شود.\n\n"
        "_فقط شما مجاز هستید._",
        parse_mode="Markdown"
    )

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

    data = query.data

    if data == "upload":
        if not session.files:
            await query.edit_message_text("❌ هیچ فایلی برای آپلود نیست.")
            return
        await query.edit_message_text("🔄 در حال آپلود به گیت‌هاب...")
        results = []
        for idx, f in enumerate(session.files):
            progress_msg = await context.bot.send_message(session.chat_id, f"🔄 {idx+1}/{len(session.files)}: شروع آپلود {f['name']}...")
            try:
                relative_path = await upload_to_github_with_git(
                    f["local_path"], 
                    f["name"], 
                    f.get("caption", ""), 
                    f.get("is_split", False), 
                    progress_msg
                )
                if f.get("is_split", False):
                    results.append(f"✅ {f['name']} → تقسیم شده در: `{relative_path}` (قطعات: {f.get('total_parts', '?')})")
                else:
                    results.append(f"✅ {f['name']} → `{relative_path}`")
                logger.info(f"✅ آپلود موفق: {f['name']}")
            except Exception as e:
                logger.error(f"❌ خطا در آپلود {f['name']}: {e}")
                results.append(f"❌ {f['name']} – خطا: {str(e)}")
                await progress_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
        final = "**نتیجه آپلود:**\n\n" + "\n".join(results) + "\n\n📌 **نحوه دانلود و بازسازی:**\n1. مخزن را به صورت ZIP دانلود کنید.\n2. داخل پوشه مربوطه، اگر فایل split شده، اسکریپت `reassemble.sh` (لینوکس) یا `reassemble.bat` (ویندوز) را اجرا کنید تا فایل اصلی ساخته شود."
        await query.edit_message_text(final, parse_mode="Markdown", disable_web_page_preview=True)
        await finish(send_message=True)

    elif data == "cancel":
        await query.edit_message_text("❌ لغو شد. نشست پایان یافت.")
        await finish(send_message=True)

    elif data == "remove_last":
        if session.files:
            removed = session.files.pop()
            local_path = removed.get("local_path")
            if local_path and os.path.exists(local_path):
                if os.path.isdir(local_path):
                    shutil.rmtree(local_path, ignore_errors=True)
                else:
                    os.remove(local_path)
            await update_status(context.bot)
            await query.answer("آخرین فایل حذف شد", show_alert=True)
        else:
            await query.answer("لیست خالی", show_alert=True)

    elif data == "clear_all":
        for f in session.files:
            local_path = f.get("local_path")
            if local_path and os.path.exists(local_path):
                if os.path.isdir(local_path):
                    shutil.rmtree(local_path, ignore_errors=True)
                else:
                    os.remove(local_path)
        session.files.clear()
        await update_status(context.bot)
        await query.answer("همه پاک شدند", show_alert=True)

# ========== راه‌اندازی یوزربات ==========
async def post_init(app: Application):
    session.app = app
    logger.info("🔌 راه‌اندازی یوزربات (همیشه متصل می‌ماند)...")
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

    bot_info = await app.bot.get_me()
    if not bot_info.username:
        raise ValueError("❌ ربات یوزرنیم ندارد.")
    session.bot_username = bot_info.username
    logger.info(f"✅ ربات @{session.bot_username} آماده است.")

    await app.bot.send_message(OWNER_ID, "🤖 ربات فعال شد (نسخه پایدار با split). /start")

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

    logger.info("🚀 ربات در حال اجرا (بدون تایم‌اوت، با قابلیت split خودکار)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
