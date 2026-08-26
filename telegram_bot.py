# telegram_bot.py
# ══════════════════════════════════════════════════════════════════════════════
# ربات مدیریت تلگرام — فقط با /login وارد پنل بشو.
# خرید کانفیگ → در انتظار تایید ادمین → ادمین تایید/رد → ارسال کانفیگ.
# ══════════════════════════════════════════════════════════════════════════════

import asyncio
import re

import httpx

from datetime import datetime, timedelta

from main import (
    LINKS,
    make_link,
    remove_link,
    set_link_active,
    vless_link_for_link,
    get_host,
    fmt_bytes,
    is_link_allowed,
    logger,
    add_bot_log,
    PROTOCOLS,
    DEFAULT_PROTOCOL,
    FINGERPRINTS,
    DEFAULT_FINGERPRINT,
    DEFAULT_ALPN_BY_PROTOCOL,
    DEFAULT_PORT,
    DEFAULT_SPEED_LIMIT,
    MIN_PORT,
    MAX_PORT,
    parse_size_to_bytes,
    parse_speed_to_bytes,
    TELEGRAM_CONFIG,
    PLANS,
    TRIALS,
    active_group,
    create_bundle,
    create_trial,
    _bundle_items,
)
from discord_bot import PAYMENT_CARD, PAYMENT_HOLDER

def _bot_token() -> str:
    return (TELEGRAM_CONFIG.get("token") or "").strip()

def _admin_ids() -> set:
    raw = (TELEGRAM_CONFIG.get("admin_ids") or "").strip()
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()} if raw else set()

def _admin_password() -> str:
    return (TELEGRAM_CONFIG.get("admin_password") or "nerula2024").strip()

def _channel_id() -> str:
    return (TELEGRAM_CONFIG.get("channel_id") or "").strip()

def _refresh_config():
    global BOT_TOKEN, ADMIN_IDS, API_BASE, CHANNEL_ID
    BOT_TOKEN = _bot_token()
    ADMIN_IDS = _admin_ids()
    CHANNEL_ID = _channel_id()
    API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

BOT_TOKEN = _bot_token()
ADMIN_IDS = _admin_ids()
CHANNEL_ID = _channel_id()

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
PAGE_SIZE = 6

_client: httpx.AsyncClient | None = None
_poll_task: asyncio.Task | None = None
_running = False
_pending: dict = {}   # chat_id -> {"action": "wizard", "step": "...", "data": {...}}

# ── وضعیت پرداخت‌های در انتظار رسید ────────────────────────────────────────────
_payments: dict = {}   # chat_id -> {"plan_id", "stage"}

# ── لاگین ادمین‌ها ────────────────────────────────────────────────────────────
_admin_sessions: set = set()   # chat_id هایی که با /login وارد شدن

# ── در انتظار تایید ادمین ─────────────────────────────────────────────────────
_pending_approvals: dict = {}   # user_id -> {"plan_name","main_uid","user_chat_id","requested_by"}

def _fa_price(n) -> str:
    try:
        n = int(float(n or 0))
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "رایگان"
    fa = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    return f"{n:,}".replace(",", "٬").translate(fa) + " تومان"

def _fa_vol(n) -> str:
    if not n:
        return "نامحدود"
    gb = n / 1024 ** 3
    if gb >= 1:
        return f"{gb:g} گیگ"
    mb = n / 1024 ** 2
    return f"{mb:g} مگ"

# ── Config creation wizard ────────────────────────────────────────────────────
WIZARD_STEPS = ["label", "protocol", "fingerprint", "alpn", "port", "volume", "speed", "iplimit", "days"]

PROTOCOL_LABELS = {
    "vless-ws": "VLESS + WebSocket",
    "xhttp": "XHTTP (mode: auto)",
}

def _protocol_label(p: str) -> str:
    return PROTOCOL_LABELS.get(p, p)

def _fp_label(fp: str) -> str:
    return fp.capitalize()

_VOLUME_RE = re.compile(r"^([\d.]+)\s*(GB|MB|KB)?$", re.IGNORECASE)
_SPEED_RE = re.compile(r"^([\d.]+)\s*(MBIT|MBPS|MB|KB)?$", re.IGNORECASE)

def _parse_volume_text(text: str):
    m = _VOLUME_RE.match(text.strip())
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    if value <= 0:
        return 0
    unit = (m.group(2) or "GB").upper()
    return parse_size_to_bytes(value, unit)

def _parse_speed_text(text: str):
    m = _SPEED_RE.match(text.strip())
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    if value <= 0:
        return 0
    unit_raw = (m.group(2) or "MBIT").upper()
    unit = "MBIT" if unit_raw in ("MBIT", "MBPS") else unit_raw
    return parse_speed_to_bytes(value, unit)

def _parse_nonneg_int(text: str):
    try:
        n = int(text.strip())
    except ValueError:
        return None
    return max(0, n)

# ── Telegram API helpers ────────────────────────────────────────────────────
_api_lock = asyncio.Lock()

async def _call(method: str, **params):
    if _client is None:
        return None
    async with _api_lock:
        try:
            r = await _client.post(f"{API_BASE}/{method}", json=params, timeout=40)
            data = r.json()
            if not data.get("ok"):
                logger.warning(f"Telegram API {method} failed: {data}")
            return data
        except Exception as e:
            logger.warning(f"Telegram API {method} error: {e}")
            return None

