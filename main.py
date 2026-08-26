import sys as _sys
_sys.modules.setdefault('main', _sys.modules['__main__'])
import asyncio
import json
import os
import hashlib
import secrets
import time
import aiofiles
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from collections import deque, defaultdict
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import Response, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import httpx
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NERULA")

IRAN_TZ = ZoneInfo("Asia/Tehran")

app = FastAPI(title="NERULA", docs_url=None, redoc_url=None)

# ── Persistence ───────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_FILE = DATA_DIR / "nerula_state.json"
SECRET_FILE = DATA_DIR / "nerula_secret.key"
SAVE_LOCK = asyncio.Lock()

def _load_or_create_secret() -> str:
    """SECRET_KEY را روی دیسک ذخیره و ثابت نگه می‌دارد.
    قبلاً وقتی متغیر محیطی SECRET_KEY تنظیم نشده بود، با هر ری‌استارت سرویس
    (که روی Railway هر چند ساعت یک‌بار اتفاق می‌افتد) یک مقدار تصادفی جدید
    ساخته می‌شد. چون هش پسورد بر پایه‌ی همین secret ساخته می‌شود، تغییر آن
    باعث می‌شد پسورد درست هم دیگر قبول نشود. حالا secret یک‌بار ساخته و در
    فایل ذخیره می‌شود و در ری‌استارت‌های بعدی همان مقدار خوانده می‌شود."""
    env_secret = os.environ.get("SECRET_KEY")
    if env_secret:
        return env_secret
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if SECRET_FILE.exists():
            existing = SECRET_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        new_secret = secrets.token_urlsafe(32)
        SECRET_FILE.write_text(new_secret, encoding="utf-8")
        return new_secret
    except Exception as e:
        logger.warning(f"Could not persist SECRET_KEY, sessions/password may reset on restart: {e}")
        return secrets.token_urlsafe(32)

CONFIG = {
    "port": int(os.environ.get("PORT", 8000)),
    "secret": _load_or_create_secret(),
    "host": os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost"),
}

# تنظیمات ربات دیسکورد — از ستینگ پنل قابل تغییره و در state ذخیره می‌شه
DISCORD_CONFIG = {
    "token": os.environ.get("DISCORD_BOT_TOKEN", "").strip(),
    "admin_ids": os.environ.get("DISCORD_ADMIN_IDS", "").strip(),
    "admin_password": os.environ.get("DISCORD_ADMIN_PASSWORD", "nerula2024").strip(),
    "channel_id": os.environ.get("DISCORD_CHANNEL_ID", "").strip(),
}

# تنظیمات ربات تلگرام — از ستینگ پنل قابل تغییره و در state ذخیره می‌شه
TELEGRAM_CONFIG = {
    "token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
    "admin_ids": os.environ.get("TELEGRAM_ADMIN_IDS", "").strip(),
    "admin_password": os.environ.get("TELEGRAM_ADMIN_PASSWORD", "nerula2024").strip(),
    "channel_id": os.environ.get("TELEGRAM_CHANNEL_ID", "").strip(),
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def load_state():
    global LINKS, AUTH, SUBS, INBOUNDS, GROUPS, PLANS, TRIALS, DEFAULT_INBOUND_ID, bot_logs
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            async with aiofiles.open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
            data = json.loads(raw)
            LINKS.update(data.get("links", {}))
            SUBS.update(data.get("subs", {}))
            if "password_hash" in data:
                AUTH["password_hash"] = data["password_hash"]
            global WEBHOOK_URL
            WEBHOOK_URL = data.get("webhook_url", "")
            if isinstance(data.get("discord"), dict):
                DISCORD_CONFIG.update(data["discord"])
            if isinstance(data.get("telegram"), dict):
                TELEGRAM_CONFIG.update(data["telegram"])
            if isinstance(data.get("bot_logs"), list):
                bot_logs.clear()
                bot_logs.extend(data["bot_logs"])
            if isinstance(data.get("inbounds"), dict) and data["inbounds"]:
                INBOUNDS.update(data["inbounds"])
            if isinstance(data.get("groups"), dict) and data["groups"]:
                GROUPS.update(data["groups"])
            if isinstance(data.get("plans"), dict) and data["plans"]:
                PLANS.update(data["plans"])
            if isinstance(data.get("trials"), dict):
                TRIALS.update(data["trials"])
            DEFAULT_INBOUND_ID = str(data.get("default_inbound", "") or "")
            # لینک پیش‌فرضی که در نسخه‌های قبلی به‌صورت خودکار ساخته می‌شد دیگر
            # پشتیبانی نمی‌شود؛ اگر از قبل روی دیسک ذخیره شده باشد، حذفش می‌کنیم.
            legacy_default_uids = [uid for uid, l in LINKS.items() if l.get("is_default")]
            for uid in legacy_default_uids:
                LINKS.pop(uid, None)
            if legacy_default_uids:
                asyncio.create_task(save_state())
            # مهاجرت: در نسخه‌های قبلی bundle_main روی خودِ کانفیگ اصلی هم ست می‌شد؛
            # حالا فقط اعضای داخل ساب باید bundle_main داشته باشن تا پنل کانفیگ اصلی رو فیلتر نکنه.
            bundle_fixed = False
            for uid, l in LINKS.items():
                if l.get("bundle_main") == uid:
                    l.pop("bundle_main", None)
                    bundle_fixed = True
            if bundle_fixed:
                asyncio.create_task(save_state())
        if not INBOUNDS:
            INBOUNDS.update(_default_inbound_dict())
            if DEFAULT_INBOUND_ID not in INBOUNDS:
                DEFAULT_INBOUND_ID = "xhttp" if "xhttp" in INBOUNDS else next(iter(INBOUNDS))
        if not GROUPS:
            GROUPS.update(_default_groups_dict())
        if not PLANS:
            PLANS.update(_default_plans_dict())
        # مهاجرت نام وب‌سوکت به لاتین «WebSocket» در stateهای قدیمی
        migrated = False
        for v in INBOUNDS.values():
            if v.get("name") == "وب‌سوکت":
                v["name"] = "WebSocket"
                migrated = True
        for g in GROUPS.values():
            for c in (g.get("configs") or []):
                if c.get("name") == "وب‌سوکت":
                    c["name"] = "WebSocket"
                    migrated = True
        if migrated:
            asyncio.create_task(save_state())
        logger.info(f"State loaded: {len(LINKS)} links, {len(SUBS)} subs, {len(INBOUNDS)} inbounds, {len(GROUPS)} groups, {len(PLANS)} plans")
    except Exception as e:
        logger.warning(f"Could not load state: {e}")

async def save_state():
    async with SAVE_LOCK:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "links": dict(LINKS),
                "subs": dict(SUBS),
                "password_hash": AUTH["password_hash"],
                "webhook_url": WEBHOOK_URL,
                "discord": dict(DISCORD_CONFIG),
                "telegram": dict(TELEGRAM_CONFIG),
                "inbounds": dict(INBOUNDS),
                "default_inbound": DEFAULT_INBOUND_ID,
                "groups": dict(GROUPS),
                "plans": dict(PLANS),
                "trials": dict(TRIALS),
                "bot_logs": list(bot_logs)[:300],
                "saved_at": datetime.now().isoformat(),
            }
            tmp = DATA_FILE.with_suffix(".tmp")
            async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(DATA_FILE)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

