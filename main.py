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
from fastapi.responses import HTMLResponse, JSONResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NERULA")

app = FastAPI(title="NERULA", docs_url=None, redoc_url=None)

DATA_DIR = os.path.join(os.getcwd(), "nerula_data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "state.json")
SECRET_KEY = secrets.token_urlsafe(32)
SALT = "nerula_vpn_2024"
SESSION_COOKIE = "n_session"

CONFIG = {
    "password_hash": None,
    "bot_token": "",
    "admin_id": "",
    "log_channel": "",
    "usage_channel": "",
    "railway_api_key": "",
}

SESSIONS: dict = {}
sessions_lock = asyncio.Lock()

def hash_pw(pw: str) -> str:
    return hashlib.sha256((SALT + pw).encode()).hexdigest()

def load_config():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            CONFIG.update(d)
    except:
        pass
    if not CONFIG.get("password_hash"):
        CONFIG["password_hash"] = hash_pw("NERULA2024")
    save_config()

def save_config():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Save error: {e}")

def verify_sig(data: bytes, sig: str, key: str) -> bool:
    import hmac, hashlib
    digest = hmac.new(key.encode(), data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sig)

async def make_session():
    token = secrets.token_urlsafe(32)
    async with sessions_lock:
        SESSIONS[token] = time.time()
    return token

async def is_valid(token):
    if not token:
        return False
    async with sessions_lock:
        return token in SESSIONS

async def check_auth(request):
    return await is_valid(request.cookies.get(SESSION_COOKIE))

# ══════════════════════════════════════════════════════════════
# HTML PAGES
# ══════════════════════════════════════════════════════════════

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NERULA</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f23;color:#e0e0e0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.login-box{background:#1a1a2e;padding:2rem;border-radius:12px;width:100%;max-width:360px;border:1px solid #16213e}
.login-box h1{text-align:center;color:#00d4ff;margin-bottom:1.5rem;font-size:1.5rem}
.login-box input{width:100%;padding:0.75rem;border:1px solid #16213e;border-radius:8px;background:#0f0f23;color:#e0e0e0;font-size:1rem;margin-bottom:1rem}
.login-box button{width:100%;padding:0.75rem;border:none;border-radius:8px;background:#00d4ff;color:#0f0f23;font-weight:bold;font-size:1rem;cursor:pointer}
.login-box button:hover{background:#00b8d9}
.error{color:#ff6b6b;font-size:0.875rem;text-align:center;margin-top:0.5rem;display:none}
</style>
</head>
<body>
<div class="login-box">
<h1>⚡ NERULA</h1>
<input type="password" id="pw" placeholder="Password" onkeypress="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">Login</button>
<div class="error" id="err">Wrong password</div>
</div>
<script>
async function doLogin(){
const pw=document.getElementById('pw').value;
if(!pw)return;
try{
const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
const j=await r.json();
if(j.ok)location.href='/dashboard';
else{document.getElementById('err').style.display='block';}
}catch(e){document.getElementById('err').style.display='block';}
}
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NERULA - Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f23;color:#e0e0e0;min-height:100vh}
.nav{background:#1a1a2e;padding:0.75rem 1.5rem;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #16213e}
.nav h1{color:#00d4ff;font-size:1.1rem}
.nav-links a{color:#a0a0c0;text-decoration:none;margin-left:1rem;font-size:0.875rem}
.nav-links a:hover{color:#00d4ff}
.nav-links a.active{color:#00d4ff;font-weight:bold}
.container{max-width:900px;margin:2rem auto;padding:0 1rem}
.card{background:#1a1a2e;border:1px solid #16213e;border-radius:12px;padding:1.5rem;margin-bottom:1rem}
.card h2{color:#00d4ff;font-size:1rem;margin-bottom:1rem}
.status-row{display:flex;justify-content:space-between;padding:0.5rem 0;border-bottom:1px solid #16213e22}
.status-row:last-child{border:none}
.label{color:#808090}
.value{font-weight:500}
.online{color:#4ade80}
.offline{color:#ff6b6b}
.activity-list{max-height:300px;overflow-y:auto}
.activity-item{padding:0.5rem 0;border-bottom:1px solid #16213e22;font-size:0.875rem}
.activity-item .time{color:#808090;font-size:0.75rem}
</style>
</head>
<body>
<div class="nav">
<h1>⚡ NERULA</h1>
<div class="nav-links">
<a href="/dashboard" class="active">Dashboard</a>
<a href="/discord">Discord</a>
<a href="/settings">Settings</a>
</div>
</div>
<div class="container">
<div class="card">
<h2>Bot Status</h2>
<div class="status-row"><span class="label">Status</span><span class="value" id="status">Loading...</span></div>
<div class="status-row"><span class="label">Guilds</span><span class="value" id="guilds">-</span></div>
<div class="status-row"><span class="label">Users</span><span class="value" id="users">-</span></div>
</div>
<div class="card">
<h2>Recent Activity</h2>
<div class="activity-list" id="activity"><p style="color:#808090">No activity yet</p></div>
</div>
</div>
<script>
async function load(){
try{
const r=await fetch('/api/discord/status');
const j=await r.json();
const s=document.getElementById('status');
if(j.running){s.textContent='Online';s.className='value online';}
else{s.textContent='Offline';s.className='value offline';}
document.getElementById('guilds').textContent=j.guilds||'-';
document.getElementById('users').textContent=j.users||'-';
if(j.activity){
const el=document.getElementById('activity');
el.innerHTML='';
j.activity.slice(-20).reverse().forEach(a=>{
const d=document.createElement('div');
d.className='activity-item';
d.innerHTML=`<div class="time">${a.time}</div><div>${a.msg}</div>`;
el.appendChild(d);
});
}
}catch(e){}
}
load();
setInterval(load,10000);
</script>
</body>
</html>"""

DISCORD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NERULA - Discord</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f23;color:#e0e0e0;min-height:100vh}
.nav{background:#1a1a2e;padding:0.75rem 1.5rem;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #16213e}
.nav h1{color:#00d4ff;font-size:1.1rem}
.nav-links a{color:#a0a0c0;text-decoration:none;margin-left:1rem;font-size:0.875rem}
.nav-links a:hover{color:#00d4ff}
.nav-links a.active{color:#00d4ff;font-weight:bold}
.container{max-width:900px;margin:2rem auto;padding:0 1rem}
.card{background:#1a1a2e;border:1px solid #16213e;border-radius:12px;padding:1.5rem;margin-bottom:1rem}
.card h2{color:#00d4ff;font-size:1rem;margin-bottom:1rem}
.btn{padding:0.75rem 1.5rem;border:none;border-radius:8px;font-weight:bold;cursor:pointer;font-size:0.875rem;margin-right:0.5rem}
.btn-green{background:#4ade80;color:#0f0f23}
.btn-red{background:#ff6b6b;color:#fff}
.btn:hover{opacity:0.9}
.btn:disabled{opacity:0.5;cursor:not-allowed}
.channel-select{width:100%;padding:0.75rem;border:1px solid #16213e;border-radius:8px;background:#0f0f23;color:#e0e0e0;font-size:0.875rem;margin-top:0.5rem}
.msg{margin-top:0.5rem;font-size:0.875rem;color:#4ade80;display:none}
.err{margin-top:0.5rem;font-size:0.875rem;color:#ff6b6b;display:none}
</style>
</head>
<body>
<div class="nav">
<h1>⚡ NERULA</h1>
<div class="nav-links">
<a href="/dashboard">Dashboard</a>
<a href="/discord" class="active">Discord</a>
<a href="/settings">Settings</a>
</div>
</div>
<div class="container">
<div class="card">
<h2>Discord Bot</h2>
<p style="margin-bottom:1rem">Start or stop the Discord bot.</p>
<button class="btn btn-green" id="startBtn" onclick="startBot()">Start</button>
<button class="btn btn-red" id="stopBtn" onclick="stopBot()" disabled>Stop</button>
<div class="msg" id="msg"></div>
<div class="err" id="err"></div>
</div>
<div class="card">
<h2>Setup Channel</h2>
<p style="margin-bottom:0.5rem">Select channel for /setup command:</p>
<select class="channel-select" id="channelSelect" onchange="setChannel()">
<option value="">Select channel...</option>
</select>
</div>
</div>
<script>
async function loadStatus(){
try{
const r=await fetch('/api/discord/status');
const j=await r.json();
document.getElementById('startBtn').disabled=j.running;
document.getElementById('stopBtn').disabled=!j.running;
if(j.channels&&j.channels.length){
const sel=document.getElementById('channelSelect');
j.channels.forEach(c=>{
const o=document.createElement('option');
o.value=c.id;o.textContent='#'+c.name;
if(c.selected)o.selected=true;
sel.appendChild(o);
});
}
}catch(e){}
}
async function startBot(){
show('Starting...','');
try{
const r=await fetch('/api/discord/start',{method:'POST'});
const j=await r.json();
if(j.ok)show('Bot started!','');
else show('',j.error||'Failed');
setTimeout(loadStatus,3000);
}catch(e){show('','Network error');}
}
async function stopBot(){
try{
const r=await fetch('/api/discord/stop',{method:'POST'});
const j=await r.json();
show('Bot stopped','');
setTimeout(loadStatus,2000);
}catch(e){show('','Network error');}
}
async function setChannel(){
const ch=document.getElementById('channelSelect').value;
try{await fetch('/api/discord/channel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel:ch})});}catch(e){}
}
function show(m,e){
const ms=document.getElementById('msg'),er=document.getElementById('err');
ms.textContent=m;er.textContent=e;
ms.style.display=m?'block':'none';
er.style.display=e?'block':'none';
}
loadStatus();
</script>
</body>
</html>"""

SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NERULA - Settings</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f23;color:#e0e0e0;min-height:100vh}
.nav{background:#1a1a2e;padding:0.75rem 1.5rem;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #16213e}
.nav h1{color:#00d4ff;font-size:1.1rem}
.nav-links a{color:#a0a0c0;text-decoration:none;margin-left:1rem;font-size:0.875rem}
.nav-links a:hover{color:#00d4ff}
.nav-links a.active{color:#00d4ff;font-weight:bold}
.container{max-width:900px;margin:2rem auto;padding:0 1rem}
.card{background:#1a1a2e;border:1px solid #16213e;border-radius:12px;padding:1.5rem;margin-bottom:1rem}
.card h2{color:#00d4ff;font-size:1rem;margin-bottom:1rem}
.field{margin-bottom:1rem}
.field label{display:block;color:#808090;font-size:0.875rem;margin-bottom:0.25rem}
.field input{width:100%;padding:0.75rem;border:1px solid #16213e;border-radius:8px;background:#0f0f23;color:#e0e0e0;font-size:0.875rem}
.field input:focus{outline:none;border-color:#00d4ff}
.btn{padding:0.75rem 1.5rem;border:none;border-radius:8px;background:#00d4ff;color:#0f0f23;font-weight:bold;cursor:pointer;font-size:0.875rem}
.btn:hover{background:#00b8d9}
.msg{margin-top:0.5rem;font-size:0.875rem;color:#4ade80;display:none}
.err{margin-top:0.5rem;font-size:0.875rem;color:#ff6b6b;display:none}
</style>
</head>
<body>
<div class="nav">
<h1>⚡ NERULA</h1>
<div class="nav-links">
<a href="/dashboard">Dashboard</a>
<a href="/discord">Discord</a>
<a href="/settings" class="active">Settings</a>
</div>
</div>
<div class="container">
<div class="card">
<h2>Bot Configuration</h2>
<div class="field"><label>Bot Token</label><input type="password" id="bot_token" placeholder="Discord bot token"></div>
<div class="field"><label>Admin User ID</label><input type="text" id="admin_id" placeholder="Your Discord user ID"></div>
<div class="field"><label>Log Channel ID</label><input type="text" id="log_channel" placeholder="Channel for purchase logs"></div>
<div class="field"><label>Usage Channel ID</label><input type="text" id="usage_channel" placeholder="Channel for usage reports"></div>
<div class="field"><label>Railway API Key</label><input type="password" id="railway_api_key" placeholder="Railway API token (optional)"></div>
<button class="btn" onclick="save()">Save</button>
<div class="msg" id="msg">Saved!</div>
<div class="err" id="err"></div>
</div>
<div class="card">
<h2>Change Password</h2>
<div class="field"><label>Current Password</label><input type="password" id="old_pw" placeholder="Current password"></div>
<div class="field"><label>New Password</label><input type="password" id="new_pw" placeholder="New password"></div>
<button class="btn" onclick="changePw()">Change Password</button>
<div class="msg" id="msg2">Password changed!</div>
<div class="err" id="err2"></div>
</div>
</div>
<script>
async function load(){
try{
const r=await fetch('/api/config');
const j=await r.json();
if(j.ok){
const c=j.config;
document.getElementById('bot_token').value=c.bot_token||'';
document.getElementById('admin_id').value=c.admin_id||'';
document.getElementById('log_channel').value=c.log_channel||'';
document.getElementById('usage_channel').value=c.usage_channel||'';
document.getElementById('railway_api_key').value=c.railway_api_key||'';
}
}catch(e){}
}
async function save(){
const data={
bot_token:document.getElementById('bot_token').value,
admin_id:document.getElementById('admin_id').value,
log_channel:document.getElementById('log_channel').value,
usage_channel:document.getElementById('usage_channel').value,
railway_api_key:document.getElementById('railway_api_key').value
};
try{
const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
const j=await r.json();
if(j.ok){show('msg','err','Saved!','');}
else{show('msg','err','',j.error||'Failed');}
}catch(e){show('msg','err','',e.message);}
}
async function changePw(){
const data={old_password:document.getElementById('old_pw').value,new_password:document.getElementById('new_pw').value};
try{
const r=await fetch('/api/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
const j=await r.json();
if(j.ok){show('msg2','err2','Password changed!','');document.getElementById('old_pw').value='';document.getElementById('new_pw').value='';}
else{show('msg2','err2','',j.error||'Failed');}
}catch(e){show('msg2','err2','',e.message);}
}
function show(okId,errId,okMsg,errMsg){
document.getElementById(okId).textContent=okMsg;document.getElementById(okId).style.display=okMsg?'block':'none';
document.getElementById(errId).textContent=errMsg;document.getElementById(errId).style.display=errMsg?'block':'none';
}
load();
</script>
</body>
</html>"""

RECEIPT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NERULA - Receipt</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f23;color:#e0e0e0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.receipt{background:#1a1a2e;border:1px solid #16213e;border-radius:12px;padding:2rem;width:100%;max-width:400px;text-align:center}
.receipt h1{color:#00d4ff;margin-bottom:0.5rem;font-size:1.5rem}
.receipt p{color:#808090;margin-bottom:1rem}
.receipt .amount{font-size:2rem;color:#4ade80;font-weight:bold;margin:1rem 0}
.receipt .plan{font-size:1.1rem;color:#e0e0e0;margin-bottom:0.5rem}
.receipt .details{font-size:0.875rem;color:#808090;margin-top:1rem;text-align:left;border-top:1px solid #16213e;padding-top:1rem}
.receipt .details div{margin-bottom:0.5rem}
.receipt .details span{color:#a0a0c0}
</style>
</head>
<body>
<div class="receipt">
<h1>⚡ NERULA</h1>
<p>Payment Receipt</p>
<div class="amount" id="amount">-</div>
<div class="plan" id="plan">-</div>
<div class="details">
<div><span>Order:</span> <span id="order">-</span></div>
<div><span>Date:</span> <span id="date">-</span></div>
<div><span>Status:</span> <span id="status" style="color:#4ade80">Pending</span></div>
</div>
</div>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════
# DISCORD BOT (inline)
# ══════════════════════════════════════════════════════════════

import discord
from discord import app_commands

_discord_client = None
_discord_task = None
_discord_ready = asyncio.Event()
_activity: list = []
_receipts: dict = {}
_pending_buys: dict = {}

async def _discord_log(msg: str):
    _activity.append({"time": datetime.now().strftime("%H:%M"), "msg": msg})
    if len(_activity) > 100:
        _activity.pop(0)
    if not CONFIG.get("log_channel"):
        return
    try:
        ch = _discord_client.get_channel(int(CONFIG["log_channel"]))
        if ch:
            await ch.send(msg)
    except:
        pass

def _build_receipt(user_id, plan, amount):
    order_id = f"V{int(time.time())}"
    _receipts[order_id] = {
        "user_id": str(user_id),
        "plan": plan,
        "amount": amount,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "pending",
    }
    return order_id

def _get_discord_client():
    global _discord_client
    if _discord_client is not None:
        return _discord_client

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    _discord_client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(_discord_client)

    @_discord_client.event
    async def on_ready():
        global _discord_ready
        _discord_ready.set()
        await tree.sync()
        await _discord_log(f"Bot online — {_discord_client.user}")
        logger.info(f"Discord bot ready: {_discord_client.user}")

    # ── /setup command ──
    @tree.command(name="setup", description="Open VPN config panel")
    async def setup_cmd(interaction: discord.Interaction):
        view = discord.ui.View(timeout=None)

        async def buy_callback(button_interaction: discord.Interaction):
            if button_interaction.user.id != interaction.user.id:
                await button_interaction.response.send_message("Not for you.", ephemeral=True)
                return
            plan_view = discord.ui.View(timeout=None)
            async def plan_callback(bi: discord.Interaction, plan_name: str, price: str):
                if bi.user.id != interaction.user.id:
                    await bi.response.send_message("Not for you.", ephemeral=True)
                    return
                pay_view = discord.ui.View(timeout=None)
                async def pay_callback(bi2: discord.Interaction):
                    if bi2.user.id != interaction.user.id:
                        await bi2.response.send_message("Not for you.", ephemeral=True)
                        return
                    order_id = _build_receipt(bi2.user.id, plan_name, price)
                    uid = CONFIG.get("admin_id", "")
                    _pending_buys[order_id] = {
                        "user_id": str(bi2.user.id),
                        "plan": plan_name,
                        "amount": price,
                        "username": str(bi2.user),
                    }
                    admin_msg = (
                        f"New purchase\n"
                        f"User: {bi2.user.mention}\n"
                        f"Plan: {plan_name} ({price})\n"
                        f"Order: `{order_id}`"
                    )
                    if uid:
                        try:
                            u = await _discord_client.fetch_user(int(uid))
                            await u.send(admin_msg)
                        except:
                            pass
                    if CONFIG.get("log_channel"):
                        try:
                            ch = _discord_client.get_channel(int(CONFIG["log_channel"]))
                            if ch:
                                await ch.send(admin_msg)
                        except:
                            pass
                    await bi2.response.send_message(
                        f"Payment requested!\nPlan: **{plan_name}**\nAmount: **{price}**\nOrder: `{order_id}`\n\nWaiting for admin approval...",
                        ephemeral=True,
                    )
                    await _discord_log(f"Buy: {bi2.user} → {plan_name} ({price})")

                pay_btn = discord.ui.Button(label="Pay", style=discord.ButtonStyle.green)
                pay_btn.callback = pay_callback
                pay_view.add_item(pay_btn)
                await bi.response.send_message(
                    f"**{plan_name}** — {price}\n\nClick Pay to continue",
                    view=pay_view,
                    ephemeral=True,
                )

            plan_btns = [
                ("10 GB — 100K", "10 GB", "100K"),
                ("20 GB — 150K", "20 GB", "150K"),
                ("Unlimited — 250K", "Unlimited", "250K"),
            ]
            for label, pn, pr in plan_btns:
                btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
                btn.callback = lambda bi, p=pn, r=pr: plan_callback(bi, p, r)
                plan_view.add_item(btn)
            await bi.response.send_message("Select plan:", view=plan_view, ephemeral=True)

        buy_btn = discord.ui.Button(label="Buy Config", style=discord.ButtonStyle.green)
        buy_btn.callback = buy_callback
        view.add_item(buy_btn)
        await interaction.response.send_message(
            "**NERULA VPN Panel**\n\nGet your secure VPN config:",
            view=view,
            ephemeral=True,
        )

    # ── /status command ──
    @tree.command(name="status", description="Show bot status")
    async def status_cmd(interaction: discord.Interaction):
        guilds = len(_discord_client.guilds) if _discord_client.is_ready() else 0
        users = sum(g.member_count or 0 for g in _discord_client.guilds) if _discord_client.is_ready() else 0
        await interaction.response.send_message(
            f"**Status:** {'Online' if _discord_client.is_ready() else 'Offline'}\n"
            f"**Guilds:** {guilds}\n**Users:** {users}",
            ephemeral=True,
        )

    # ── approval buttons ──
    @_discord_client.event
    async def on_message(message):
        if message.author.bot:
            return
        if not CONFIG.get("admin_id"):
            return
        if str(message.author.id) != CONFIG["admin_id"]:
            return

        content = message.content.strip()
        # Check for approve/reject by order ID
        for oid, buy in list(_pending_buys.items()):
            if oid in content:
                lower = content.lower()
                if "approve" in lower or "/approve" in lower:
                    order = _receipts.get(oid, {})
                    uid = buy["user_id"]
                    try:
                        u = await _discord_client.fetch_user(int(uid))
                        await u.send(f"Order `{oid}` approved!\nPlan: **{buy['plan']}**\n\nYour config is being prepared.")
                    except:
                        pass
                    del _pending_buys[oid]
                    order["status"] = "approved"
                    await _discord_log(f"Approved: {oid}")
                elif "reject" in lower or "/reject" in lower:
                    uid = buy["user_id"]
                    try:
                        u = await _discord_client.fetch_user(int(uid))
                        await u.send(f"Order `{oid}` was rejected.\nContact support for details.")
                    except:
                        pass
                    del _pending_buys[oid]
                    _receipts[oid]["status"] = "rejected"
                    await _discord_log(f"Rejected: {oid}")

        # Handle receipt image upload
        if message.attachments:
            for att in message.attachments:
                if att.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                    await _discord_log(f"Receipt uploaded by {message.author}: {att.filename}")

    return _discord_client


async def start_discord_bot():
    global _discord_task, _discord_ready
    if _discord_task and not _discord_task.done():
        return
    if not CONFIG.get("bot_token"):
        return
    _discord_ready.clear()
    client = _get_discord_client()
    async def _run():
        try:
            await client.start(CONFIG["bot_token"])
        except Exception as e:
            logger.error(f"Discord error: {e}")
    _discord_task = asyncio.create_task(_run())
    try:
        await asyncio.wait_for(_discord_ready.wait(), timeout=30)
    except:
        pass

async def stop_discord_bot():
    global _discord_task, _discord_client, _discord_ready
    if _discord_task and not _discord_task.done():
        _discord_task.cancel()
    if _discord_client and _discord_client.is_ready():
        await _discord_client.close()
    _discord_client = None
    _discord_task = None
    _discord_ready.clear()

def discord_status():
    running = _discord_client is not None and _discord_client.is_ready()
    guilds = len(_discord_client.guilds) if running else 0
    users = sum(g.member_count or 0 for g in _discord_client.guilds) if running else 0
    channels = []
    if running and CONFIG.get("log_channel"):
        for g in _discord_client.guilds:
            for c in g.text_channels:
                channels.append({
                    "id": str(c.id),
                    "name": c.name,
                    "selected": str(c.id) == CONFIG.get("log_channel"),
                })
    return {
        "running": running,
        "guilds": guilds,
        "users": users,
        "channels": channels,
        "activity": _activity[-20:],
    }

# ══════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}

@app.get("/", response_class=HTMLResponse)
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if hash_pw(body.get("password", "")) == CONFIG.get("password_hash"):
        token = await make_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
        return resp
    return JSONResponse({"ok": False}, status_code=401)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not await check_auth(request):
        return RedirectResponse("/login")
    return DASHBOARD_HTML

@app.get("/discord", response_class=HTMLResponse)
async def discord_page(request: Request):
    if not await check_auth(request):
        return RedirectResponse("/login")
    return DISCORD_HTML

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not await check_auth(request):
        return RedirectResponse("/login")
    return SETTINGS_HTML

@app.get("/api/config")
async def api_config(request: Request):
    if not await check_auth(request):
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True, "config": {k: v for k, v in CONFIG.items() if k != "password_hash"}}

@app.post("/api/config")
async def api_config_save(request: Request):
    if not await check_auth(request):
        return JSONResponse({"ok": False}, status_code=401)
    body = await request.json()
    for k in ["bot_token", "admin_id", "log_channel", "usage_channel", "railway_api_key"]:
        if k in body:
            CONFIG[k] = str(body[k]).strip()
    save_config()
    return {"ok": True}

@app.post("/api/change-password")
async def change_password(request: Request):
    if not await check_auth(request):
        return JSONResponse({"ok": False}, status_code=401)
    body = await request.json()
    if hash_pw(body.get("old_password", "")) != CONFIG.get("password_hash"):
        return JSONResponse({"ok": False, "error": "Wrong password"}, status_code=400)
    new_pw = body.get("new_password", "")
    if len(new_pw) < 4:
        return JSONResponse({"ok": False, "error": "Min 4 chars"}, status_code=400)
    CONFIG["password_hash"] = hash_pw(new_pw)
    save_config()
    return {"ok": True}

@app.get("/api/discord/status")
async def discord_status_api(request: Request):
    if not await check_auth(request):
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True, **discord_status()}

@app.post("/api/discord/start")
async def discord_start(request: Request):
    if not await check_auth(request):
        return JSONResponse({"ok": False}, status_code=401)
    asyncio.create_task(start_discord_bot())
    return {"ok": True, "msg": "Starting..."}

@app.post("/api/discord/stop")
async def discord_stop(request: Request):
    if not await check_auth(request):
        return JSONResponse({"ok": False}, status_code=401)
    asyncio.create_task(stop_discord_bot())
    return {"ok": True, "msg": "Stopping..."}

@app.post("/api/discord/channel")
async def set_channel(request: Request):
    if not await check_auth(request):
        return JSONResponse({"ok": False}, status_code=401)
    body = await request.json()
    CONFIG["log_channel"] = body.get("channel", "")
    save_config()
    return {"ok": True}

@app.post("/railway")
async def railway_webhook(request: Request):
    try:
        body = await request.body()
        sig = request.headers.get("railway-signature", "")
        if CONFIG.get("railway_api_key"):
            if not verify_sig(body, sig, CONFIG["railway_api_key"]):
                return JSONResponse({"ok": False, "error": "Invalid signature"}, status_code=400)
        data = json.loads(body) if body else {}
        deployment = data.get("deployment", {})
        project = data.get("project", {})
        event_type = data.get("type", "unknown")
        msg = f"Railway: {event_type} — {deployment.get('id', '?')}"
        if CONFIG.get("log_channel") and _discord_client and _discord_client.is_ready():
            try:
                ch = _discord_client.get_channel(int(CONFIG["log_channel"]))
                if ch:
                    await ch.send(msg)
            except:
                pass
        await _discord_log(msg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ══════════════════════════════════════════════════════════════
# RAILWAY USAGE REPORTS
# ══════════════════════════════════════════════════════════════

async def _usage_report_loop():
    while True:
        try:
            if CONFIG.get("railway_api_key") and CONFIG.get("usage_channel"):
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        "https://backboard.railway.app/graphql",
                        json={
                            "query": '{ me { projects(first: 10) { edges { node { name id } } } } }'
                        },
                        headers={"Authorization": f"Bearer {CONFIG['railway_api_key']}"},
                        timeout=15,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        projects = data.get("data", {}).get("me", {}).get("projects", {}).get("edges", [])
                        if projects and _discord_client and _discord_client.is_ready():
                            ch = _discord_client.get_channel(int(CONFIG["usage_channel"]))
                            if ch:
                                msg = "📊 **Usage Report**\n"
                                for p in projects:
                                    msg += f"• {p['node']['name']}\n"
                                await ch.send(msg)
        except Exception as e:
            logger.warning(f"Usage report error: {e}")
        await asyncio.sleep(3600)

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    load_config()
    if CONFIG.get("bot_token"):
        asyncio.create_task(start_discord_bot())
    if CONFIG.get("railway_api_key"):
        asyncio.create_task(_usage_report_loop())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