async def _send(chat_id: int, text: str, kb: dict | None = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        payload["reply_markup"] = kb
    return await _call("sendMessage", **payload)

async def _edit(chat_id: int, message_id: int, text: str, kb: dict | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        payload["reply_markup"] = kb
    res = await _call("editMessageText", **payload)
    if res is None or not res.get("ok"):
        logger.warning(f"Telegram editMessageText failed, falling back to new message: {res}")
        await _send(chat_id, text, kb)

async def _answer_cb(cb_id: str, text: str = ""):
    if _client is None:
        return
    try:
        async with _api_lock:
            await _client.post(f"{API_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": text}, timeout=15)
    except Exception as e:
        logger.warning(f"Telegram answerCallbackQuery error: {e}")

def _is_admin(chat_id: int) -> bool:
    return chat_id in ADMIN_IDS

def _is_logged_in(chat_id: int) -> bool:
    return chat_id in _admin_sessions

# ── Keyboards ────────────────────────────────────────────────────────────────
def _main_menu_kb():
    rows = [
        [{"text": "🛒 خرید کانفیگ", "callback_data": "shop"}],
        [{"text": "🧪 تست رایگان ۱۰۰ مگ", "callback_data": "trial"}],
        [{"text": "📋 کانفیگ‌های من", "callback_data": "list:0"}],
    ]
    rows.append([{"text": "🔄 رفرش", "callback_data": "menu"}])
    return {"inline_keyboard": rows}

def _admin_menu_kb():
    rows = [
        [{"text": "🛒 خرید کانفیگ", "callback_data": "shop"}],
        [{"text": "🧪 تست رایگان ۱۰۰ مگ", "callback_data": "trial"}],
        [{"text": "📋 کانفیگ‌های من", "callback_data": "list:0"}],
        [{"text": "➕ ساخت کانفیگ جدید", "callback_data": "newcfg"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "settings"}],
        [{"text": "🔄 رفرش", "callback_data": "menu"}],
    ]
    return {"inline_keyboard": rows}

def _settings_kb():
    return {"inline_keyboard": [
        [{"text": "🔄 رفرش تنظیمات", "callback_data": "settings"}],
        [{"text": "⬅ منوی اصلی", "callback_data": "menu"}],
    ]}

def _approval_kb(user_id: int):
    return {"inline_keyboard": [
        [{"text": "✅ تایید", "callback_data": f"approve:{user_id}"},
         {"text": "❌ رد", "callback_data": f"reject:{user_id}"}],
    ]}

def _shop_kb():
    rows = []
    for pid, p in PLANS.items():
        price = int(p.get("price") or 0)
        if price <= 0:
            continue
        rows.append([{"text": f"{p.get('emoji', '')} {p.get('name', '')} — {_fa_price(price)}", "callback_data": f"pay:{pid}"}])
    rows.append([{"text": "⬅ منوی اصلی", "callback_data": "menu"}])
    return {"inline_keyboard": rows}

def _payment_kb(pid: str):
    return {"inline_keyboard": [
        [{"text": "✅ پرداخت کردم", "callback_data": f"buy:paid:{pid}"}],
        [{"text": "❌ لغو", "callback_data": "shop"}],
    ]}

def _receipt_cancel_kb():
    return {"inline_keyboard": [[{"text": "❌ لغو خرید", "callback_data": "receipt:cancel"}]]}

def _links_list_kb(page: int):
    items = sorted(LINKS.items(), key=lambda kv: kv[1].get("created_at", ""), reverse=True)
    total = len(items)
    start = page * PAGE_SIZE
    chunk = items[start:start + PAGE_SIZE]
    rows = []
    for uid, l in chunk:
        dot = "🟢" if is_link_allowed(l) else "🔴"
        rows.append([{"text": f"{dot} {l.get('label','?')[:28]}", "callback_data": f"view:{uid}"}])
    nav = []
    if start > 0:
        nav.append({"text": "◀ قبلی", "callback_data": f"list:{page-1}"})
    if start + PAGE_SIZE < total:
        nav.append({"text": "بعدی ▶", "callback_data": f"list:{page+1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "➕ ساخت کانفیگ جدید", "callback_data": "newcfg"}])
    rows.append([{"text": "⬅ منوی اصلی", "callback_data": "menu"}])
    return {"inline_keyboard": rows}

def _link_detail_kb(uid: str, active: bool):
    return {"inline_keyboard": [
        [{"text": "🔗 نمایش لینک اتصال", "callback_data": f"link:{uid}"}],
        [{"text": ("⛔ غیرفعال‌سازی" if active else "✅ فعال‌سازی"), "callback_data": f"toggle:{uid}"}],
        [{"text": "🗑 حذف کانفیگ", "callback_data": f"del:{uid}"}],
        [{"text": "⬅ بازگشت به لیست", "callback_data": "list:0"}],
    ]}

def _confirm_delete_kb(uid: str):
    return {"inline_keyboard": [
        [{"text": "✅ بله، حذف کن", "callback_data": f"delok:{uid}"},
         {"text": "❌ انصراف", "callback_data": f"view:{uid}"}],
    ]}

# ── Wizard keyboards ─────────────────────────────────────────────────────────
def _wizard_cancel_kb():
    return {"inline_keyboard": [[{"text": "❌ انصراف", "callback_data": "w:cancel"}]]}

def _wizard_protocol_kb():
    rows = [[{"text": _protocol_label(p), "callback_data": f"w:proto:{p}"}] for p in PROTOCOLS]
    rows.append([{"text": "❌ انصراف", "callback_data": "w:cancel"}])
    return {"inline_keyboard": rows}

def _wizard_fp_kb():
    rows, row = [], []
    for fp in FINGERPRINTS:
        row.append({"text": _fp_label(fp), "callback_data": f"w:fp:{fp}"})
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "❌ انصراف", "callback_data": "w:cancel"}])
    return {"inline_keyboard": rows}

def _wizard_skip_kb(step_key: str, label: str):
    return {"inline_keyboard": [
        [{"text": label, "callback_data": f"w:skip:{step_key}"}],
        [{"text": "❌ انصراف", "callback_data": "w:cancel"}],
    ]}

ALPN_PRESET_MAP = {"p1": "http/1.1", "p2": "h2,http/1.1", "p3": "h2"}

def _wizard_alpn_kb():
    return {"inline_keyboard": [
        [{"text": "🔤 http/1.1 (پیشنهادی)", "callback_data": "w:alpnpreset:p1"}],
        [{"text": "🔤 h2,http/1.1", "callback_data": "w:alpnpreset:p2"}],
        [{"text": "🔤 h2", "callback_data": "w:alpnpreset:p3"}],
        [{"text": "⏭ پیش‌فرض پروتکل", "callback_data": "w:skip:alpn"}],
        [{"text": "❌ انصراف", "callback_data": "w:cancel"}],
    ]}

def _wizard_unlimited_kb(step_key: str):
    return _wizard_skip_kb(step_key, "♾ نامحدود")

def _wizard_confirm_kb():
    return {"inline_keyboard": [
        [{"text": "✅ ساخت کانفیگ", "callback_data": "w:confirm"}],
        [{"text": "❌ انصراف", "callback_data": "w:cancel"}],
    ]}