# ── In-memory state ───────────────────────────────────────────────────────────
connections: dict = {}
stats = {
    "total_bytes": 0,
    "total_requests": 0,
    "total_errors": 0,
    "start_time": time.time(),
}
error_logs: deque = deque(maxlen=50)
activity_logs: deque = deque(maxlen=200)
bot_logs: deque = deque(maxlen=500)   # چت‌های ورودی ربات‌ها (برای «بات ویور»)
hourly_traffic: dict = defaultdict(int)
http_client: httpx.AsyncClient | None = None
LINKS: dict = {}
LINKS_LOCK = asyncio.Lock()
SUBS: dict = {}
SUBS_LOCK = asyncio.Lock()
WEBHOOK_URL: str = ""

# ── بات ویور: ثبت پیام‌های ورودی ربات تلگرام/دیسکورد ───────────────────────────
def add_bot_log(source: str, chat_id, who: str, text: str):
    try:
        bot_logs.appendleft({
            "source": source,
            "chat_id": str(chat_id),
            "who": (who or "")[:60],
            "text": (text or "")[:500],
            "time": datetime.now().isoformat(),
        })
    except Exception:
        pass

# ── اینباندها / گروه‌ها / پلن‌ها (مثل بقیه پنل‌ها) ──────────────────────────────
# INBOUNDS:  id -> {"name", "protocol", "port"}
# GROUPS:    id -> {"name", "is_default", "configs": [{"name", "icon", "inbound"}]}
# PLANS:     id -> {"name", "emoji", "limit_bytes", "speed_limit_bytes", "days"}
INBOUNDS: dict = {}
DEFAULT_INBOUND_ID: str = ""
GROUPS: dict = {}
PLANS: dict = {}
# TRIALS: owner (مثلاً آیدی دیسکورد) -> {"at": iso} — یک بار تست رایگان به هر کاربر
TRIALS: dict = {}
TRIAL_LIMIT = 100 * 1024 * 1024   # 100 مگابایت
TRIAL_DAYS = 1

def _default_inbound_dict() -> dict:
    return {
        "xhttp": {"name": "اصلی", "protocol": "xhttp", "port": 443},
        "ws": {"name": "WebSocket", "protocol": "vless-ws", "port": 443},
    }

def _default_groups_dict() -> dict:
    return {
        "g1": {
            "name": "پیش‌فرض",
            "is_default": True,
            "configs": [
                {"name": "NERULA", "icon": "🛍️", "inbound": "xhttp"},
                {"name": "WebSocket", "icon": "🌐", "inbound": "ws"},
                {"name": "XHTTP", "icon": "🚀", "inbound": "xhttp"},
            ],
        }
    }

def _default_plans_dict() -> dict:
    return {
        "bronze": {"name": "𝑩𝑹𝑶𝑵𝒁𝑬", "emoji": "🥉", "price": 100000, "limit_bytes": parse_size_to_bytes(10, "GB"), "speed_limit_bytes": parse_speed_to_bytes(100, "MBIT"), "days": 30},
        "silver": {"name": "𝑺𝑰𝑳𝑽𝑬𝑹", "emoji": "🥈", "price": 150000, "limit_bytes": parse_size_to_bytes(50, "GB"), "speed_limit_bytes": parse_speed_to_bytes(150, "MBIT"), "days": 30},
        "diamond": {"name": "𝑫𝑰𝑨𝑴𝑶𝑵𝑫", "emoji": "👑", "price": 250000, "limit_bytes": 0, "speed_limit_bytes": parse_speed_to_bytes(250, "MBIT"), "days": 30},
    }

def default_inbound() -> dict | None:
    if DEFAULT_INBOUND_ID in INBOUNDS:
        return INBOUNDS[DEFAULT_INBOUND_ID]
    for v in INBOUNDS.values():
        return v
    return None

def active_group() -> dict | None:
    for v in GROUPS.values():
        if v.get("is_default"):
            return v
    for v in GROUPS.values():
        return v
    return None

# پروتکل‌های پشتیبانی‌شده برای هر کانفیگ
PROTOCOLS = ("vless-ws", "xhttp")
DEFAULT_PROTOCOL = "vless-ws"

# Fingerprint (uTLS) های قابل انتخاب برای هر کانفیگ
FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized")
DEFAULT_FINGERPRINT = "chrome"

# پیش‌فرض ALPN بر اساس نوع ترابرد (اگر کاربر مقدار دستی نده)
DEFAULT_ALPN_BY_PROTOCOL = {
    "vless-ws": "http/1.1",
    "xhttp": "h2,http/1.1",
}
DEFAULT_PORT = 443
MIN_PORT, MAX_PORT = 1, 65535

# محدودیت سرعت (0 = نامحدود). واحد ذخیره‌سازی داخلی همیشه بایت‌بر‌ثانیه است.
DEFAULT_SPEED_LIMIT = 0

def log_activity(kind: str, message: str, level: str = "info"):
    """ثبت یک رخداد در لاگ فعالیت‌ها (ساخت/حذف/ویرایش کانفیگ، ورود، و...)."""
    activity_logs.append({
        "kind": kind,
        "level": level,
        "message": message,
        "time": datetime.now().isoformat(),
    })

# ── Auth ──────────────────────────────────────────────────────────────────────
SESSION_COOKIE = "nerula_session"
SESSION_TTL = 60 * 60 * 24 * 365

def hash_password(pw: str) -> str:
    return hashlib.sha256(f"{pw}{CONFIG['secret']}".encode()).hexdigest()

AUTH = {"password_hash": hash_password(os.environ.get("ADMIN_PASSWORD", "NERULA2024"))}
SESSIONS: dict = {}
SESSIONS_LOCK = asyncio.Lock()

async def create_session() -> str:
    token = secrets.token_urlsafe(32)
    async with SESSIONS_LOCK:
        SESSIONS[token] = time.time() + SESSION_TTL
    return token

async def is_valid_session(token: str | None) -> bool:
    if not token:
        return False
    async with SESSIONS_LOCK:
        exp = SESSIONS.get(token)
        if exp is None:
            return False
        if exp < time.time():
            SESSIONS.pop(token, None)
            return False
        return True

async def destroy_session(token: str | None):
    if not token:
        return
    async with SESSIONS_LOCK:
        SESSIONS.pop(token, None)

