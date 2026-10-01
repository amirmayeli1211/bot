# -*- coding: utf-8 -*-
"""
RIX PANEL Worker Deployer
ربات تلگرام برای Deploy خودکار Worker روی Cloudflare (Workers + KV)

Environment (Railway):
    BOT_TOKEN      توکن ربات تلگرام
    OWNER_ID       آیدی عددی مالک
    FORCE_CHANNEL  مثال: @RIX_PANEL

Start Command: python bot.py
"""

import asyncio
import html
import json
import logging
import os
import random
import re
import string
import sys
import time

import requests


# ----------------------------------------------------------------------------
# Self-heal: python-telegram-bot < 21.7 با Python 3.13 کرش می‌کند.
# اگر نسخه‌ی قدیمی نصب بود، خودکار نسخه‌ی سازگار نصب و ربات ری‌استارت می‌شود.
# ----------------------------------------------------------------------------
def _ensure_ptb():
    import subprocess
    from importlib import metadata

    def _ver():
        try:
            v = metadata.version("python-telegram-bot")
            return tuple(int(x) for x in re.findall(r"\d+", v)[:2])
        except Exception:
            return (0, 0)

    if _ver() >= (21, 7):
        return
    print("Upgrading python-telegram-bot ...", flush=True)
    cmds = [
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "python-telegram-bot==21.10"],
        ["uv", "pip", "install", "--python", sys.executable, "python-telegram-bot==21.10"],
    ]
    ok = False
    for cmd in cmds:
        try:
            if subprocess.call(cmd) == 0:
                ok = True
                break
        except Exception:
            pass
        if cmd[0] == sys.executable:
            try:
                subprocess.call([sys.executable, "-m", "ensurepip", "--upgrade"])
                if subprocess.call(cmd) == 0:
                    ok = True
                    break
            except Exception:
                pass
    if ok:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    print("Failed to upgrade python-telegram-bot", flush=True)


_ensure_ptb()
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# تنظیمات
# ----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "@RIX_PANEL").strip() or "@RIX_PANEL"
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0").strip())
except ValueError:
    OWNER_ID = 0

if FORCE_CHANNEL and not FORCE_CHANNEL.startswith(("@", "-")):
    FORCE_CHANNEL = "@" + FORCE_CHANNEL

CF_API = "https://api.cloudflare.com/client/v4"
CF_TOKEN_URL = "https://dash.cloudflare.com/profile/api-tokens"
COMPAT_DATE = "2024-09-23"
MAX_WORKER_SIZE = 10 * 1024 * 1024  # 10MB

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rix-deployer")