def _wizard_prompt(step: str, data: dict) -> str:
    n = WIZARD_STEPS.index(step) + 1 if step in WIZARD_STEPS else len(WIZARD_STEPS)
    head = f"🧩 ساخت کانفیگ جدید — مرحله {n}/{len(WIZARD_STEPS)}\n\n"
    if step == "label":
        return head + "✏️ اسم/برچسب کانفیگ رو بفرست:"
    if step == "protocol":
        return head + "🌐 پروتکل رو از دکمه‌های زیر انتخاب کن:"
    if step == "fingerprint":
        return head + "🖐 Fingerprint (uTLS) رو انتخاب کن:"
    if step == "alpn":
        return head + ("🔤 ALPN رو از دکمه‌های زیر انتخاب کن (پیشنهادی: <code>http/1.1</code>)\n"
                        "یا خودت هر مقدار دلخواهی رو تایپ و ارسال کن (مثلاً h2,http/1.1):")
    if step == "port":
        return head + f"🔌 شماره پورت (بین {MIN_PORT} تا {MAX_PORT}) رو بفرست\nیا پیش‌فرض ({DEFAULT_PORT}) رو انتخاب کن:"
    if step == "volume":
        return head + "📦 محدودیت حجم مصرفی رو بفرست، مثلاً:\n<code>10GB</code> یا <code>500MB</code>\nیا دکمه‌ی نامحدود رو بزن:"
    if step == "speed":
        return head + "🚀 محدودیت سرعت رو به مگابیت‌بر‌ثانیه بفرست، مثلاً <code>20</code>\nیا دکمه‌ی نامحدود رو بزن:"
    if step == "iplimit":
        return head + "👥 حداکثر تعداد آی‌پی/کاربر هم‌زمان مجاز رو بفرست\nیا دکمه‌ی نامحدود رو بزن:"
    if step == "days":
        return head + "📅 تعداد روزهای اعتبار کانفیگ رو بفرست\nیا دکمه‌ی نامحدود (بدون انقضا) رو بزن:"
    return head

def _wizard_summary(data: dict) -> str:
    limit = "نامحدود" if not data.get("limit_bytes") else fmt_bytes(data["limit_bytes"])
    speed = "نامحدود" if not data.get("speed_limit_bytes") else f"{data['speed_limit_bytes']*8/1024/1024:.1f} Mbps"
    iplim = data.get("ip_limit", 0) or "نامحدود"
    days = data.get("expires_days", 0)
    days_txt = "بدون انقضا" if not days else f"{days} روز"
    proto = data.get("protocol", DEFAULT_PROTOCOL)
    alpn = data.get("alpn") or f"پیش‌فرض ({DEFAULT_ALPN_BY_PROTOCOL.get(proto, 'http/1.1')})"
    return (
        "🧩 خلاصه‌ی کانفیگ جدید — تایید کن:\n\n"
        f"برچسب: <b>{data.get('label','?')}</b>\n"
        f"پروتکل: {_protocol_label(proto)}\n"
        f"Fingerprint: {_fp_label(data.get('fingerprint', DEFAULT_FINGERPRINT))}\n"
        f"ALPN: {alpn}\n"
        f"پورت: {data.get('port', DEFAULT_PORT)}\n"
        f"محدودیت حجم: {limit}\n"
        f"محدودیت سرعت: {speed}\n"
        f"محدودیت آی‌پی: {iplim}\n"
        f"انقضا: {days_txt}"
    )

# ── View builders ────────────────────────────────────────────────────────────
def _shop_text() -> str:
    head = "🛒 <b>خرید کانفیگ</b> — پلن مورد نظرت رو انتخاب کن:\n\n"
    lines = []
    for pid, p in PLANS.items():
        price = int(p.get("price") or 0)
        if price <= 0:
            continue
        days = int(p.get("days") or 30)
        lines.append(f"{p.get('emoji', '')} <b>{p.get('name', '')}</b>\n   حجم: {_fa_vol(p.get('limit_bytes', 0))} · اعتبار {days} روز\n   💰 {_fa_price(price)}")
    return head + "\n".join(lines) + "\n\nهمه‌ی پلن‌ها <b>۱ ماهه</b> هستن."

def _payment_text(plan: dict) -> str:
    return (
        "💳 <b>پرداخت با کارت</b>\n\n"
        f"{plan.get('emoji', '')} <b>{plan.get('name', '')}</b>\n"
        f"💰 مبلغ: <b>{_fa_price(plan.get('price'))}</b>\n"
        f"💳 شماره کارت:\n<code>{PAYMENT_CARD}</code>\n"
        f"👤 به نام: <b>{PAYMENT_HOLDER}</b>\n"
        f"📦 حجم: {_fa_vol(plan.get('limit_bytes', 0))}\n"
        f"📅 اعتبار: {int(plan.get('days') or 30)} روز\n\n"
        "بعد از واریز دکمه‌ی «✅ پرداخت کردم» رو بزن، بعد <b>عکس رسید</b> رو همین‌جا بفرست."
    )

def _receipt_prompt_text(plan: dict) -> str:
    return (
        f"🖼 انتخاب تو: {plan.get('emoji', '')} <b>{plan.get('name', '')}</b> — مبلغ <b>{_fa_price(plan.get('price'))}</b>\n\n"
        "بعد از واریز، <b>عکس رسید</b> رو همین‌جا (توی همین چت) بفرست تا کانفیگت تحویل داده بشه."
    )

def _deliver_text(res: dict) -> str:
    host = get_host()
    main_uid = res["main_uid"]
    sub_url = f"https://{host}/sub/{main_uid}"
    public_url = f"https://{host}/p/{main_uid}"
    lines = [f"✅ کانفیگت آماده‌ست — {res.get('plan', '')}"]
    for m in res.get("members", []):
        mu = m.get("uid") if isinstance(m, dict) else m[0]
        ml = m.get("link") if isinstance(m, dict) else m[1]
        lines.append(f"🔗 {ml.get('label', '')}:\n<code>{vless_link_for_link(ml, mu, host)}</code>")
    lines.append(f"\n📥 لینک ساب (همه با هم):\n<code>{sub_url}</code>")
    lines.append(f"✨ صفحه‌ی ساب:\n<code>{public_url}</code>")
    return "\n\n".join(lines)