async def require_auth(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not await is_valid_session(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    return token

# ── Startup / Shutdown ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global http_client
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=100)
    timeout = httpx.Timeout(30.0, connect=10.0)
    http_client = httpx.AsyncClient(
        limits=limits, timeout=timeout, follow_redirects=True,
    )
    await load_state()
    await _tg_start_bot()
    await _dc_start_bot()
    log_activity("system", "سرور راه‌اندازی شد", "ok")
    logger.info(f"NERULA v9.8 started on port {CONFIG['port']}")

@app.on_event("shutdown")
async def shutdown():
    await save_state()
    await _tg_stop_bot()
    await _dc_stop_bot()
    if http_client:
        await http_client.aclose()

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_host(request: Request | None = None) -> str:
    """آدرس دامنه رو ترجیحاً از خودِ درخواست HTTP می‌گیره (هدر Host/X-Forwarded-Host)
    چون این همیشه دقیقاً همون دامنه‌ایه که کاربر واقعاً بهش وصل شده. متغیر محیطی
    RAILWAY_PUBLIC_DOMAIN فقط به‌عنوان fallback استفاده می‌شه، چون گاهی موقع بالا اومدن
    کانتینر هنوز مقداردهی نشده و باعث می‌شد لینک‌ها گاهی با "localhost" ساخته بشن."""
    if request is not None:
        h = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if h:
            h = h.split(":")[0]
            CONFIG["host"] = h  # کش آخرین دامنه‌ی واقعی دیده‌شده، برای جاهایی که request نداریم (مثل ربات تلگرام)
            return h
    return os.environ.get("RAILWAY_PUBLIC_DOMAIN", CONFIG["host"])

def generate_uuid() -> str:
    h = secrets.token_hex(16)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    
def now_ir() -> datetime:
    return datetime.now(IRAN_TZ)

def generate_vless_link(
    uuid: str,
    host: str,
    remark: str = "NERULA",
    protocol: str = DEFAULT_PROTOCOL,
    fingerprint: str | None = None,
    alpn: str | None = None,
    port: int | None = None,
) -> str:
    """می‌سازد VLESS share-link متناسب با پروتکل انتخاب‌شده (WS کلاسیک یا یکی از مدهای XHTTP).
    fingerprint / alpn / port در صورت ندادن، از پیش‌فرض‌های خود پروتکل استفاده می‌شوند."""
    fp = (fingerprint or DEFAULT_FINGERPRINT).strip() or DEFAULT_FINGERPRINT
    if fp not in FINGERPRINTS:
        fp = DEFAULT_FINGERPRINT
    alpn_val = (alpn or "").strip() or DEFAULT_ALPN_BY_PROTOCOL.get(protocol, "http/1.1")
    port_val = port or DEFAULT_PORT
    if not (MIN_PORT <= port_val <= MAX_PORT):
        port_val = DEFAULT_PORT

    if protocol == "vless-ws":
        path = f"/ws/{uuid}"
        params = {
            "encryption": "none",
            "security": "tls",
            "type": "ws",
            "host": host,
            "path": path,
            "sni": host,
            "fp": fp,
            "alpn": alpn_val,
        }
    else:
        # xhttp — مود auto: خود کلاینت بر اساس نوع اتصال (H2/REALITY یا نه)
        # بین packet-up و stream-up انتخاب می‌کنه؛ مسیر سرور به مود بستگی نداره.
        path = f"/xhttp-siz10/{uuid}"
        params = {
            "encryption": "none",
            "security": "tls",
            "type": "xhttp",
            "mode": "auto",
            "host": host,
            "path": path,
            "sni": host,
            "fp": fp,
            "alpn": alpn_val,
        }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"vless://{uuid}@{host}:{port_val}?{query}#{quote(remark)}"

def vless_link_for_link(link: dict, uid: str, host: str) -> str:
    """generate_vless_link رو با تنظیمات دستی همون کانفیگ (fingerprint/alpn/port) صدا می‌زنه."""
    proto = link.get("protocol", DEFAULT_PROTOCOL)
    return generate_vless_link(
        uid, host,
        remark=f"NERULA-{link.get('label','')}",
        protocol=proto,
        fingerprint=link.get("fingerprint"),
        alpn=link.get("alpn"),
        port=link.get("port"),
    )

def uptime() -> str:
    secs = int(time.time() - stats["start_time"])
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def parse_size_to_bytes(value: float, unit: str) -> int:
    unit = unit.upper()
    if unit == "GB": return int(value * 1024 ** 3)
    if unit == "MB": return int(value * 1024 ** 2)
    if unit == "KB": return int(value * 1024)
    return int(value)

def parse_speed_to_bytes(value: float, unit: str) -> int:
    """محدودیت سرعت رو به بایت‌بر‌ثانیه تبدیل می‌کنه.
    واحدهای پشتیبانی‌شده: MBIT (مگابیت‌بر‌ثانیه، رایج‌ترین)، KB (کیلوبایت‌بر‌ثانیه)، MB (مگابایت‌بر‌ثانیه)."""
    if value <= 0:
        return 0
    unit = (unit or "MBIT").upper()
    if unit == "MBIT":
        return int(value * 1024 * 1024 / 8)
    if unit == "KB":
        return int(value * 1024)
    if unit == "MB":
        return int(value * 1024 * 1024)
    return int(value)

def is_link_expired(link: dict) -> bool:
    exp = link.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.now() > datetime.fromisoformat(exp)
    except Exception:
        return False

def _limit_source(link: dict) -> dict:
    """برای کانفیگ‌های داخل یک bundle، کانفیگ اصلی رو برمی‌گردونه تا حجم کلی (مشترک)
    روی اون حساب بشه؛ مصرف تک‌تک اعضا روی سهم مشترک اعمال می‌شه."""
    bm = link.get("bundle_main")
    if bm and bm in LINKS:
        return LINKS[bm]
    return link

def is_link_allowed(link: dict | None) -> bool:
    if link is None:
        return False
    if not link.get("active", True):
        return False
    if is_link_expired(link):
        return False
    lb = link.get("limit_bytes", 0)
    if lb > 0 and link.get("used_bytes", 0) >= lb:
        return False
    src = _limit_source(link)
    if src is not link:
        slb = src.get("limit_bytes", 0)
        if slb > 0 and src.get("used_bytes", 0) >= slb:
            return False
    return True

def fmt_bytes(b: int) -> str:
    if b < 1024: return f"{b} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.2f} MB"
    return f"{b/1024**3:.2f} GB"

def unique_ips_for_uuid(uuid: str) -> set:
    """آی‌پی‌های یکتای همین لحظه متصل به یک UUID خاص (بر اساس dict اتصالات زنده)."""
    return {c.get("ip") for c in connections.values() if c.get("uuid") == uuid and c.get("ip")}

def is_ip_allowed(link: dict | None, uuid: str, ip: str) -> bool:
    """محدودیت تعداد آی‌پی/کاربر هم‌زمان برای هر کانفیگ. ip_limit=0 یعنی نامحدود.
    اگر همین آی‌پی از قبل روی این کانفیگ سشن باز داشته باشه، همیشه مجازه (برای چند اتصال
    هم‌زمان از یک دستگاه/مرورگر مشکلی پیش نمیاد)."""
    if link is None:
        return False
    limit = int(link.get("ip_limit", 0) or 0)
    if limit <= 0:
        return True
    ips = unique_ips_for_uuid(uuid)
    if ip in ips:
        return True
    return len(ips) < limit

def client_ip(request: Request) -> str:
    """آی‌پی واقعی کلاینت رو با احتساب هدرهای پراکسی (Railway/Cloudflare) برمی‌گردونه."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "نامشخص"

# ── Default link ──────────────────────────────────────────────────────────────

# ── Basic endpoints ───────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"service": "NERULA", "version": "1.0", "status": "active", "channel": "https://discord.gg/PJJavvtZ7U"}

@app.get("/health")
async def health():
    return {"status": "ok", "connections": len(connections), "uptime": uptime()}

# ── Subscription (single link / bundle) ───────────────────────────────────────
@app.get("/sub/{uuid}")
async def subscription_single(uuid: str, request: Request):
    import base64
    async with LINKS_LOCK:
        link = LINKS.get(uuid)
    if not link or not is_link_allowed(link):
        raise HTTPException(status_code=404, detail="not found or inactive")
    host = get_host(request)
    items = _bundle_items(uuid)
    lines = [vless_link_for_link(ml, mu, host) for mu, ml in items if is_link_allowed(ml)]
    if not lines:
        raise HTTPException(status_code=404, detail="not found or inactive")
    content = base64.b64encode("\n".join(lines).encode()).decode()
    return Response(content=content, media_type="text/plain",
                    headers={"profile-title": quote(link["label"]), "support-url": "https://discord.gg/PJJavvtZ7U"})

@app.get("/sub-all")
async def subscription_all(request: Request, _=Depends(require_auth)):
    import base64
    host = get_host(request)
    async with LINKS_LOCK:
        lines = [
            vless_link_for_link(d, uid, host)
            for uid, d in LINKS.items()
            if is_link_allowed(d)
        ]
    content = base64.b64encode("\n".join(lines).encode()).decode()
    return Response(content=content, media_type="text/plain")

# ── Auth endpoints ────────────────────────────────────────────────────────────
@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    ip = client_ip(request)
    if hash_password(str(body.get("password", ""))) != AUTH["password_hash"]:
        log_activity("auth", f"تلاش ورود ناموفق از {ip}", "err")
        raise HTTPException(status_code=401, detail="رمز عبور اشتباه است")
    token = await create_session()
    log_activity("auth", f"ورود موفق به پنل از {ip}", "ok")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/")
    return resp

@app.post("/api/logout")
async def api_logout(request: Request):
    await destroy_session(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

@app.get("/api/me")
async def api_me(request: Request):
    return {"authenticated": await is_valid_session(request.cookies.get(SESSION_COOKIE))}

@app.post("/api/change-password")
async def api_change_password(request: Request, token=Depends(require_auth)):
    body = await request.json()
    if hash_password(str(body.get("current_password", ""))) != AUTH["password_hash"]:
        raise HTTPException(status_code=400, detail="رمز فعلی اشتباه است")
    new = str(body.get("new_password", ""))
    if len(new) < 4:
        raise HTTPException(status_code=400, detail="رمز جدید باید حداقل ۴ کاراکتر باشد")
    AUTH["password_hash"] = hash_password(new)
    async with SESSIONS_LOCK:
        SESSIONS.clear()
        SESSIONS[token] = time.time() + SESSION_TTL
    await save_state()
    log_activity("auth", "رمز عبور پنل تغییر کرد", "ok")
    return {"ok": True}

# ── Stats ─────────────────────────────────────────────────────────────────────
@app.get("/stats")
async def get_stats(_=Depends(require_auth)):
    async with LINKS_LOCK:
        snap = dict(LINKS)
    return {
        "active_connections": len(connections),
        "total_traffic_mb": round(stats["total_bytes"] / (1024 ** 2), 2),
        "total_requests": stats["total_requests"],
        "total_errors": stats["total_errors"],
        "uptime": uptime(),
        "timestamp": datetime.now().isoformat(),
        "hourly": dict(hourly_traffic),
        "recent_errors": list(error_logs)[-10:],
        "links_count": len(snap),
        "active_links": sum(1 for l in snap.values() if is_link_allowed(l)),
        "expired_links": sum(1 for l in snap.values() if is_link_expired(l)),
    }

# ── Activity Logs ─────────────────────────────────────────────────────────────
@app.get("/api/activity")
async def get_activity(_=Depends(require_auth)):
    return {"logs": list(activity_logs)[-150:]}

# ── بات ویور: پیام‌های ورودی ربات تلگرام/دیسکورد ───────────────────────────────
@app.get("/api/botlog")
async def get_botlog(_=Depends(require_auth)):
    return {"logs": list(bot_logs)}

# ── Live connections (با دسته‌بندی بر اساس کانفیگ) ────────────────────────────
@app.get("/api/connections")
async def get_connections(_=Depends(require_auth)):
    """
    خروجی این endpoint حالا بر اساس کانفیگ (uuid) گروه‌بندی شده: هر کانفیگ
    یک آیتم با تعداد آی‌پی/سشن و مجموع ترافیکشه، و داخل هرکدوم لیست
    آی‌پی‌های متصل به همون کانفیگ (با جمع بایت و تعداد سشن هر آی‌پی) هست.
    raw_count همچنان تعداد واقعی اتصالات باز (سشن‌های خام) را برمی‌گرداند.
    """
    async with LINKS_LOCK:
        snap = dict(LINKS)

    by_uuid: dict[str, dict] = {}
    for conn_id, c in connections.items():
        uid = c.get("uuid", "نامشخص")
        ip = c.get("ip", "نامشخص")
        link = snap.get(uid)
        label = link.get("label") if link else "کانفیگ حذف‌شده"
        proto = link.get("protocol", DEFAULT_PROTOCOL) if link else "?"

        cfg = by_uuid.get(uid)
        if cfg is None:
            cfg = {
                "uuid": uid,
                "label": label,
                "protocol": proto,
                "sessions": 0,
                "bytes": 0,
                "ips": {},
                "first_connected_at": c.get("connected_at"),
                "last_connected_at": c.get("connected_at"),
            }
            by_uuid[uid] = cfg
        cfg["sessions"] += 1
        cfg["bytes"] += c.get("bytes", 0)

        ip_entry = cfg["ips"].get(ip)
        if ip_entry is None:
            ip_entry = {
                "ip": ip, "sessions": 0, "bytes": 0, "transports": set(),
                "first_connected_at": c.get("connected_at"),
                "last_connected_at": c.get("connected_at"),
            }
            cfg["ips"][ip] = ip_entry
        ip_entry["sessions"] += 1
        ip_entry["bytes"] += c.get("bytes", 0)
        ip_entry["transports"].add(c.get("transport", "vless-ws"))

        ca = c.get("connected_at")
        for entry in (cfg, ip_entry):
            if ca:
                if not entry["first_connected_at"] or ca < entry["first_connected_at"]:
                    entry["first_connected_at"] = ca
                if not entry["last_connected_at"] or ca > entry["last_connected_at"]:
                    entry["last_connected_at"] = ca

    configs = []
    for uid, cfg in by_uuid.items():
        ip_list = []
        for ip, e in cfg["ips"].items():
            ip_list.append({
                "ip": ip,
                "sessions": e["sessions"],
                "bytes": e["bytes"],
                "bytes_fmt": fmt_bytes(e["bytes"]),
                "transports": sorted(e["transports"]),
                "connected_at": e["first_connected_at"],
                "last_connected_at": e["last_connected_at"],
            })
        ip_list.sort(key=lambda x: x.get("last_connected_at") or "", reverse=True)
        configs.append({
            "uuid": uid,
            "label": cfg["label"],
            "protocol": cfg["protocol"],
            "ip_count": len(ip_list),
            "sessions": cfg["sessions"],
            "bytes": cfg["bytes"],
            "bytes_fmt": fmt_bytes(cfg["bytes"]),
            "connected_at": cfg["first_connected_at"],
            "last_connected_at": cfg["last_connected_at"],
            "connections": ip_list,
        })
    configs.sort(key=lambda x: x.get("last_connected_at") or "", reverse=True)

    return {
        "configs": configs,
        "count": len(configs),          # تعداد کانفیگ‌های دارای اتصال فعال
        "raw_count": len(connections),  # تعداد کل اتصالات باز (بدون گروه‌بندی)
    }

# ── Shared link create/delete helpers (استفاده مشترک API و ربات تلگرام) ───────
async def make_link(
    label: str = "لینک جدید",
    limit_bytes: int = 0,
    expires_at: str | None = None,
    note: str = "",
    protocol: str = DEFAULT_PROTOCOL,
    fingerprint: str = DEFAULT_FINGERPRINT,
    alpn: str = "",
    port: int = DEFAULT_PORT,
    ip_limit: int = 0,
    speed_limit_bytes: int = 0,
    owner: str = "",
    extra: dict | None = None,
) -> tuple[str, dict]:
    if protocol not in PROTOCOLS:
        protocol = DEFAULT_PROTOCOL
    fingerprint = (fingerprint or DEFAULT_FINGERPRINT).strip().lower()
    if fingerprint not in FINGERPRINTS:
        fingerprint = DEFAULT_FINGERPRINT
    if not (MIN_PORT <= port <= MAX_PORT):
        port = DEFAULT_PORT
    uid = generate_uuid()
    entry = {
        "label": (label or "لینک جدید").strip()[:60] or "لینک جدید",
        "limit_bytes": max(0, limit_bytes),
        "used_bytes": 0,
        "created_at": datetime.now().isoformat(),
        "active": True,
        "expires_at": expires_at,
        "note": (note or "").strip()[:200],
        "is_default": False,
        "protocol": protocol,
        "fingerprint": fingerprint,
        "alpn": (alpn or "").strip()[:100],
        "port": port,
        "ip_limit": max(0, ip_limit),
        "speed_limit_bytes": max(0, speed_limit_bytes),
        "owner": (owner or "").strip()[:100],
    }
    if extra:
        entry.update(extra)
    async with LINKS_LOCK:
        LINKS[uid] = entry
    asyncio.create_task(save_state())
    log_activity("link", f"کانفیگ «{LINKS[uid]['label']}» ساخته شد", "ok")
    asyncio.create_task(send_dc_notify(uid, LINKS[uid]))
    return uid, LINKS[uid]

# ── Bundle (گروه کانفیگ) ──────────────────────────────────────────────────────
def _bundle_items(uid: str) -> list:
    """اعضای یک bundle رو برمی‌گردونه؛ اگه کانفیگ sub_members داره همه رو، وگرنه خودش."""
    l = LINKS.get(uid)
    if not l:
        return []
    members = l.get("sub_members") or [uid]
    out = []
    for mu in members:
        ml = LINKS.get(mu)
        if ml:
            out.append((mu, ml))
    return out

def resolve_main(uid: str) -> dict | None:
    """برای کانفیگ‌های داخل یک bundle، کانفیگ اصلی (پاکت حجم مشترک) رو برمی‌گردونه.
    مصرف هر عضو روی کانفیگ اصلی حساب می‌شه تا حجم «کلی پلن» سهم مشترک باشه."""
    link = LINKS.get(uid)
    if link is None:
        return None
    for m in LINKS.values():
        members = m.get("sub_members")
        if members and uid in members:
            return m
    return link

async def create_bundle(plan: dict, group: dict, owner: str = "", name: str = "") -> dict:
    """طبق قالب گروه (۳ کانفیگ) و محدودیت‌های پلن، یک دسته کانفیگ می‌سازه.
    اولین کانفیگ به‌عنوان main شناخته می‌شه (حجم مشترک روی اونه) و لینک ساب ترکیبی
    از همه رو برمی‌گردونه. «name» اسم دلخواه کاربر برای کانفیگشه."""
    days = int(plan.get("days") or 0)
    expires_at = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
    limit_bytes = int(plan.get("limit_bytes") or 0)
    speed = int(plan.get("speed_limit_bytes") or 0)
    templates = group.get("configs") or []
    if not templates:
        templates = [{"name": "NERULA", "icon": "🛍️", "inbound": None}]
    plan_tag = f"{plan.get('emoji', '')} {plan.get('name', '')}".strip()
    uids: list[str] = []
    members = []
    for i, tpl in enumerate(templates):
        inb = INBOUNDS.get(tpl.get("inbound") or "") or {}
        if i == 0 and name:
            label = name.strip()[:60] or plan_tag or "کانفیگ جدید"
        else:
            label = " ".join([
                str(tpl.get("icon") or ""),
                str(tpl.get("name") or "کانفیگ"),
                plan_tag,
            ]).strip()[:60] or "کانفیگ جدید"
        uid, link = await make_link(
            label=label,
            limit_bytes=limit_bytes,
            expires_at=expires_at,
            protocol=inb.get("protocol") or DEFAULT_PROTOCOL,
            port=int(inb.get("port") or DEFAULT_PORT),
            speed_limit_bytes=speed,
            owner=owner,
            extra={"bundle_index": len(uids)},
        )
        uids.append(uid)
        members.append({"uid": uid, "link": link})
    main_uid = uids[0]
    async with LINKS_LOCK:
        LINKS[main_uid]["sub_members"] = list(uids)
        LINKS[main_uid]["bundle_label"] = plan_tag or "NERULA"
        for mu in uids:
            if mu != main_uid:
                LINKS[mu]["bundle_main"] = main_uid
    asyncio.create_task(save_state())
    return {"main_uid": main_uid, "uids": uids, "members": members, "plan": plan_tag}

async def create_trial(owner: str, name: str = "") -> tuple[dict | None, str]:
    """تست رایگان: به هر کاربر فقط یک بار، ۱۰۰ مگابایت با قالب گروه پیش‌فرض.
    برمی‌گردونه (res, ok) یا (None, 'used') اگه قبلاً تست گرفته."""
    if owner in TRIALS:
        return None, "used"
    plan = {
        "name": "TEST",
        "emoji": "🧪",
        "limit_bytes": TRIAL_LIMIT,
        "speed_limit_bytes": 0,
        "days": TRIAL_DAYS,
    }
    group = active_group() or {"name": "پیش‌فرض", "configs": []}
    res = await create_bundle(plan, group, owner=owner, name=name or "🧪 TEST")
    TRIALS[owner] = {"at": datetime.now().isoformat()}
    asyncio.create_task(save_state())
    return res, "ok"

async def send_dc_notify(uid: str, link: dict):
    if not WEBHOOK_URL:
        return
    label = link.get("label", "بدون نام")
    protocol = link.get("protocol", "vless-ws")
    proto = "VLESS / WS" if protocol == "vless-ws" else "XHTTP · auto"
    limit = link.get("limit_bytes", 0)
    limit_txt = "نامحدود" if limit == 0 else fmt_bytes(limit)
    ip_lim = link.get("ip_limit", 0)
    ip_txt = f"{ip_lim}" if ip_lim > 0 else "نامحدود"
    exp = link.get("expires_at")
    exp_txt = f"<t:{int(datetime.fromisoformat(exp).timestamp())}:R>" if exp else "بدون انقضا"
    speed = link.get("speed_limit_bytes", 0)
    speed_txt = "نامحدود" if speed == 0 else f"{speed // 1000000} Mbps"
    color = 0x22c55e
    embed = {
        "title": "✅ کانفیگ جدید ساخته شد",
        "color": color,
        "fields": [
            {"name": "نام", "value": label, "inline": True},
            {"name": "پروتکل", "value": proto, "inline": True},
            {"name": "سهمیه", "value": limit_txt, "inline": True},
            {"name": "محدودیت IP", "value": ip_txt, "inline": True},
            {"name": "سرعت", "value": speed_txt, "inline": True},
            {"name": "انقضا", "value": exp_txt, "inline": False},
        ],
        "footer": {"text": "NERULA v1.0 · Discord Webhook"},
    }
    await send_dc_webhook(embed)

async def remove_link(uid: str) -> str | None:
    async with LINKS_LOCK:
        if uid not in LINKS:
            return None
        label = LINKS[uid].get("label", uid)
        del LINKS[uid]
    asyncio.create_task(save_state())
    log_activity("link", f"کانفیگ «{label}» حذف شد", "err")
    return label

async def remove_bundle(uid: str) -> str | None:
    """کل یک bundle (کانفیگ اصلی + همه‌ی کانفیگ‌های داخلش) رو حذف می‌کنه.
    اگه با کانفیگ عضو (غیر اصلی) صدا زده بشه، کل باندلِ همون کانفیگ حذف می‌شه."""
    async with LINKS_LOCK:
        l = LINKS.get(uid)
        if l is None:
            return None
        main = LINKS.get(str(l.get("bundle_main") or "")) if l.get("bundle_main") else None
        target = main if main is not None else l
        label = target.get("label", uid)
        for mu in (target.get("sub_members") or [uid]):
            LINKS.pop(mu, None)
    asyncio.create_task(save_state())
    log_activity("link", f"باندل «{label}» و همه‌ی کانفیگ‌هاش حذف شد", "err")
    return label

async def set_link_active(uid: str, active: bool) -> dict | None:
    async with LINKS_LOCK:
        if uid not in LINKS:
            return None
        LINKS[uid]["active"] = bool(active)
        label = LINKS[uid]["label"]
    log_activity("link", f"کانفیگ «{label}» {'فعال' if active else 'غیرفعال'} شد", "ok" if active else "warn")
    asyncio.create_task(save_state())
    return LINKS[uid]

# ── Discord Webhook ────────────────────────────────────────────────────────────
async def send_dc_webhook(embed: dict):
    if not WEBHOOK_URL:
        return
    try:
        payload = {"embeds": [embed]}
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(WEBHOOK_URL, json=payload)
            if r.status_code >= 400:
                logger.warning(f"Discord webhook returned {r.status_code}")
    except Exception as e:
        logger.warning(f"Discord webhook failed: {e}")

@app.get("/api/webhook")
async def get_webhook(_=Depends(require_auth)):
    return {"url": WEBHOOK_URL}

@app.post("/api/webhook")
async def set_webhook(request: Request, _=Depends(require_auth)):
    global WEBHOOK_URL
    body = await request.json()
    url = (body.get("url") or "").strip()
    if url and not url.startswith("https://discord.com/api/webhooks/"):
        raise HTTPException(400, "لینک وبهوک معتبر نیست")
    WEBHOOK_URL = url
    asyncio.create_task(save_state())
    return {"url": WEBHOOK_URL}

@app.delete("/api/webhook")
async def delete_webhook(_=Depends(require_auth)):
    global WEBHOOK_URL
    WEBHOOK_URL = ""
    asyncio.create_task(save_state())
    return {"ok": True}

# ── ربات مدیریت دیسکورد (مدیریت از تنظیمات پنل) ─────────────────────────────────
@app.get("/api/discordbot")
async def get_discordbot(_=Depends(require_auth)):
    from discord_bot import get_status
    return get_status()

@app.post("/api/discordbot/config")
async def set_discordbot_config(request: Request, _=Depends(require_auth)):
    from discord_bot import apply_config
    body = await request.json()
    return await apply_config(body)

@app.get("/api/discordbot/channels")
async def get_discordbot_channels(_=Depends(require_auth)):
    from discord_bot import list_channels
    return await list_channels()

@app.post("/api/discordbot/send")
async def send_discordbot_panel(request: Request, _=Depends(require_auth)):
    from discord_bot import send_panel
    body = await request.json()
    return await send_panel(body.get("channel_id", ""))

# ── ربات تلگرام (مدیریت از تنظیمات پنل) ──────────────────────────────────────────
@app.get("/api/telegrambot")
async def get_telegrambot(_=Depends(require_auth)):
    from telegram_bot import get_status
    return get_status()

@app.post("/api/telegrambot/config")
async def set_telegrambot_config(request: Request, _=Depends(require_auth)):
    from telegram_bot import apply_config
    body = await request.json()
    return await apply_config(body)

# ── Inbounds / Groups / Plans ─────────────────────────────────────────────────
def _new_id(prefix: str, existing: dict) -> str:
    n = len(existing) + 1
    while f"{prefix}{n}" in existing:
        n += 1
    return f"{prefix}{n}"

@app.get("/api/inbounds")
async def get_inbounds(_=Depends(require_auth)):
    return {
        "inbounds": [{"id": iid, **v} for iid, v in INBOUNDS.items()],
        "default_inbound": DEFAULT_INBOUND_ID,
    }

@app.post("/api/inbounds")
async def create_inbound(request: Request, _=Depends(require_auth)):
    global DEFAULT_INBOUND_ID
    body = await request.json()
    name = (str(body.get("name") or "")).strip()[:40] or "اینباند جدید"
    proto = (str(body.get("protocol") or "")).strip()
    if proto not in PROTOCOLS:
        proto = DEFAULT_PROTOCOL
    try:
        port = int(body.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if not (MIN_PORT <= port <= MAX_PORT):
        port = DEFAULT_PORT
    iid = _new_id("inb", INBOUNDS)
    INBOUNDS[iid] = {"name": name, "protocol": proto, "port": port}
    if not DEFAULT_INBOUND_ID or DEFAULT_INBOUND_ID not in INBOUNDS:
        DEFAULT_INBOUND_ID = iid
    asyncio.create_task(save_state())
    return {"id": iid, **INBOUNDS[iid]}

@app.patch("/api/inbounds/{iid}")
async def update_inbound(iid: str, request: Request, _=Depends(require_auth)):
    if iid not in INBOUNDS:
        raise HTTPException(404, "inbound not found")
    body = await request.json()
    if "name" in body:
        INBOUNDS[iid]["name"] = str(body["name"]).strip()[:40] or INBOUNDS[iid]["name"]
    if "protocol" in body:
        p = str(body["protocol"]).strip()
        if p in PROTOCOLS:
            INBOUNDS[iid]["protocol"] = p
    if "port" in body:
        try:
            port = int(body["port"])
            if MIN_PORT <= port <= MAX_PORT:
                INBOUNDS[iid]["port"] = port
        except (TypeError, ValueError):
            pass
    asyncio.create_task(save_state())
    return {"id": iid, **INBOUNDS[iid]}

@app.delete("/api/inbounds/{iid}")
async def delete_inbound(iid: str, _=Depends(require_auth)):
    global DEFAULT_INBOUND_ID
    if iid not in INBOUNDS:
        raise HTTPException(404, "inbound not found")
    del INBOUNDS[iid]
    if DEFAULT_INBOUND_ID == iid:
        DEFAULT_INBOUND_ID = next(iter(INBOUNDS), "")
    asyncio.create_task(save_state())
    return {"ok": True}

@app.post("/api/inbounds/default")
async def set_default_inbound(request: Request, _=Depends(require_auth)):
    global DEFAULT_INBOUND_ID
    body = await request.json()
    iid = str(body.get("id") or "")
    if iid not in INBOUNDS:
        raise HTTPException(404, "inbound not found")
    DEFAULT_INBOUND_ID = iid
    asyncio.create_task(save_state())
    return {"default_inbound": DEFAULT_INBOUND_ID}

@app.get("/api/groups")
async def get_groups(_=Depends(require_auth)):
    def _def(gid: str):
        return GROUPS[gid].get("is_default", False)
    default_group = next((gid for gid in GROUPS if _def(gid)), next(iter(GROUPS), ""))
    return {
        "groups": [{"id": gid, **v} for gid, v in GROUPS.items()],
        "default_group": default_group,
    }

@app.post("/api/groups")
async def create_group(request: Request, _=Depends(require_auth)):
    body = await request.json()
    name = (str(body.get("name") or "")).strip()[:40] or "گروه جدید"
    raw_configs = body.get("configs") or []
    configs = []
    for c in raw_configs:
        if not isinstance(c, dict):
            continue
        configs.append({
            "name": str(c.get("name") or "کانفیگ")[:40],
            "icon": str(c.get("icon") or "")[:4],
            "inbound": str(c.get("inbound") or ""),
        })
    if not configs:
        inb = DEFAULT_INBOUND_ID if DEFAULT_INBOUND_ID in INBOUNDS else next(iter(INBOUNDS), "")
        configs = [{"name": "NERULA", "icon": "🛍️", "inbound": inb}]
    gid = _new_id("g", GROUPS)
    GROUPS[gid] = {"name": name, "is_default": False, "configs": configs}
    if not any(g.get("is_default") for g in GROUPS.values()):
        GROUPS[gid]["is_default"] = True
    asyncio.create_task(save_state())
    return {"id": gid, **GROUPS[gid]}

@app.patch("/api/groups/{gid}")
async def update_group(gid: str, request: Request, _=Depends(require_auth)):
    if gid not in GROUPS:
        raise HTTPException(404, "group not found")
    body = await request.json()
    if "name" in body:
        GROUPS[gid]["name"] = str(body["name"]).strip()[:40] or GROUPS[gid]["name"]
    if "configs" in body:
        configs = []
        for c in body["configs"]:
            if not isinstance(c, dict):
                continue
            configs.append({
                "name": str(c.get("name") or "کانفیگ")[:40],
                "icon": str(c.get("icon") or "")[:4],
                "inbound": str(c.get("inbound") or ""),
            })
        if configs:
            GROUPS[gid]["configs"] = configs
    if "is_default" in body and body["is_default"]:
        for g in GROUPS.values():
            g["is_default"] = False
        GROUPS[gid]["is_default"] = True
    asyncio.create_task(save_state())
    return {"id": gid, **GROUPS[gid]}

@app.delete("/api/groups/{gid}")
async def delete_group(gid: str, _=Depends(require_auth)):
    if gid not in GROUPS:
        raise HTTPException(404, "group not found")
    was_default = GROUPS[gid].get("is_default", False)
    del GROUPS[gid]
    if was_default and GROUPS:
        next(iter(GROUPS.values()))["is_default"] = True
    asyncio.create_task(save_state())
    return {"ok": True}

@app.post("/api/groups/default")
async def set_default_group(request: Request, _=Depends(require_auth)):
    body = await request.json()
    gid = str(body.get("id") or "")
    if gid not in GROUPS:
        raise HTTPException(404, "group not found")
    for g in GROUPS.values():
        g["is_default"] = False
    GROUPS[gid]["is_default"] = True
    asyncio.create_task(save_state())
    return {"ok": True}

@app.get("/api/plans")
async def get_plans(_=Depends(require_auth)):
    return {"plans": [{"id": pid, **v} for pid, v in PLANS.items()]}

@app.post("/api/plans")
async def create_plan(request: Request, _=Depends(require_auth)):
    body = await request.json()
    name = (str(body.get("name") or "")).strip()[:40] or "پلن جدید"
    emoji = (str(body.get("emoji") or "")).strip()[:4]
    try:
        lv = float(body.get("limit_value") or 0)
    except (TypeError, ValueError):
        lv = 0
    lu = (str(body.get("limit_unit") or "GB")).upper()
    limit_bytes = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
    try:
        sv = float(body.get("speed_value") or 0)
    except (TypeError, ValueError):
        sv = 0
    su = (str(body.get("speed_unit") or "MBIT")).upper()
    speed_limit_bytes = 0 if sv <= 0 else parse_speed_to_bytes(sv, su)
    try:
        days = int(body.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    try:
        price = int(float(body.get("price") or 0))
    except (TypeError, ValueError):
        price = 0
    pid = _new_id("p", PLANS)
    PLANS[pid] = {
        "name": name,
        "emoji": emoji,
        "price": max(0, price),
        "limit_bytes": limit_bytes,
        "speed_limit_bytes": speed_limit_bytes,
        "days": days,
    }
    asyncio.create_task(save_state())
    return {"id": pid, **PLANS[pid]}

@app.patch("/api/plans/{pid}")
async def update_plan(pid: str, request: Request, _=Depends(require_auth)):
    if pid not in PLANS:
        raise HTTPException(404, "plan not found")
    body = await request.json()
    p = PLANS[pid]
    if "name" in body:
        p["name"] = str(body["name"]).strip()[:40] or p["name"]
    if "emoji" in body:
        p["emoji"] = str(body["emoji"]).strip()[:4]
    if "limit_value" in body:
        try:
            lv = float(body.get("limit_value") or 0)
        except (TypeError, ValueError):
            lv = 0
        lu = (str(body.get("limit_unit") or "GB")).upper()
        p["limit_bytes"] = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
    if "speed_value" in body:
        try:
            sv = float(body.get("speed_value") or 0)
        except (TypeError, ValueError):
            sv = 0
        su = (str(body.get("speed_unit") or "MBIT")).upper()
        p["speed_limit_bytes"] = 0 if sv <= 0 else parse_speed_to_bytes(sv, su)
    if "days" in body:
        try:
            p["days"] = max(1, int(body["days"]))
        except (TypeError, ValueError):
            pass
    if "price" in body:
        try:
            p["price"] = max(0, int(float(body["price"])))
        except (TypeError, ValueError):
            pass
    asyncio.create_task(save_state())
    return {"id": pid, **p}

@app.delete("/api/plans/{pid}")
async def delete_plan(pid: str, _=Depends(require_auth)):
    if pid not in PLANS:
        raise HTTPException(404, "plan not found")
    del PLANS[pid]
    asyncio.create_task(save_state())
    return {"ok": True}

# ── Link Management ───────────────────────────────────────────────────────────
@app.post("/api/links")
async def create_link(request: Request, _=Depends(require_auth)):
    body = await request.json()
    lv = float(body.get("limit_value") or 0)
    lu = body.get("limit_unit") or "GB"
    limit_bytes = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
    exp_days = int(body.get("expires_days") or 0)
    expires_at = (datetime.now() + timedelta(days=exp_days)).isoformat() if exp_days > 0 else None
    try:
        port = int(body.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    try:
        ip_limit = int(body.get("ip_limit") or 0)
    except (TypeError, ValueError):
        ip_limit = 0

    sv = float(body.get("speed_limit_value") or 0)
    su = body.get("speed_limit_unit") or "MBIT"
    speed_limit_bytes = 0 if sv <= 0 else parse_speed_to_bytes(sv, su)

    uid, link = await make_link(
        label=body.get("label") or "لینک جدید",
        limit_bytes=limit_bytes,
        expires_at=expires_at,
        note=body.get("note") or "",
        protocol=body.get("protocol") or DEFAULT_PROTOCOL,
        fingerprint=body.get("fingerprint") or DEFAULT_FINGERPRINT,
        alpn=body.get("alpn") or "",
        port=port,
        ip_limit=ip_limit,
        speed_limit_bytes=speed_limit_bytes,
    )

    host = get_host(request)
    return {
        "uuid": uid,
        **link,
        "expired": False,
        "vless_link": vless_link_for_link(link, uid, host),
        "sub_url": f"https://{host}/p/{uid}",
        "raw_sub_url": f"https://{host}/sub/{uid}",
    }

@app.get("/api/links")
async def list_links(request: Request, _=Depends(require_auth)):
    host = get_host(request)
    async with LINKS_LOCK:
        snap = dict(LINKS)
    result = []
    for uid, d in snap.items():
        proto = d.get("protocol", DEFAULT_PROTOCOL)
        members = d.get("sub_members") or []
        result.append({
            "uuid": uid,
            **d,
            "protocol": proto,
            "expired": is_link_expired(d),
            "vless_link": vless_link_for_link(d, uid, host),
            "sub_url": f"https://{host}/p/{uid}",
            "raw_sub_url": f"https://{host}/sub/{uid}",
            "connected_ips": len(unique_ips_for_uuid(uid)),
            "bundle_members": [mu for mu in members if mu in snap],
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return {"links": result}

@app.patch("/api/links/{uid}")
async def update_link(uid: str, request: Request, _=Depends(require_auth)):
    body = await request.json()
    async with LINKS_LOCK:
        if uid not in LINKS:
            raise HTTPException(status_code=404, detail="link not found")
        link = LINKS[uid]
        label = link.get("label")
        if "active" in body:
            link["active"] = bool(body["active"])
            log_activity("link", f"کانفیگ «{label}» {'فعال' if link['active'] else 'غیرفعال'} شد", "ok" if link["active"] else "warn")
        if "label" in body:
            link["label"] = str(body["label"])[:60]
        if "note" in body:
            link["note"] = str(body["note"])[:200]
        if "reset_usage" in body and body["reset_usage"]:
            link["used_bytes"] = 0
            src = _limit_source(link)
            if src is not link:
                src["used_bytes"] = 0
            log_activity("link", f"مصرف کانفیگ «{label}» ریست شد", "info")
        if "limit_value" in body:
            lv = float(body.get("limit_value") or 0)
            lu = body.get("limit_unit") or "GB"
            link["limit_bytes"] = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
            src = _limit_source(link)
            if src is not link:
                src["limit_bytes"] = link["limit_bytes"]
        if "expires_days" in body:
            ed = int(body["expires_days"] or 0)
            link["expires_at"] = (datetime.now() + timedelta(days=ed)).isoformat() if ed > 0 else None
        if "fingerprint" in body:
            fp = str(body.get("fingerprint") or DEFAULT_FINGERPRINT).strip().lower()
            link["fingerprint"] = fp if fp in FINGERPRINTS else DEFAULT_FINGERPRINT
        if "alpn" in body:
            link["alpn"] = str(body.get("alpn") or "").strip()[:100]
        if "port" in body:
            try:
                p = int(body.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                p = DEFAULT_PORT
            link["port"] = p if (MIN_PORT <= p <= MAX_PORT) else DEFAULT_PORT
        if "ip_limit" in body:
            try:
                il = int(body.get("ip_limit") or 0)
            except (TypeError, ValueError):
                il = 0
            link["ip_limit"] = max(0, il)
        if "speed_limit_value" in body:
            sv = float(body.get("speed_limit_value") or 0)
            su = body.get("speed_limit_unit") or "MBIT"
            link["speed_limit_bytes"] = 0 if sv <= 0 else parse_speed_to_bytes(sv, su)
            from speed_limit import reset_bucket
            reset_bucket(uid)
        if any(k in body for k in ("label", "note", "limit_value", "expires_days", "fingerprint", "alpn", "port", "ip_limit", "speed_limit_value")):
            log_activity("link", f"کانفیگ «{link['label']}» ویرایش شد", "info")

    asyncio.create_task(save_state())
    return {"ok": True}

@app.delete("/api/links/{uid}")
async def delete_link(uid: str, _=Depends(require_auth)):
    label = await remove_bundle(uid)
    if label is None:
        raise HTTPException(status_code=404, detail="link not found")
    return {"ok": True, "deleted": uid}

# ══════════════════════════════════════════════════════════════════════════════
# VLESS Relay — جدا شده به relay_vless.py (دست نخورده)
# ══════════════════════════════════════════════════════════════════════════════

from relay_vless import (
    RELAY_BUF,
    parse_vless_header,
    check_and_use,
    relay_ws_to_tcp,
    relay_tcp_to_ws,
    websocket_tunnel,
)

app.add_api_websocket_route("/ws/{uuid}", websocket_tunnel)

# ══════════════════════════════════════════════════════════════════════════════
# XHTTP — Siz10a XHTTP Ultra (ترابرد جدید، جدا از VLESS/WS، هر ۳ مد)
# ══════════════════════════════════════════════════════════════════════════════
from xhttp_siz10 import router as xhttp_router
app.include_router(xhttp_router)

# ══════════════════════════════════════════════════════════════════════════════
# ربات مدیریت تلگرام (اختیاری — فقط اگه TELEGRAM_BOT_TOKEN ست شده باشه فعال می‌شه)
# ══════════════════════════════════════════════════════════════════════════════
async def _tg_start_bot():
    from telegram_bot import start_bot
    await start_bot()

async def _tg_stop_bot():
    from telegram_bot import stop_bot
    await stop_bot()

# ربات مدیریت دیسکورد (اختیاری — فقط اگه DISCORD_BOT_TOKEN ست شده باشه فعال می‌شه)
async def _dc_start_bot():
    from discord_bot import start_bot
    await start_bot()

async def _dc_stop_bot():
    from discord_bot import stop_bot
    await stop_bot()

# ── HTTP Proxy ────────────────────────────────────────────────────────────────
_HOP = {"connection","keep-alive","proxy-authenticate","proxy-authorization",
        "te","trailers","transfer-encoding","upgrade","content-encoding","content-length"}

@app.api_route("/proxy/{target_url:path}", methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
async def http_proxy(target_url: str, request: Request):
    if not target_url.startswith("http"):
        target_url = "https://" + target_url
    try:
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP and k.lower() != "host"}
        resp = await http_client.request(method=request.method, url=target_url, headers=headers, content=body)
        stats["total_bytes"] += len(resp.content)
        stats["total_requests"] += 1
        hourly_traffic[now_ir().strftime("%H:00")] += len(resp.content)
        return Response(content=resp.content, status_code=resp.status_code,
                        headers={k: v for k, v in resp.headers.items() if k.lower() not in _HOP})
    except Exception as exc:
        stats["total_errors"] += 1
        error_logs.append({"error": str(exc), "url": target_url, "time": datetime.now().isoformat()})
        raise HTTPException(status_code=502, detail=f"Proxy error: {exc}")

# ── Public sub page (یک صفحه‌ی زیبا و مستقل به‌ازای هر کانفیگ) ────────────────
@app.get("/p/{uuid_key}", response_class=HTMLResponse)
async def public_sub_page(uuid_key: str, request: Request):
    from pages import get_public_page_html
    async with LINKS_LOCK:
        exists = uuid_key in LINKS
    if not exists:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px'>کانفیگ پیدا نشد</h2>", status_code=404)
    return HTMLResponse(content=get_public_page_html(uuid_key))

@app.get("/api/public/sub/{uuid_key}")
async def public_sub_data(uuid_key: str, request: Request):
    async with LINKS_LOCK:
        link = LINKS.get(uuid_key)
    if not link:
        raise HTTPException(status_code=404, detail="not found")

    host = get_host(request)
    items = _bundle_items(uuid_key)
    links_out = []
    total_used = 0
    for mu, ml in items:
        allowed = is_link_allowed(ml)
        conn_count = sum(1 for c in connections.values() if c.get("uuid") == mu)
        proto = ml.get("protocol", DEFAULT_PROTOCOL)
        used = ml.get("used_bytes", 0)
        total_used += used
        links_out.append({
            "uuid": mu,
            "label": ml["label"],
            "active": allowed,
            "protocol": proto,
            "used_bytes": used,
            "used_fmt": fmt_bytes(used),
            "limit_bytes": ml.get("limit_bytes", 0),
            "limit_fmt": "∞" if ml.get("limit_bytes", 0) == 0 else fmt_bytes(ml["limit_bytes"]),
            "expires_at": ml.get("expires_at"),
            "vless_link": vless_link_for_link(ml, mu, host),
            "sub_url": f"https://{host}/sub/{mu}",
            "connections": conn_count,
            "ip_limit": ml.get("ip_limit", 0),
            "speed_limit_bytes": ml.get("speed_limit_bytes", 0),
        })

    return {
        "locked": False,
        "name": link["label"],
        "desc": link.get("note", ""),
        "sub_url": f"https://{host}/p/{uuid_key}",
        "active_connections": sum(x["connections"] for x in links_out),
        "total_used_fmt": fmt_bytes(total_used),
        "links": links_out,
    }

# ── HTML Pages (login + dashboard) ───────────────────────────────────────────
from pages import LOGIN_HTML, DASHBOARD_HTML

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/dashboard")
    return HTMLResponse(content=LOGIN_HTML)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not await is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/login")
    return HTMLResponse(content=DASHBOARD_HTML)

@app.get("/test-ws", response_class=HTMLResponse)
async def test_ws_redirect():
    return HTMLResponse(content="<script>location.href='/dashboard'</script>")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=CONFIG["port"], log_level="info", workers=1)