# ----------------------------------------------------------------------------
# Cloudflare API
# ----------------------------------------------------------------------------
class CFError(Exception):
    """kind: invalid_token | permission | kv | deploy | other"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _cf_detail(data: dict, fallback: str) -> str:
    errs = (data or {}).get("errors") or []
    if errs:
        return "; ".join(
            f"[{e.get('code')}] {e.get('message')}" for e in errs if isinstance(e, dict)
        ) or fallback
    return fallback


def cf_request(method: str, path: str, token: str, kind: str = "other", **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(kwargs.pop("headers", {}))
    try:
        resp = requests.request(
            method, CF_API + path, headers=headers, timeout=90, **kwargs
        )
    except requests.RequestException as exc:
        raise CFError("other", f"خطای شبکه: {exc}")

    try:
        data = resp.json()
    except ValueError:
        raise CFError(kind, f"پاسخ نامعتبر از Cloudflare (HTTP {resp.status_code})")

    if resp.status_code in (400, 401) and kind != "deploy" and "/accounts" == path:
        raise CFError("invalid_token", _cf_detail(data, "توکن نامعتبر است"))
    if resp.status_code == 401:
        raise CFError("invalid_token", _cf_detail(data, "توکن نامعتبر است"))
    if resp.status_code == 403:
        raise CFError("permission", _cf_detail(data, "دسترسی کافی نیست"))
    if not data.get("success", False):
        raise CFError(kind, _cf_detail(data, f"HTTP {resp.status_code}"))
    return data


def cf_get_accounts(token: str) -> list:
    data = cf_request("GET", "/accounts", token)
    accounts = data.get("result") or []
    if not accounts:
        raise CFError(
            "permission",
            "هیچ اکانتی برای این توکن یافت نشد. دسترسی Account Settings: Read را هم اضافه کنید.",
        )
    return [{"id": a["id"], "name": a.get("name", a["id"])} for a in accounts]


def random_kv_name() -> str:
    # فقط a-z و 0-9، بدون حروف بزرگ و فاصله
    return "kv" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def random_worker_name() -> str:
    return "rix-worker-" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=6)
    )


def cf_create_kv(token: str, account_id: str, kv_name: str) -> str:
    data = cf_request(
        "POST",
        f"/accounts/{account_id}/storage/kv/namespaces",
        token,
        kind="kv",
        json={"title": kv_name},
    )
    return data["result"]["id"]


def cf_delete_kv(token: str, account_id: str, namespace_id: str) -> None:
    try:
        cf_request(
            "DELETE",
            f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}",
            token,
            kind="kv",
        )
    except CFError as exc:
        log.warning("KV rollback failed: %s", exc.detail)


def is_module_worker(code: str) -> bool:
    return bool(re.search(r"\bexport\s+(default|\{|async|const|function)", code))


def cf_deploy_worker(
    token: str, account_id: str, script_name: str, code: str, namespace_id: str
) -> None:
    """
    آپلود Worker با Binding به KV (نام binding دقیقاً: kv)
    برای اتصال Binding لازم است درخواست به صورت multipart ارسال شود.
    """
    binding = {"type": "kv_namespace", "name": "kv", "namespace_id": namespace_id}
    code_bytes = code.encode("utf-8")

    if is_module_worker(code):
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": COMPAT_DATE,
            "bindings": [binding],
        }
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "worker.js": ("worker.js", code_bytes, "application/javascript+module"),
        }
    else:
        metadata = {
            "body_part": "script",
            "compatibility_date": COMPAT_DATE,
            "bindings": [binding],
        }
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "script": ("worker.js", code_bytes, "application/javascript"),
        }

    cf_request(
        "PUT",
        f"/accounts/{account_id}/workers/scripts/{script_name}",
        token,
        kind="deploy",
        files=files,
    )


def cf_get_or_create_subdomain(token: str, account_id: str) -> str:
    try:
        data = cf_request("GET", f"/accounts/{account_id}/workers/subdomain", token)
        sub = (data.get("result") or {}).get("subdomain")
        if sub:
            return sub
    except CFError:
        pass
    new_sub = "rix" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    data = cf_request(
        "PUT",
        f"/accounts/{account_id}/workers/subdomain",
        token,
        json={"subdomain": new_sub},
    )
    return (data.get("result") or {}).get("subdomain", new_sub)


def cf_enable_workers_dev(token: str, account_id: str, script_name: str) -> None:
    cf_request(
        "POST",
        f"/accounts/{account_id}/workers/scripts/{script_name}/subdomain",
        token,
        json={"enabled": True, "previews_enabled": False},
    )


def deploy_step_kv(token, account_id):
    kv_name = random_kv_name()
    ns_id = cf_create_kv(token, account_id, kv_name)
    return kv_name, ns_id


def deploy_step_worker(token, account_id, code, ns_id):
    script_name = random_worker_name()
    cf_deploy_worker(token, account_id, script_name, code, ns_id)
    return script_name


def deploy_step_url(token, account_id, script_name):
    url = None
    try:
        sub = cf_get_or_create_subdomain(token, account_id)
        try:
            cf_enable_workers_dev(token, account_id, script_name)
        except CFError as exc:
            log.warning("enable workers.dev failed: %s", exc.detail)
        url = f"https://{script_name}.{sub}.workers.dev"
    except CFError as exc:
        log.warning("subdomain failed: %s", exc.detail)
    return url


# ----------------------------------------------------------------------------
# متن‌ها و کیبوردها
# ----------------------------------------------------------------------------
def esc(s) -> str:
    return html.escape(str(s))


def channel_url() -> str:
    return "https://t.me/" + FORCE_CHANNEL.lstrip("@")


def kb_join() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("عضویت در کانال", url=channel_url())],
            [InlineKeyboardButton("بررسی عضویت", callback_data="check_sub")],
        ]
    )


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Deploy Worker", callback_data="deploy")],
            [InlineKeyboardButton("🔑 آموزش ساخت API Token", callback_data="guide")],
            [InlineKeyboardButton("👤 حساب من", callback_data="account")],
        ]
    )


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="menu")]])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="cancel")]])


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 آپلود worker.js", callback_data="adm_upload")],
            [InlineKeyboardButton("🗑 حذف worker", callback_data="adm_delete")],
            [InlineKeyboardButton("📊 وضعیت worker", callback_data="adm_status")],
            [InlineKeyboardButton("👥 تعداد کاربران", callback_data="adm_users")],
        ]
    )


MENU_TEXT = (
    "☁️ <b>RIX PANEL Worker Deployer</b>\n\n"
    "با این ربات می‌توانید Worker را به‌صورت خودکار روی اکانت Cloudflare خودتان Deploy کنید.\n\n"
    "یکی از گزینه‌ها را انتخاب کنید:"
)

GUIDE_TEXT = (
    "🔑 <b>آموزش ساخت Cloudflare API Token</b>\n\n"
    f"1️⃣ وارد این لینک شوید:\n{CF_TOKEN_URL}\n\n"
    "2️⃣ روی <b>Create Token</b> بزنید و گزینه <b>Create Custom Token</b> را انتخاب کنید.\n\n"
    "3️⃣ دسترسی‌ها (Permissions) را دقیقاً اینطور اضافه کنید:\n"
    "• Account ← Workers Scripts ← <b>Edit</b>\n"
    "• Account ← Workers KV Storage ← <b>Edit</b>\n"
    "• Account ← Account Settings ← <b>Read</b> (برای شناسایی خودکار Account ID)\n\n"
    "4️⃣ در بخش Account Resources گزینه Include ← اکانت خودتان را انتخاب کنید.\n\n"
    "5️⃣ روی Continue to summary و سپس Create Token بزنید.\n\n"
    "6️⃣ توکن نمایش داده‌شده را کپی کنید و در ربات بفرستید.\n\n"
    "🔐 توکن شما ذخیره نمی‌شود و بلافاصله بعد از Deploy حذف خواهد شد."
)

ERR_MESSAGES = {
    "invalid_token": "❌ <b>توکن Cloudflare نامعتبر است.</b>\nتوکن را دوباره بررسی کنید.",
    "permission": "❌ <b>دسترسی کافی نیست (Permission Denied).</b>\nمطمئن شوید Workers Scripts و Workers KV Storage روی Edit هستند.",
    "kv": "❌ <b>ساخت KV Namespace ناموفق بود.</b>",
    "deploy": "❌ <b>Deploy کردن Worker ناموفق بود.</b>",
    "other": "❌ <b>خطای نامشخص از Cloudflare.</b>",
}


def err_text(exc: CFError) -> str:
    base = ERR_MESSAGES.get(exc.kind, ERR_MESSAGES["other"])
    if exc.detail:
        base += f"\n\n<code>{esc(exc.detail[:500])}</code>"
    return base


# ----------------------------------------------------------------------------
# عضویت اجباری
# ----------------------------------------------------------------------------
async def is_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        member = await context.bot.get_chat_member(FORCE_CHANNEL, user_id)
    except TelegramError as exc:
        log.warning("get_chat_member failed (ربات باید ادمین کانال باشد): %s", exc)
        return False
    if member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):
        return True
    if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
        return True
    return False


async def ask_join(update: Update) -> None:
    text = "📢 <b>ابتدا عضو کانال شوید</b>\n\nبعد از عضویت روی «بررسی عضویت» بزنید."
    if update.callback_query:
        await update.callback_query.answer("ابتدا عضو کانال شوید", show_alert=True)
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=kb_join()
            )
        except TelegramError:
            pass
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb_join()
        )


# ----------------------------------------------------------------------------
# وضعیت هر کاربر / داده‌ها (در حافظه)
# ----------------------------------------------------------------------------
def register_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    context.bot_data.setdefault("users", set()).add(user_id)


def clear_user_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("state", None)
    context.user_data.pop("pending", None)  # شامل توکن موقت


# ----------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(context, user.id)
    clear_user_state(context)
    if not await is_member(context, user.id):
        return await ask_join(update)
    await update.effective_message.reply_text(
        MENU_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_main()
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        return  # فقط مالک
    clear_user_state(context)
    await update.effective_message.reply_text(
        "🔐 <b>پنل مالک</b>", parse_mode=ParseMode.HTML, reply_markup=kb_admin()
    )


async def show_menu(query):
    await query.edit_message_text(
        MENU_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_main()
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    data = query.data or ""
    register_user(context, user.id)

    # ---- پنل مالک ----
    if data.startswith("adm_"):
        await query.answer()
        if user.id != OWNER_ID:
            return
        return await admin_callbacks(update, context, data)

    # ---- بررسی عضویت ----
    if data == "check_sub":
        if await is_member(context, user.id):
            await query.answer("✅ عضویت تایید شد")
            return await show_menu(query)
        await query.answer("❌ هنوز عضو کانال نیستید", show_alert=True)
        return

    if not await is_member(context, user.id):
        return await ask_join(update)

    await query.answer()

    if data == "menu":
        clear_user_state(context)
        return await show_menu(query)

    if data == "cancel":
        clear_user_state(context)
        return await show_menu(query)

    if data == "guide":
        return await query.edit_message_text(
            GUIDE_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back(),
            disable_web_page_preview=True,
        )

    if data == "account":
        return await show_account(query, context, user)

    if data == "deploy":
        if not context.bot_data.get("worker"):
            return await query.edit_message_text(
                "❌ <b>فایل worker.js هنوز توسط مالک آپلود نشده است.</b>\n"
                "لطفاً بعداً دوباره تلاش کنید.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back(),
            )
        if context.user_data.get("busy"):
            return await query.answer("⏳ عملیات قبلی هنوز در حال انجام است", show_alert=True)
        context.user_data["state"] = "await_token"
        return await query.edit_message_text(
            "☁️ <b>مرحله اول: دریافت Cloudflare API Token</b>\n\n"
            "توکن خود را بفرستید.\n"
            f"ساخت توکن: {CF_TOKEN_URL}\n\n"
            "دسترسی‌های لازم:\n"
            "• Account ← Workers Scripts ← Edit\n"
            "• Account ← Workers KV Storage ← Edit\n\n"
            "🔐 توکن فقط هنگام Deploy استفاده شده و سپس حذف می‌شود.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_cancel(),
            disable_web_page_preview=True,
        )

    if data.startswith("acc:"):
        pending = context.user_data.get("pending")
        if not pending:
            return await query.edit_message_text(
                "❌ نشست منقضی شده است. دوباره Deploy را شروع کنید.",
                reply_markup=kb_back(),
            )
        try:
            idx = int(data.split(":", 1)[1])
            account = pending["accounts"][idx]
        except (ValueError, IndexError):
            return await query.edit_message_text("❌ انتخاب نامعتبر.", reply_markup=kb_back())
        token = pending["token"]
        context.user_data.pop("pending", None)
        await query.edit_message_text(
            f"☁️ اکانت انتخاب شد: <b>{esc(account['name'])}</b>",
            parse_mode=ParseMode.HTML,
        )
        return await run_deploy(query.message, context, user.id, token, account)


async def show_account(query, context, user):
    deploys = context.bot_data.get("deploys", {}).get(user.id, [])
    lines = [
        "👤 <b>حساب من</b>\n",
        f"🆔 آیدی: <code>{user.id}</code>",
        f"📛 نام: {esc(user.full_name)}",
        f"🚀 تعداد Deploy موفق: {len(deploys)}",
    ]
    if deploys:
        lines.append("\n<b>آخرین Deployها:</b>")
        for d in deploys[-5:][::-1]:
            lines.append(
                f"• {esc(d['url'] or d['worker'])}\n  KV: <code>{esc(d['kv'])}</code>"
            )
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back(),
        disable_web_page_preview=True,
    )


# ---------------------------- پنل مالک ----------------------------
async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    back = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data="adm_home")]]
    )

    if data == "adm_home":
        context.user_data.pop("state", None)
        return await query.edit_message_text(
            "🔐 <b>پنل مالک</b>", parse_mode=ParseMode.HTML, reply_markup=kb_admin()
        )

    if data == "adm_upload":
        context.user_data["state"] = "await_worker"
        return await query.edit_message_text(
            "📤 فایل <b>worker.js</b> را به‌صورت Document ارسال کنید.",
            parse_mode=ParseMode.HTML,
            reply_markup=back,
        )

    if data == "adm_delete":
        if context.bot_data.pop("worker", None):
            text = "✅ worker ذخیره‌شده حذف شد."
        else:
            text = "❌ هیچ workerی ذخیره نشده است."
        return await query.edit_message_text(text, reply_markup=back)

    if data == "adm_status":
        w = context.bot_data.get("worker")
        if not w:
            text = "❌ <b>وضعیت:</b> worker ذخیره نشده است."
        else:
            text = (
                "✅ <b>وضعیت:</b> worker آماده است\n\n"
                f"📄 نام فایل: <code>{esc(w['filename'])}</code>\n"
                f"📦 حجم: {w['size']:,} بایت\n"
                f"🧩 نوع: {'ES Module' if w['module'] else 'Service Worker'}\n"
                f"🕒 زمان آپلود: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(w['ts']))}"
            )
        return await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=back
        )

    if data == "adm_users":
        users = context.bot_data.get("users", set())
        total_deploys = sum(len(v) for v in context.bot_data.get("deploys", {}).values())
        return await query.edit_message_text(
            f"👥 تعداد کاربران: <b>{len(users)}</b>\n🚀 مجموع Deployهای موفق: <b>{total_deploys}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=back,
        )


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if user.id != OWNER_ID:
        return
    doc = msg.document
    if context.user_data.get("state") != "await_worker" and not (
        doc.file_name or ""
    ).lower().endswith(".js"):
        return
    if not (doc.file_name or "").lower().endswith(".js"):
        return await msg.reply_text("❌ فایل باید با پسوند .js باشد (worker.js).")
    if doc.file_size and doc.file_size > MAX_WORKER_SIZE:
        return await msg.reply_text("❌ حجم فایل بیش از حد مجاز است.")

    try:
        tg_file = await doc.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
        code = raw.decode("utf-8")
    except UnicodeDecodeError:
        return await msg.reply_text("❌ فایل باید با کدگذاری UTF-8 باشد.")
    except TelegramError as exc:
        return await msg.reply_text(f"❌ دانلود فایل ناموفق بود: {esc(exc)}")

    if not code.strip():
        return await msg.reply_text("❌ فایل خالی است.")

    context.bot_data["worker"] = {
        "code": code,
        "filename": doc.file_name,
        "size": len(raw),
        "module": is_module_worker(code),
        "ts": time.time(),
    }
    context.user_data.pop("state", None)
    await msg.reply_text(
        f"✅ فایل <code>{esc(doc.file_name)}</code> ذخیره شد ({len(raw):,} بایت).",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_admin(),
    )


# ---------------------------- Deploy ----------------------------
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    register_user(context, user.id)

    if context.user_data.get("state") != "await_token":
        return

    if not await is_member(context, user.id):
        return await ask_join(update)

    token = (msg.text or "").strip()

    # حذف پیام حاوی توکن از چت
    try:
        await msg.delete()
    except TelegramError:
        pass

    if context.user_data.get("busy"):
        return
    if len(token) < 20 or re.search(r"\s", token):
        return await msg.chat.send_message(
            "❌ <b>توکن Cloudflare نامعتبر است.</b>\nدوباره بفرستید یا انصراف دهید.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_cancel(),
        )

    context.user_data.pop("state", None)
    context.user_data["busy"] = True
    status = await msg.chat.send_message("🔐 در حال بررسی توکن Cloudflare ...")

    try:
        try:
            accounts = await asyncio.to_thread(cf_get_accounts, token)
        except CFError as exc:
            await status.edit_text(
                err_text(exc), parse_mode=ParseMode.HTML, reply_markup=kb_back()
            )
            return

        if len(accounts) == 1:
            await run_deploy(status, context, user.id, token, accounts[0])
        else:
            # چند اکانت: توکن تا انتخاب کاربر به‌صورت موقت نگه‌داری می‌شود
            context.user_data["pending"] = {"token": token, "accounts": accounts}
            buttons = [
                [InlineKeyboardButton(a["name"][:40], callback_data=f"acc:{i}")]
                for i, a in enumerate(accounts[:20])
            ]
            buttons.append([InlineKeyboardButton("❌ انصراف", callback_data="cancel")])
            await status.edit_text(
                "☁️ چند اکانت پیدا شد. یکی را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
    except Exception as exc:  # noqa
        log.exception("unexpected error")
        try:
            await status.edit_text(
                f"❌ خطای غیرمنتظره: <code>{esc(exc)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back(),
            )
        except TelegramError:
            pass
    finally:
        token = None  # حذف توکن از حافظه
        context.user_data["busy"] = False


async def run_deploy(status_msg, context, user_id: int, token: str, account: dict):
    """مراحل: ساخت KV ← Deploy Worker با Binding ← فعال‌سازی workers.dev"""
    worker = context.bot_data.get("worker")
    if not worker:
        await status_msg.edit_text(
            "❌ <b>فایل worker.js وجود ندارد (Missing worker.js).</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back(),
        )
        return

    account_id = account["id"]
    ns_id = None
    context.user_data["busy"] = True
    try:
        await status_msg.edit_text("☁️ در حال ساخت KV Namespace ...")
        kv_name, ns_id = await asyncio.to_thread(deploy_step_kv, token, account_id)

        await status_msg.edit_text("🚀 در حال Deploy کردن Worker و اتصال KV ...")
        script_name = await asyncio.to_thread(
            deploy_step_worker, token, account_id, worker["code"], ns_id
        )

        await status_msg.edit_text("☁️ در حال فعال‌سازی آدرس workers.dev ...")
        url = await asyncio.to_thread(deploy_step_url, token, account_id, script_name)

        context.bot_data.setdefault("deploys", {}).setdefault(user_id, []).append(
            {"worker": script_name, "kv": kv_name, "url": url, "ts": time.time()}
        )

        if url:
            worker_line = f"🌐 Worker:\n{esc(url)}"
        else:
            worker_line = (
                f"🌐 Worker: <code>{esc(script_name)}</code>\n"
                "⚠️ آدرس workers.dev به‌صورت خودکار فعال نشد؛ از داشبورد Cloudflare فعال کنید."
            )
        await status_msg.edit_text(
            "✅ <b>Deploy موفق شد</b>\n\n"
            f"{worker_line}\n\n"
            f"🗄 KV: <code>{esc(kv_name)}</code>\n"
            "🔗 Binding: <code>kv</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back(),
            disable_web_page_preview=True,
        )
    except CFError as exc:
        # Rollback: حذف KV ساخته‌شده اگر Deploy کامل نشد
        if ns_id and exc.kind in ("deploy", "permission", "other", "invalid_token"):
            await asyncio.to_thread(cf_delete_kv, token, account_id, ns_id)
        await status_msg.edit_text(
            err_text(exc), parse_mode=ParseMode.HTML, reply_markup=kb_back()
        )
    except Exception as exc:  # noqa
        log.exception("deploy error")
        if ns_id:
            await asyncio.to_thread(cf_delete_kv, token, account_id, ns_id)
        await status_msg.edit_text(
            f"❌ <b>Deploy ناموفق بود:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back(),
        )
    finally:
        token = None  # حذف توکن
        context.user_data.pop("pending", None)
        context.user_data["busy"] = False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception", exc_info=context.error)


# ----------------------------------------------------------------------------
# اجرا
# ----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        log.error("❌ BOT_TOKEN تنظیم نشده است.")
        sys.exit(1)
    if not OWNER_ID:
        log.warning("⚠️ OWNER_ID تنظیم نشده یا نامعتبر است؛ پنل مالک غیرفعال خواهد بود.")

    try:
        app = Application.builder().token(BOT_TOKEN).build()
    except InvalidToken:
        log.error("❌ Invalid Telegram Token")
        sys.exit(1)

    app.bot_data["users"] = set()
    app.bot_data["deploys"] = {}

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    log.info("🚀 RIX PANEL Worker Deployer started (polling)")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except InvalidToken:
        log.error("❌ Invalid Telegram Token")
        sys.exit(1)


if __name__ == "__main__":
    main()