def _links_text(uid: str, l: dict) -> str:
    host = get_host()
    sub_url = f"https://{host}/sub/{uid}"
    public_url = f"https://{host}/p/{uid}"
    items = _bundle_items(uid) or [(uid, l)]
    lines = [f"🔗 کانفیگ «{l.get('label', '')}» — {len(items)} کانفیگ داخل ساب:"]
    for mu, ml in items:
        lines.append(f"🔗 {ml.get('label', '')}:\n<code>{vless_link_for_link(ml, mu, host)}</code>")
    lines.append(f"\n📥 لینک ساب (همه با هم):\n<code>{sub_url}</code>")
    lines.append(f"✨ صفحه‌ی ساب:\n<code>{public_url}</code>")
    return "\n\n".join(lines)

def _owned_items(user_id: int):
    items = sorted(LINKS.items(), key=lambda kv: kv[1].get("created_at", ""), reverse=True)
    items = [(uid, l) for uid, l in items if l.get("sub_members") or not l.get("bundle_main")]
    if user_id in ADMIN_IDS:
        return items
    return [(uid, l) for uid, l in items if str(l.get("owner", "") or "") == str(user_id)]

def _can_view(user_id: int, uid: str) -> bool:
    if user_id in ADMIN_IDS:
        return True
    owner = str(LINKS.get(uid, {}).get("owner", "") or "")
    return bool(owner) and owner == str(user_id)

def _chunk_send(text: str, size: int = 3800):
    return [text[i:i + size] for i in range(0, len(text), size)]

def _format_detail(uid: str, l: dict) -> str:
    status = "🟢 فعال" if is_link_allowed(l) else "🔴 غیرفعال/منقضی"
    limit = "نامحدود" if not l.get("limit_bytes") else fmt_bytes(l["limit_bytes"])
    speed = "نامحدود" if not l.get("speed_limit_bytes") else f"{l['speed_limit_bytes']*8/1024/1024:.1f} Mbps"
    exp = l.get("expires_at")
    exp_txt = exp.split("T")[0] if exp else "بدون انقضا"
    proto = l.get("protocol", DEFAULT_PROTOCOL)
    alpn = l.get("alpn") or f"پیش‌فرض ({DEFAULT_ALPN_BY_PROTOCOL.get(proto, 'http/1.1')})"
    return (
        f"<b>{l.get('label','?')}</b>\n"
        f"وضعیت: {status}\n"
        f"مصرف: {fmt_bytes(l.get('used_bytes',0))} / {limit}\n"
        f"محدودیت سرعت: {speed}\n"
        f"محدودیت آی‌پی: {l.get('ip_limit',0) or 'نامحدود'}\n"
        f"پروتکل: {_protocol_label(proto)}\n"
        f"Fingerprint: {_fp_label(l.get('fingerprint', DEFAULT_FINGERPRINT))}\n"
        f"ALPN: {alpn}\n"
        f"پورت: {l.get('port', DEFAULT_PORT)}\n"
        f"انقضا: {exp_txt}\n"
        f"UUID: <code>{uid}</code>"
    )

