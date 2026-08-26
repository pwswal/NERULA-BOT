import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

import uvicorn
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NERULA")

app = FastAPI(title="NERULA", docs_url=None, redoc_url=None)

_base = os.environ.get("DATA_DIR", "")
if not _base:
    try:
        _base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(_base, exist_ok=True)
    except Exception:
        try:
            _base = os.path.join(os.getcwd(), "nerula_data")
            os.makedirs(_base, exist_ok=True)
        except Exception:
            _base = "/tmp/nerula_data"
            os.makedirs(_base, exist_ok=True)
DATA_DIR = Path(_base)
DATA_FILE = DATA_DIR / "state.json"
SECRET_FILE = DATA_DIR / "secret.key"
SESSION_COOKIE = "n_session"

def _get_secret():
    try:
        if SECRET_FILE.exists():
            s = SECRET_FILE.read_text().strip()
            if s:
                return s
        s = secrets.token_urlsafe(32)
        SECRET_FILE.write_text(s)
        return s
    except:
        return secrets.token_urlsafe(32)

SECRET_KEY = _get_secret()
VERSION = "1.0.0"
SALT = "nerula_vpn_2024"

AUTH = {"password_hash": None}
SESSIONS: dict = {}
sessions_lock = asyncio.Lock()

CONFIG = {
    "password_hash": None,
    "bot_token": "",
    "admin_id": "",
    "log_channel": "",
    "usage_channel": "",
    "railway_api_key": "",
}

def hash_pw(pw: str) -> str:
    return hashlib.sha256((SALT + pw).encode()).hexdigest()

def load_config():
    global CONFIG
    try:
        if DATA_FILE.exists():
            d = json.loads(DATA_FILE.read_text("utf-8"))
            CONFIG.update(d)
    except:
        pass
    if not CONFIG.get("password_hash"):
        CONFIG["password_hash"] = hash_pw("NERULA2024")
    save_config()

def save_config():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        logger.warning(f"Save error: {e}")

async def make_session():
    token = secrets.token_urlsafe(32)
    async with sessions_lock:
        SESSIONS[token] = time.time()
    return token

async def is_valid(token: str | None) -> bool:
    if not token:
        return False
    async with sessions_lock:
        return token in SESSIONS

async def check_auth(request: Request) -> bool:
    return await is_valid(request.cookies.get(SESSION_COOKIE))

@app.on_event("startup")
async def startup():
    load_config()
    try:
        import discord_bot as db
        db.discord_config = {
            "bot_token": CONFIG.get("bot_token", ""),
            "admin_id": CONFIG.get("admin_id", ""),
            "log_channel": CONFIG.get("log_channel", ""),
            "usage_channel": CONFIG.get("usage_channel", ""),
        }
        if db.discord_config.get("bot_token"):
            asyncio.create_task(db.start_bot())
    except Exception as e:
        logger.warning(f"Discord init: {e}")
    if CONFIG.get("railway_api_key"):
        asyncio.create_task(_usage_report_loop())
    logger.info(f"NERULA v{VERSION} started on port {os.environ.get('PORT', 8000)}")

@app.on_event("shutdown")
async def shutdown():
    try:
        import discord_bot as db
        await db.stop_bot()
    except:
        pass

@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}

@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await is_valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/dashboard")
    from pages import LOGIN_HTML
    return HTMLResponse(content=LOGIN_HTML)

@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    pw = body.get("password", "")
    if hash_pw(pw) == CONFIG.get("password_hash"):
        token = await make_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, max_age=86400)
        return resp
    return JSONResponse({"ok": False, "error": "Password wrong"}, status_code=401)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/login")
    from pages import DASHBOARD_HTML
    return HTMLResponse(content=DASHBOARD_HTML)

@app.get("/discord", response_class=HTMLResponse)
async def discord_page(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/login")
    from pages import DISCORD_HTML
    return HTMLResponse(content=DISCORD_HTML)

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/login")
    from pages import SETTINGS_HTML
    return HTMLResponse(content=SETTINGS_HTML)

@app.get("/api/config")
async def api_config(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"ok": False}, status_code=401)
    safe = {k: v for k, v in CONFIG.items() if k != "password_hash"}
    return {"ok": True, "config": safe}