# ── Update handling ──────────────────────────────────────────────────────────
async def _handle_message(msg: dict):
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None:
        return

    # ثبت در «بات ویور» پنل
    who = (msg.get("from") or {}).get("username") or (msg.get("from") or {}).get("first_name") or "?"
    if msg.get("photo"):
        text = f"[عکس — احتمالاً رسید] {text}"
    if msg.get("document"):
        text = f"[فایل: {(msg.get('document') or {}).get('file_name', '?')}] {text}"
    add_bot_log("telegram", chat_id, who, text)

    # ── اگه ادمین لاگین کرده و پیام می‌فرسته → ممکنه کانفیگ باشه ──
    if _is_logged_in(chat_id) and _is_admin(chat_id):
        if not text.startswith("/"):
            await _handle_admin_message(msg, chat_id, text)
            return

    # ── اگه ادمین لاگین نکرده و رسید فرستاده ──
    pay = _payments.get(chat_id)
    if pay and pay.get("stage") == "waiting_receipt":
        has_photo = bool(msg.get("photo"))
        has_doc = bool(msg.get("document"))
        if has_photo or has_doc:
            await _handle_receipt(chat_id, pay, msg)
            return

    # ── دستورات ──
    if text in ("/start", "/menu"):
        _pending.pop(chat_id, None)
        if _is_admin(chat_id):
            if _is_logged_in(chat_id):
                await _send(chat_id, "👋 خوش اومدی ادمین!\nاز دکمه‌های زیر استفاده کن:", _admin_menu_kb())
            else:
                await _send(chat_id, "👋 به ربات NERULA خوش اومدی.\nبرای دسترسی به پنل ادمین از /login استفاده کن.\n\nاز دکمه‌های زیر هم می‌تونی استفاده کنی:", _main_menu_kb())
        else:
            await _send(chat_id, "👋 به ربات فروش NERULA خوش اومدی.\nاز دکمه‌های زیر استفاده کن:", _main_menu_kb())
        return

    if text == "/login":
        if not _is_admin(chat_id):
            await _send(chat_id, "⛔ شما ادمین نیستید. فقط ادمین‌های مجاز اجازه ورود دارند.")
            return
        pw = text.replace("/login", "").strip()
        if not pw:
            await _send(chat_id, "🔑 رمز ورود را وارد کنید:\n\n<code>/login رمز_ورود</code>")
            return
        if pw == _admin_password():
            _admin_sessions.add(chat_id)
            await _send(chat_id, "✅ ورود موفق!\n\nبه پنل مدیریت خوش اومدی.", _admin_menu_kb())
        else:
            await _send(chat_id, "❌ رمز اشتباه است!")
        return

    if text.startswith("/login ") and len(text.split(None, 1)) > 1:
        pw = text.split(None, 1)[1].strip()
        if not _is_admin(chat_id):
            await _send(chat_id, "⛔ شما ادمین نیستید.")
            return
        if pw == _admin_password():
            _admin_sessions.add(chat_id)
            await _send(chat_id, "✅ ورود موفق!\n\nبه پنل مدیریت خوش اومدی.", _admin_menu_kb())
        else:
            await _send(chat_id, "❌ رمز اشتباه است!")
        return

    if text == "/logout":
        if chat_id in _admin_sessions:
            _admin_sessions.discard(chat_id)
            await _send(chat_id, "✅ از پنل خارج شدی.", _main_menu_kb())
        else:
            await _send(chat_id, "شما در پنل لاگین نبودید.", _main_menu_kb())
        return

    if text == "/trial":
        _pending.pop(chat_id, None)
        await _do_trial(chat_id)
        return

    if text == "/list":
        pass

    if text == "/cancel":
        _pending.pop(chat_id, None)
        await _send(chat_id, "لغو شد.", _main_menu_kb())
        return

    pending = _pending.get(chat_id)

    if pending and pending.get("action") == "wizard" and text:
        step = pending["step"]
        data = pending["data"]

        if step == "label":
            data["label"] = text[:60] or "کانفیگ جدید"
            pending["step"] = "protocol"
            await _send(chat_id, _wizard_prompt("protocol", data), _wizard_protocol_kb())
            return

        if step in ("protocol", "fingerprint"):
            kb = _wizard_protocol_kb() if step == "protocol" else _wizard_fp_kb()
            await _send(chat_id, "لطفاً از دکمه‌های بالا یکی رو انتخاب کن 👆", kb)
            return

        if step == "alpn":
            data["alpn"] = text.strip()[:100]
            pending["step"] = "port"
            await _send(chat_id, _wizard_prompt("port", data), _wizard_skip_kb("port", f"⏭ پیش‌فرض ({DEFAULT_PORT})"))
            return

        if step == "port":
            try:
                p = int(text.strip())
            except ValueError:
                p = None
            if p is None or not (MIN_PORT <= p <= MAX_PORT):
                await _send(chat_id, f"❗️ عدد پورت نامعتبره. یه عدد بین {MIN_PORT} تا {MAX_PORT} بفرست:", _wizard_skip_kb("port", f"⏭ پیش‌فرض ({DEFAULT_PORT})"))
                return
            data["port"] = p
            pending["step"] = "volume"
            await _send(chat_id, _wizard_prompt("volume", data), _wizard_unlimited_kb("volume"))
            return

        if step == "volume":
            parsed = _parse_volume_text(text)
            if parsed is None:
                await _send(chat_id, "❗️ فرمت درست نیست. مثلاً بفرست: <code>10GB</code> یا <code>500MB</code>", _wizard_unlimited_kb("volume"))
                return
            data["limit_bytes"] = parsed
            pending["step"] = "speed"
            await _send(chat_id, _wizard_prompt("speed", data), _wizard_unlimited_kb("speed"))
            return

        if step == "speed":
            parsed = _parse_speed_text(text)
            if parsed is None:
                await _send(chat_id, "❗️ فرمت درست نیست. یه عدد بفرست، مثلاً <code>20</code> (Mbps)", _wizard_unlimited_kb("speed"))
                return
            data["speed_limit_bytes"] = parsed
            pending["step"] = "iplimit"
            await _send(chat_id, _wizard_prompt("iplimit", data), _wizard_unlimited_kb("iplimit"))
            return

        if step == "iplimit":
            n = _parse_nonneg_int(text)
            if n is None:
                await _send(chat_id, "❗️ یه عدد صحیح بفرست:", _wizard_unlimited_kb("iplimit"))
                return
            data["ip_limit"] = n
            pending["step"] = "days"
            await _send(chat_id, _wizard_prompt("days", data), _wizard_unlimited_kb("days"))
            return

        if step == "days":
            n = _parse_nonneg_int(text)
            if n is None:
                await _send(chat_id, "❗️ یه عدد صحیح بفرست (تعداد روز):", _wizard_unlimited_kb("days"))
                return
            data["expires_days"] = n
            pending["step"] = "confirm"
            await _send(chat_id, _wizard_summary(data), _wizard_confirm_kb())
            return

    # پیام ناشناخته → منو رو نشون بده
    if _is_admin(chat_id) and _is_logged_in(chat_id):
        await _send(chat_id, "از دکمه‌های زیر استفاده کن:", _admin_menu_kb())
    else:
        await _send(chat_id, "از دکمه‌های زیر استفاده کن:", _main_menu_kb())

async def _handle_admin_message(msg: dict, chat_id: int, text: str):
    """وقتی ادمین لاگین کرده و پیام می‌فرسته → اگه کانفیگ مورد انتظار باشه، به کاربر فوروارد کن."""
    if chat_id not in _pending_approvals:
        return
    approval = _pending_approvals[chat_id]
    if approval.get("status") != "approved":
        return
    user_chat_id = approval["user_chat_id"]
    try:
        # اگه عکس/فایل باشه
        if msg.get("photo"):
            largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
            await _call("sendPhoto", chat_id=user_chat_id, photo=largest["file_id"], caption="🔗 لینک ساب شما:")
        elif msg.get("document"):
            await _call("sendDocument", chat_id=user_chat_id, document=msg["document"]["file_id"], caption="🔗 لینک ساب شما:")
        else:
            await _send(user_chat_id, f"🔗 بفرمایید لینک ساب کانفیگ تون:\n\n{text}")
        await _send(chat_id, "✅ کانفیگ با موفقیت برای کاربر ارسال شد.")
        _pending_approvals.pop(chat_id, None)
    except Exception as e:
        logger.warning(f"Admin DM forward failed: {e}")
        await _send(chat_id, f"❌ ارسال ناموفق بود: {e}")

async def _handle_receipt(chat_id: int, pay: dict, msg: dict):
    """رسید اومده → تحویل خودکار باندل پلن + ارسال پیام انتظار تایید."""
    if pay.get("stage") == "done":
        await _send(chat_id, "✅ کانفیگت قبلاً تحویل داده شده. اگه سوالی داری با پشتیبانی در ارتباط باش.")
        return
    if pay.get("stage") == "processing":
        await _send(chat_id, "⏳ رسیدت داره پردازش می‌شه، چند لحظه صبر کن…")
        return
    plan = PLANS.get(pay.get("plan_id"))
    if not plan:
        _payments.pop(chat_id, None)
        await _send(chat_id, "❌ این پلن دیگه وجود نداره. با پشتیبانی تماس بگیر.", _main_menu_kb())
        return
    pay["stage"] = "processing"
    await _send(chat_id, "✅ رسیدت دریافت شد — کانفیگت داره آماده می‌شه…")
    try:
        group = active_group() or {"name": "پیش‌فرض", "configs": []}
        res = await create_bundle(plan, group, owner=str(chat_id), name="")
    except Exception as e:
        logger.warning(f"Telegram auto-deliver failed: {e}")
        pay["stage"] = "waiting_receipt"
        await _send(chat_id, "❌ تحویل کانفیگ موفق نشد. دوباره عکس رسید رو بفرست یا با پشتیبانی تماس بگیر.")
        return
    pay["stage"] = "done"

    # ارسال به ادمین برای تایید
    plan_name = f"{plan.get('emoji', '')} {plan.get('name', '')}"
    main_uid = res.get("main_uid", "")

    # ذخیره در انتظار تایید
    if ADMIN_IDS:
        first_admin = next(iter(ADMIN_IDS))
        _pending_approvals[first_admin] = {
            "status": "approved",
            "plan_name": plan_name,
            "main_uid": main_uid,
            "user_chat_id": chat_id,
            "requested_by": first_admin,
        }
        host = get_host()
        sub_url = f"https://{host}/sub/{main_uid}"
        admin_msg = (
            f"🛒 <b>خرید جدید!</b>\n\n"
            f"📦 پلن: {plan_name}\n"
            f"💰 مبلغ: {_fa_price(plan.get('price'))}\n"
            f"👤 کاربر: <code>{chat_id}</code>\n"
            f"🔗 ساب: <code>{sub_url}</code>\n\n"
            f"بفرمایید لینک ساب کانفیگ رو برای کاربر ارسال کنید:"
        )
        await _send(first_admin, admin_msg)

    # پیام به کاربر
    await _send(chat_id, "⏳ رسید شما توسط ادمین تایید شد.\n\n🔗 لینک ساب شما:\n<code>در حال دریافت...</code>\n\nلطفاً منتظر ارسال لینک ساب باشید.")

async def _do_trial(chat_id: int):
    if str(chat_id) in TRIALS:
        await _send(chat_id, "🧪 تست رایگان رو قبلاً گرفتی — فقط یک بار.", _main_menu_kb())
        return
    try:
        res, st = await create_trial(str(chat_id), name="")
    except Exception as e:
        logger.warning(f"Telegram trial create failed: {e}")
        await _send(chat_id, "❌ ساخت کانفیگ تست موفق نشد. دوباره تلاش کن.", _main_menu_kb())
        return
    if st != "ok" or res is None:
        await _send(chat_id, "🧪 تست رایگان رو قبلاً گرفتی — فقط یک بار.", _main_menu_kb())
        return
    await _deliver_result(chat_id, res)

async def _deliver_result(chat_id: int, res: dict):
    try:
        chunks = _chunk_send(_deliver_text(res))
        for i, chunk in enumerate(chunks):
            kb = _main_menu_kb() if i == len(chunks) - 1 else None
            await _send(chat_id, chunk, kb)
    except Exception as e:
        logger.warning(f"Telegram deliver text send failed: {e}")
        host = get_host()
        sub_url = f"https://{host}/sub/{res.get('main_uid', '')}"
        await _send(chat_id, f"✅ کانفیگ ساخته شد — لینک ساب:\n<code>{sub_url}</code>", _main_menu_kb())