@app.post("/api/config")
async def api_update_config(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"ok": False}, status_code=401)
    body = await request.json()
    for k in ("bot_token", "admin_id", "log_channel", "usage_channel", "railway_api_key"):
        if k in body and body[k]:
            CONFIG[k] = body[k].strip()
    save_config()
    return {"ok": True}

@app.post("/api/config/password")
async def api_change_password(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"ok": False}, status_code=401)
    body = await request.json()
    new_pw = body.get("new_password", "").strip()
    if len(new_pw) < 4:
        return JSONResponse({"ok": False, "error": "حداقل 4 کاراکتر"}, status_code=400)
    CONFIG["password_hash"] = hash_pw(new_pw)
    save_config()
    return {"ok": True}

@app.get("/api/discord/status")
async def discord_status(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        import discord_bot as db
        return {
            "ok": True,
            "running": db.is_ready(),
            "bot_id": db.get_bot_user_id(),
            "configured": bool(CONFIG.get("bot_token")),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/discord/start")
async def discord_start(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        import discord_bot as db
        if db.is_ready():
            return {"ok": True, "msg": "Already running"}
        db.discord_config = {
            "bot_token": CONFIG.get("bot_token", ""),
            "admin_id": CONFIG.get("admin_id", ""),
            "log_channel": CONFIG.get("log_channel", ""),
            "usage_channel": CONFIG.get("usage_channel", ""),
        }
        asyncio.create_task(db.start_bot())
        return {"ok": True, "msg": "Bot starting..."}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/discord/stop")
async def discord_stop(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        import discord_bot as db
        await db.stop_bot()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/discord/setup")
async def discord_setup(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        import discord_bot as db
        if not db.is_ready():
            return {"ok": False, "error": "Bot not running"}
        ch_id = CONFIG.get("log_channel") or CONFIG.get("channel_id")
        if not ch_id:
            return {"ok": False, "error": "Channel not set"}
        ch = db.get_channel(int(ch_id))
        if not ch:
            return {"ok": False, "error": "Channel not found"}
        await ch.send(embed=db.make_setup_embed(), view=db.make_buy_view())
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/discord/channels")
async def discord_channels(request: Request):
    if not await is_valid(request.cookies.get(SESSION_COOKIE)):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        import discord_bot as db
        if not db.is_ready():
            return {"ok": False, "channels": []}
        return {"ok": True, "channels": db.get_guild_channels()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/railway")
async def railway_webhook(request: Request):
    body = await request.json()
    logger.info(f"Railway webhook: {body.get('type', 'unknown')}")
    if body.get("type") == "DEPLOY":
        try:
            import discord_bot as db
            if not db.is_ready():
                return Response(status_code=204)
            log_ch_id = CONFIG.get("log_channel")
            if not log_ch_id:
                return Response(status_code=204)
            ch = db.get_channel(int(log_ch_id))
            if not ch:
                return Response(status_code=204)
            import discord
            proj = body.get("project", {})
            env = body.get("environment", {})
            svc = body.get("service", {})
            dep = body.get("deployment", {})
            status = body.get("status", "UNKNOWN")
            meta = dep.get("meta", {})
            color = discord.Color.red() if status == "CRASHED" else discord.Color.green()
            title = "💥 Deployment Crashed" if status == "CRASHED" else f"🚀 Deploy: {status}"
            embed = discord.Embed(title=title, color=color)
            embed.add_field(name="Project", value=proj.get("name", "?"), inline=True)
            embed.add_field(name="Environment", value=env.get("name", "?"), inline=True)
            embed.add_field(name="Service", value=svc.get("name", "?"), inline=True)
            if meta.get("commitAuthor"):
                embed.add_field(name="Author", value=meta["commitAuthor"], inline=True)
            if meta.get("commitMessage"):
                embed.add_field(name="Commit", value=meta["commitMessage"][:200], inline=False)
            embed.set_footer(text="NERULA Railway Monitor")
            embed.timestamp = datetime.utcnow()
            await ch.send(embed=embed)
            if status == "CRASHED":
                await _send_alert(f"💥 **Service crashed:** {svc.get('name', '?')} in {proj.get('name', '?')}")
        except Exception as e:
            logger.error(f"Webhook error: {e}")
    return Response(status_code=204)

USAGE_QUERY = """
query {
    usage(measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB]) {
        value
        measurement
        tags { projectId }
    }
    estimatedUsage(measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB]) {
        estimatedValue
        measurement
        projectId
    }
    me {
        projects {
            edges {
                node { id name }
            }
        }
    }
}
"""

async def _send_alert(text: str):
    try:
        import discord_bot as db
        if not db.is_ready():
            return
        ch_id = CONFIG.get("log_channel")
        if ch_id:
            ch = db.get_channel(int(ch_id))
            if ch:
                await ch.send(text)
    except:
        pass

async def _fetch_railway_usage() -> dict | None:
    api_key = CONFIG.get("railway_api_key", "")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://backboard.railway.app/graphql/v2",
                json={"query": USAGE_QUERY},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            return resp.json()
    except Exception as e:
        logger.error(f"Railway API error: {e}")
        return None

async def _usage_report_loop():
    while True:
        try:
            await asyncio.sleep(60)
            await _send_usage_report()
        except Exception as e:
            logger.error(f"Usage report error: {e}")
        await asyncio.sleep(3540)

async def _send_usage_report():
    data = await _fetch_railway_usage()
    if not data or "data" not in data:
        return
    d = data["data"]
    projects_edges = d.get("me", {}).get("projects", {}).get("edges", [])
    usage_list = d.get("usage", [])
    est_list = d.get("estimatedUsage", [])
    import discord_bot as db
    if not db.is_ready():
        return
    ch_id = CONFIG.get("usage_channel") or CONFIG.get("log_channel")
    if not ch_id:
        return
    ch = db.get_channel(int(ch_id))
    if not ch:
        return
    import discord
    embed = discord.Embed(title="📊 Railway Usage Report", color=discord.Color.blue())
    embed.timestamp = datetime.utcnow()
    for edge in projects_edges:
        node = edge.get("node", {})
        pid = node.get("id", "")
        pname = node.get("name", "Unknown")
        cpu = next((u["value"] for u in usage_list if u.get("measurement") == "CPU_USAGE" and u.get("tags", {}).get("projectId") == pid), 0)
        mem = next((u["value"] for u in usage_list if u.get("measurement") == "MEMORY_USAGE_GB" and u.get("tags", {}).get("projectId") == pid), 0)
        net = next((u["value"] for u in usage_list if u.get("measurement") == "NETWORK_TX_GB" and u.get("tags", {}).get("projectId") == pid), 0)
        est_cpu = next((u["estimatedValue"] for u in est_list if u.get("measurement") == "CPU_USAGE" and u.get("projectId") == pid), 0)
        est_mem = next((u["estimatedValue"] for u in est_list if u.get("measurement") == "MEMORY_USAGE_GB" and u.get("projectId") == pid), 0)
        est_net = next((u["estimatedValue"] for u in est_list if u.get("measurement") == "NETWORK_TX_GB" and u.get("projectId") == pid), 0)
        cost = cpu * 0.000463 + mem * 0.000231 + net * 0.1
        est_cost = est_cpu * 0.000463 + est_mem * 0.000231 + est_net * 0.1
        embed.add_field(
            name=pname,
            value=(
                f"**Cost:** ${cost:.4f} (Est: ${est_cost:.4f})\n"
                f"CPU: {cpu:.4f} vCores | Mem: {mem:.4f} GB | Net: {net:.4f} GB"
            ),
            inline=False,
        )
    embed.set_footer(text="NERULA Railway Monitor")
    await ch.send(embed=embed)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