async def _handle_callback(cb: dict):
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    message_id = cb.get("message", {}).get("message_id")
    data = cb.get("data", "")
    cb_id = cb.get("id")

    if chat_id is None:
        return
    await _answer_cb(cb_id)

    # ثبت کلیک دکمه در «بات ویور»
    add_bot_log("telegram", chat_id, (cb.get("from") or {}).get("username") or "?", f"[دکمه] {data}")

    # ── منوی اصلی (عمومی) ──
    if data == "menu":
        _pending.pop(chat_id, None)
        if _is_admin(chat_id) and _is_logged_in(chat_id):
            await _edit(chat_id, message_id, "منوی NERULA:", _admin_menu_kb())
        else:
            await _edit(chat_id, message_id, "منوی NERULA:", _main_menu_kb())
        return

    # ── خرید (عمومی) ──
    if data == "shop":
        _pending.pop(chat_id, None)
        await _edit(chat_id, message_id, _shop_text(), _shop_kb())
        return

    if data.startswith("pay:"):
        pid = data.split(":", 1)[1]
        plan = PLANS.get(pid)
        if not plan:
            await _answer_cb(cb_id, "این پلن دیگه وجود نداره.")
            return
        if int(plan.get("price") or 0) <= 0:
            await _answer_cb(cb_id, "این پلن فقط از طریق تست رایگانه.")
            return
        await _edit(chat_id, message_id, _payment_text(plan), _payment_kb(pid))
        return

    if data.startswith("buy:paid:"):
        pid = data.split(":", 2)[2]
        plan = PLANS.get(pid)
        if not plan:
            await _answer_cb(cb_id, "این پلن دیگه وجود نداره.")
            return
        current = _payments.get(chat_id)
        if current and current.get("stage") in ("waiting_receipt", "processing"):
            await _answer_cb(cb_id, "⏳ یه پرداخت در جریانه. اول اون رو تموم کن.")
            return
        _payments[chat_id] = {"plan_id": pid, "stage": "waiting_receipt"}
        await _edit(chat_id, message_id, _receipt_prompt_text(plan), _receipt_cancel_kb())
        return

    if data == "receipt:cancel":
        _payments.pop(chat_id, None)
        await _edit(chat_id, message_id, "خرید لغو شد.", _main_menu_kb())
        return

    # ── تست رایگان (عمومی) ──
    if data == "trial":
        if str(chat_id) in TRIALS:
            await _answer_cb(cb_id, "🧪 تست رایگان رو قبلاً گرفتی — فقط یک بار.")
            await _edit(chat_id, message_id, "🧪 تست رایگان رو قبلاً گرفتی — فقط یک بار.", _main_menu_kb())
            return
        await _do_trial(chat_id)
        return

    # ── لیست کانفیگ‌ها (عمومی) ──
    if data.startswith("list:"):
        page = int(data.split(":", 1)[1] or 0)
        items = _owned_items(chat_id)
        if not items:
            await _edit(chat_id, message_id, "هنوز هیچ کانفیگی نداری. با «🧪 تست رایگان» یا «🛒 خرید کانفیگ» شروع کن.", _main_menu_kb())
            return
        n = len(items)
        start = page * PAGE_SIZE
        chunk = items[start:start + PAGE_SIZE]
        if not chunk:
            page = 0
            chunk = items[0:PAGE_SIZE]
        rows = []
        for uid, l in chunk:
            dot = "🟢" if is_link_allowed(l) else "🔴"
            rows.append([{"text": f"{dot} {l.get('label','?')[:28]}", "callback_data": f"view:{uid}"}])
        nav = []
        if page > 0:
            nav.append({"text": "◀ قبلی", "callback_data": f"list:{page-1}"})
        if page * PAGE_SIZE + PAGE_SIZE < n:
            nav.append({"text": "بعدی ▶", "callback_data": f"list:{page+1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": "⬅ منوی اصلی", "callback_data": "menu"}])
        await _edit(chat_id, message_id, f"📋 کانفیگ‌های تو ({n} مورد):", {"inline_keyboard": rows})
        return

    if data.startswith("view:"):
        uid = data.split(":", 1)[1]
        l = LINKS.get(uid)
        if not l or not _can_view(chat_id, uid):
            await _answer_cb(cb_id, "⛔ این کانفیگ مال تو نیست.")
            return
        kb = {"inline_keyboard": [
            [{"text": "🔗 نمایش لینک اتصال", "callback_data": f"link:{uid}"}],
            [{"text": "⬅ بازگشت به لیست", "callback_data": "list:0"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "menu"}],
        ]}
        await _edit(chat_id, message_id, _format_detail(uid, l), kb)
        return

    if data.startswith("link:"):
        uid = data.split(":", 1)[1]
        l = LINKS.get(uid)
        if not l or not _can_view(chat_id, uid):
            await _answer_cb(cb_id, "⛔ دسترسی نداری")
            return
        await _send(chat_id, _links_text(uid, l))
        return

    # ── از اینجا به بعد فقط ادمین‌های لاگین‌کرده ──
    if not (_is_admin(chat_id) and _is_logged_in(chat_id)):
        await _answer_cb(cb_id, "⛔ لطفاً اول با /login وارد شوید")
        return

    # ── تنظیمات ──
    if data == "settings":
        _pending.pop(chat_id, None)
        st_text = (
            "⚙️ <b>تنظیمات ربات</b>\n\n"
            f"🔑 رمز ورود: <code>{_admin_password()}</code>\n"
            f"👤 ادمین‌ها: {', '.join(str(i) for i in sorted(ADMIN_IDS))}\n"
            f"📢 چنل اطلاع‌رسانی: {CHANNEL_ID or 'تنظیم نشده'}\n"
            f"🔗 توکن: {'تنظیم شده ✅' if _bot_token() else 'تنظیم نشده ❌'}"
        )
        await _edit(chat_id, message_id, st_text, _settings_kb())
        return

    # ── تایید/رد خرید ──
    if data.startswith("approve:"):
        user_id = int(data.split(":", 1)[1])
        approval = _pending_approvals.get(user_id)
        if not approval:
            await _answer_cb(cb_id, "⏳ این درخواست دیگه معتبر نیست.")
            return
        approval["status"] = "approved"
        approval["requested_by"] = chat_id
        host = get_host()
        sub_url = f"https://{host}/sub/{approval['main_uid']}"
        await _edit(chat_id, message_id,
            f"✅ <b>تایید شد!</b>\n\n"
            f"بفرمایید لینک ساب کانفیگ رو برای کاربر ارسال کنید:\n\n"
            f"🔗 ساب: <code>{sub_url}</code>",
            None)
        await _send(chat_id, "📝 حالا لینک ساب یا کانفیگ رو بفرستید تا برای کاربر ارسال بشه:")
        return

    if data.startswith("reject:"):
        user_id = int(data.split(":", 1)[1])
        approval = _pending_approvals.get(user_id)
        if not approval:
            await _answer_cb(cb_id, "⏳ این درخواست دیگه معتبر نیست.")
            return
        user_chat_id = approval["user_chat_id"]
        await _send(user_chat_id, "❌ متأسفانه خرید شما توسط ادمین تایید نشد.\n\nبا پشتیبانی تماس بگیرید.")
        await _edit(chat_id, message_id, f"❌ خرید کاربر <code>{user_id}</code> رد شد.", None)
        _pending_approvals.pop(user_id, None)
        return

    # ── ساخت کانفیگ جدید ──
    if data == "newcfg":
        _pending[chat_id] = {"action": "wizard", "step": "label", "data": {}}
        await _edit(chat_id, message_id, _wizard_prompt("label", {}), _wizard_cancel_kb())
        return

    if data == "w:cancel":
        _pending.pop(chat_id, None)
        await _edit(chat_id, message_id, "ساخت کانفیگ لغو شد.", _admin_menu_kb())
        return

    if data.startswith("w:"):
        pending = _pending.get(chat_id)
        if not pending or pending.get("action") != "wizard":
            await _edit(chat_id, message_id, "این مرحله دیگه معتبر نیست، از منوی زیر دوباره شروع کن.", _admin_menu_kb())
            return

        step = pending["step"]
        wdata = pending["data"]

        if data.startswith("w:proto:") and step == "protocol":
            proto = data.split(":", 2)[2]
            wdata["protocol"] = proto if proto in PROTOCOLS else DEFAULT_PROTOCOL
            pending["step"] = "fingerprint"
            await _edit(chat_id, message_id, _wizard_prompt("fingerprint", wdata), _wizard_fp_kb())
            return

        if data.startswith("w:fp:") and step == "fingerprint":
            fp = data.split(":", 2)[2]
            wdata["fingerprint"] = fp if fp in FINGERPRINTS else DEFAULT_FINGERPRINT
            pending["step"] = "alpn"
            await _edit(chat_id, message_id, _wizard_prompt("alpn", wdata), _wizard_alpn_kb())
            return

        if data.startswith("w:alpnpreset:") and step == "alpn":
            code = data.split(":", 2)[2]
            wdata["alpn"] = ALPN_PRESET_MAP.get(code, "")
            pending["step"] = "port"
            await _edit(chat_id, message_id, _wizard_prompt("port", wdata), _wizard_skip_kb("port", f"⏭ پیش‌فرض ({DEFAULT_PORT})"))
            return

        if data == "w:skip:alpn" and step == "alpn":
            wdata["alpn"] = ""
            pending["step"] = "port"
            await _edit(chat_id, message_id, _wizard_prompt("port", wdata), _wizard_skip_kb("port", f"⏭ پیش‌فرض ({DEFAULT_PORT})"))
            return

        if data == "w:skip:port" and step == "port":
            wdata["port"] = DEFAULT_PORT
            pending["step"] = "volume"
            await _edit(chat_id, message_id, _wizard_prompt("volume", wdata), _wizard_unlimited_kb("volume"))
            return

        if data == "w:skip:volume" and step == "volume":
            wdata["limit_bytes"] = 0
            pending["step"] = "speed"
            await _edit(chat_id, message_id, _wizard_prompt("speed", wdata), _wizard_unlimited_kb("speed"))
            return

        if data == "w:skip:speed" and step == "speed":
            wdata["speed_limit_bytes"] = 0
            pending["step"] = "iplimit"
            await _edit(chat_id, message_id, _wizard_prompt("iplimit", wdata), _wizard_unlimited_kb("iplimit"))
            return

        if data == "w:skip:iplimit" and step == "iplimit":
            wdata["ip_limit"] = 0
            pending["step"] = "days"
            await _edit(chat_id, message_id, _wizard_prompt("days", wdata), _wizard_unlimited_kb("days"))
            return

        if data == "w:skip:days" and step == "days":
            wdata["expires_days"] = 0
            pending["step"] = "confirm"
            await _edit(chat_id, message_id, _wizard_summary(wdata), _wizard_confirm_kb())
            return

        if data == "w:confirm" and step == "confirm":
            expires_days = wdata.get("expires_days", 0)
            expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat() if expires_days > 0 else None
            uid, link = await make_link(
                label=wdata.get("label") or "کانفیگ جدید",
                limit_bytes=wdata.get("limit_bytes", 0),
                expires_at=expires_at,
                protocol=wdata.get("protocol", DEFAULT_PROTOCOL),
                fingerprint=wdata.get("fingerprint", DEFAULT_FINGERPRINT),
                alpn=wdata.get("alpn", ""),
                port=wdata.get("port", DEFAULT_PORT),
                ip_limit=wdata.get("ip_limit", 0),
                speed_limit_bytes=wdata.get("speed_limit_bytes", 0),
            )
            _pending.pop(chat_id, None)
            await _edit(chat_id, message_id, f"✅ کانفیگ ساخته شد.\n\n{_format_detail(uid, link)}", _link_detail_kb(uid, link["active"]))
            return

        await _answer_cb(cb_id, "این دکمه دیگه معتبر نیست.")
        return

    if data.startswith("toggle:"):
        uid = data.split(":", 1)[1]
        l = await set_link_active(uid, not LINKS.get(uid, {}).get("active", True))
        if not l:
            await _edit(chat_id, message_id, "این کانفیگ دیگه وجود نداره.", _admin_menu_kb())
            return
        await _edit(chat_id, message_id, _format_detail(uid, l), _link_detail_kb(uid, l["active"]))
        return

    if data.startswith("del:"):
        uid = data.split(":", 1)[1]
        l = LINKS.get(uid)
        if not l:
            await _edit(chat_id, message_id, "این کانفیگ دیگه وجود نداره.", _admin_menu_kb())
            return
        await _edit(chat_id, message_id, f"❗️ از حذف «{l.get('label')}» مطمئنی؟ این عمل برگشت‌ناپذیره.", _confirm_delete_kb(uid))
        return

    if data.startswith("delok:"):
        uid = data.split(":", 1)[1]
        label = await remove_link(uid)
        if label is None:
            await _edit(chat_id, message_id, "این کانفیگ قبلاً حذف شده بود.", _admin_menu_kb())
        else:
            await _edit(chat_id, message_id, f"🗑 کانفیگ «{label}» حذف شد.", _admin_menu_kb())
        return

# ── Polling loop ─────────────────────────────────────────────────────────────
async def _poll_loop():
    global _running
    offset = 0
    logger.info(f"🤖 Telegram bot polling started (admins: {len(ADMIN_IDS)})")
    while _running:
        try:
            res = await _call("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
            if not res or not res.get("ok"):
                await asyncio.sleep(3)
                continue
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        await _handle_message(upd["message"])
                    elif "callback_query" in upd:
                        await _handle_callback(upd["callback_query"])
                except Exception as e:
                    logger.warning(f"Telegram update handling error: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Telegram poll loop error: {e}")
            await asyncio.sleep(3)

# ── Lifecycle ────────────────────────────────────────────────────────────────
async def start_bot():
    global _client, _poll_task, _running
    _refresh_config()
    if not BOT_TOKEN:
        logger.info("Telegram bot: توکن تنظیم نشده، ربات غیرفعاله.")
        return
    if not ADMIN_IDS:
        logger.warning("Telegram bot: آیدی ادمین تنظیم نشده، هیچ‌کس اجازه‌ی مدیریت نداره.")
    _client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))
    _running = True
    _poll_task = asyncio.create_task(_poll_loop())

async def stop_bot():
    global _running, _client
    _running = False
    if _poll_task:
        _poll_task.cancel()
    if _client:
        await _client.aclose()
        _client = None

# ── مدیریت از تنظیمات پنل ─────────────────────────────────────────────────────
def get_status() -> dict:
    running = _client is not None
    return {
        "configured": bool(_bot_token()),
        "running": bool(running),
        "ready": bool(running),
        "bot_name": None,
        "admins": sorted(_admin_ids()),
        "admin_password": _admin_password(),
        "channel_id": _channel_id(),
        "last_error": None,
    }

async def apply_config(config: dict) -> dict:
    from main import save_state
    token = (config.get("token") or "").strip()
    if token == "••••••••":
        token = TELEGRAM_CONFIG.get("token", "")
    TELEGRAM_CONFIG["token"] = token
    TELEGRAM_CONFIG["admin_ids"] = (config.get("admin_ids") or "").strip()
    TELEGRAM_CONFIG["admin_password"] = (config.get("admin_password") or "nerula2024").strip()
    TELEGRAM_CONFIG["channel_id"] = (config.get("channel_id") or "").strip()
    await save_state()
    await stop_bot()
    if _bot_token():
        await start_bot()
    return {"ok": True}
