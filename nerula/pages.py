_RTL = "direction:rtl;text-align:right;font-family:Tahoma,Arial,sans-serif;"

def _shell(title, body_content):
    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — NERULA</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0e17;color:#e0e0e0;{_RTL}min-height:100vh}}
.topbar{{background:#111827;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #1f2937}}
.topbar h1{{font-size:18px;color:#22d3ee}}
.topbar a{{color:#6b7280;text-decoration:none;font-size:14px;margin-right:12px}}
.topbar a:hover{{color:#22d3ee}}
.main{{max-width:800px;margin:30px auto;padding:0 16px}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:24px;margin-bottom:16px}}
.card h2{{color:#22d3ee;margin-bottom:12px;font-size:16px}}
.card p{{color:#6b7280;font-size:13px;margin-bottom:12px}}
input,select{{width:100%;padding:10px 14px;border:1px solid #1f2937;border-radius:8px;background:#0a0e17;color:#e0e0e0;font-size:14px;margin-bottom:8px;{_RTL}}}
input:focus{{outline:none;border-color:#22d3ee}}
button{{padding:10px 20px;border:none;border-radius:8px;cursor:pointer;font-weight:bold;font-size:14px;margin:4px;{_RTL}}}
.btn-primary{{background:#22d3ee;color:#0a0e17}}
.btn-success{{background:#22c55e;color:#fff}}
.btn-danger{{background:#ef4444;color:#fff}}
.btn-secondary{{background:#1f2937;color:#9ca3af;border:1px solid #374151}}
button:hover{{opacity:0.9}}
.msg{{padding:10px;border-radius:8px;margin-bottom:12px;display:none;font-size:13px}}
.msg.ok{{background:#064e3b;color:#6ee7b7;display:block}}
.msg.err{{background:#450a0a;color:#fca5a5;display:block}}
.badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:bold}}
.badge.on{{background:#064e3b;color:#6ee7b7}}
.badge.off{{background:#450a0a;color:#fca5a5}}
.badge.wait{{background:#422006;color:#fbbf24}}
.nav{{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}}
.nav a{{display:inline-block;padding:8px 16px;border-radius:8px;background:#111827;border:1px solid #1f2937;color:#9ca3af;text-decoration:none;font-size:13px}}
.nav a:hover,.nav a.active{{background:#1f2937;color:#22d3ee;border-color:#22d3ee}}
.row{{display:flex;gap:8px;flex-wrap:wrap}}
.sep{{border-top:1px solid #1f2937;margin:16px 0}}
</style>
</head>
<body>
<div class="topbar">
<h1>NERULA</h1>
<div>
<a href="/dashboard">داشبورد</a>
<a href="/discord">بات دیسکورد</a>
<a href="/settings">تنظیمات</a>
</div>
</div>
<div class="main">
{body_content}
</div>
</body>
</html>"""


LOGIN_HTML = _shell("ورود به پنل", """
<div class="card" style="max-width:400px;margin:60px auto">
<h2>ورود به پنل مدیریت</h2>
<p>رمز عبور پیش‌فرض: NERULA2024</p>
<div id="msg" class="msg"></div>
<input type="password" id="pw" placeholder="رمز عبور..." autofocus>
<button class="btn-primary" onclick="doLogin()" style="width:100%">ورود</button>
</div>
<script>
async function doLogin(){
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
  const d=await r.json();
  if(d.ok) location.href='/dashboard';
  else{const m=document.getElementById('msg');m.className='msg err';m.textContent='رمز اشتباه است';}
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
</script>
""")

DASHBOARD_HTML = _shell("داشبورد", """
<div class="card">
<h2>داشبورد NERULA</h2>
<p>نسخه 1.0.0 — پنل مدیریت ربات دیسکورد فروش VPN</p>
</div>
<div class="nav">
<a class="active" href="/dashboard">داشبورد</a>
<a href="/discord">بات دیسکورد</a>
<a href="/settings">تنظیمات</a>
</div>
<div class="card">
<h2>وضعیت سیستم</h2>
<div id="info">در حال بارگذاری...</div>
</div>
<div class="card">
<h2>آخرین فعالیت‌ها</h2>
<div id="log">هنوز فعالیتی ثبت نشده</div>
</div>
<script>
(async()=>{
  try{
    const r=await fetch('/api/config');
    const d=await r.json();
    if(d.ok){
      const c=d.config;
      let html='';
      html+='<p>توکن دیسکورد: '+(c.bot_token?'<span class="badge on">تنظیم شده</span>':'<span class="badge off">تنظیم نشده</span>')+'</p>';
      html+='<p>آیدی ادمین: '+(c.admin_id||'—')+'</p>';
      html+='<p>Railway API: '+(c.railway_api_key?'<span class="badge on">تنظیم شده</span>':'<span class="badge off">تنظیم نشده</span>')+'</p>';
      document.getElementById('info').innerHTML=html;
    }
  }catch(e){}
})();
</script>
""")

DISCORD_HTML = _shell("بات دیسکورد", """
<div class="nav">
<a href="/dashboard">داشبورد</a>
<a class="active" href="/discord">بات دیسکورد</a>
<a href="/settings">تنظیمات</a>
</div>
<div class="card">
<h2>وضعیت ربات دیسکورد</h2>
<p>وضعیت: <span id="status" class="badge wait">در حال بررسی...</span></p>
<p>Bot ID: <span id="botid">—</span></p>
</div>
<div class="card">
<h2>کنترل ربات</h2>
<div id="msg" class="msg"></div>
<div class="row">
<button class="btn-success" onclick="startBot()">▶ استارت</button>
<button class="btn-danger" onclick="stopBot()">⏹ استاپ</button>
<button class="btn-primary" onclick="sendSetup()">📨 ارسال پیام فروش</button>
<button class="btn-secondary" onclick="loadChannels()">🔄 رفرش کانال‌ها</button>
</div>
</div>
<div class="card">
<h2>انتخاب کانال فروش</h2>
<div id="msg2" class="msg"></div>
<select id="channelid"><option value="">ابتدا ربات را استارت کنید</option></select>
<button class="btn-primary" onclick="saveChannel()">ذخیره کانال</button>
</div>
<script>
function showMsg(id,text,ok){
  const m=document.getElementById(id);
  m.className='msg '+(ok?'ok':'err');
  m.textContent=text;
  setTimeout(()=>m.className='msg',4000);
}
async function api(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  return r.json();
}
async function refreshStatus(){
  try{
    const d=await fetch('/api/discord/status').then(r=>r.json());
    const s=document.getElementById('status');
    const bid=document.getElementById('botid');
    if(d.running){s.className='badge on';s.textContent='فعال';}
    else if(d.configured){s.className='badge wait';s.textContent='آماده استارت';}
    else{s.className='badge off';s.textContent='تنظیم نشده';}
    bid.textContent=d.bot_id||'—';
  }catch(e){}
}
async function loadChannels(){
  const d=await api('/api/discord/channels');
  const sel=document.getElementById('channelid');
  sel.innerHTML='';
  if(d.ok&&d.channels.length){
    d.channels.forEach(c=>{sel.innerHTML+='<option value="'+c.id+'">'+c.name+'</option>';});
  }else{sel.innerHTML='<option value="">کانالی یافت نشد</option>';}
}
async function saveChannel(){
  const ch=document.getElementById('channelid').value;
  if(!ch){showMsg('msg2','کانالی انتخاب نکردید',false);return;}
  const d=await api('/api/config',{log_channel:ch});
  if(d.ok)showMsg('msg2','کانال ذخیره شد',true);
  else showMsg('msg2',d.error||'خطا',false);
}
async function startBot(){showMsg('msg','در حال استارت...',true);const d=await api('/api/discord/start');if(d.ok)showMsg('msg',d.msg,true);else showMsg('msg',d.error||'خطا',false);setTimeout(refreshStatus,5000);setTimeout(loadChannels,8000);}
async function stopBot(){const d=await api('/api/discord/stop');if(d.ok)showMsg('msg','متوقف شد',true);else showMsg('msg',d.error||'خطا',false);refreshStatus();}
async function sendSetup(){const d=await api('/api/discord/setup');if(d.ok)showMsg('msg','ارسال شد',true);else showMsg('msg',d.error||'خطا',false);}
refreshStatus();
</script>
""")

SETTINGS_HTML = _shell("تنظیمات", """
<div class="nav">
<a href="/dashboard">داشبورد</a>
<a href="/discord">بات دیسکورد</a>
<a class="active" href="/settings">تنظیمات</a>
</div>
<div class="card">
<h2>تنظیمات کلی</h2>
<div id="msg" class="msg"></div>
<label>توکن ربات دیسکورد:</label>
<input type="password" id="bot_token" placeholder="Discord Bot Token">
<label>آیدی ادمین دیسکورد:</label>
<input type="text" id="admin_id" placeholder="Discord User ID">
<label>کانال لاگ ( deployments + crash alerts ):</label>
<input type="text" id="log_channel" placeholder="Channel ID">
<label>کانال Usage Reports:</label>
<input type="text" id="usage_channel" placeholder="Channel ID (خالی = همان log_channel)">
<label>Railway API Key (اختیاری):</label>
<input type="password" id="railway_api_key" placeholder="Railway API Token">
<button class="btn-primary" onclick="saveAll()" style="width:100%;margin-top:8px">ذخیره همه</button>
</div>
<div class="card">
<h2>تغییر رمز عبور پنل</h2>
<div id="msg2" class="msg"></div>
<input type="password" id="new_pw" placeholder="رمز جدید (حداقل 4 کاراکتر)">
<button class="btn-danger" onclick="changePw()" style="width:100%;margin-top:8px">تغییر رمز</button>
</div>
<script>
function showMsg(id,text,ok){const m=document.getElementById(id);m.className='msg '+(ok?'ok':'err');m.textContent=text;setTimeout(()=>m.className='msg',4000);}
async function loadConfig(){
  const r=await fetch('/api/config');const d=await r.json();
  if(d.ok){const c=d.config;document.getElementById('bot_token').placeholder=c.bot_token?'توکن تنظیم شده':'توکن دیسکورد';document.getElementById('admin_id').value=c.admin_id||'';document.getElementById('log_channel').value=c.log_channel||'';document.getElementById('usage_channel').value=c.usage_channel||'';document.getElementById('railway_api_key').placeholder=c.railway_api_key?'کلید تنظیم شده':'Railway API Key';}
}
async function saveAll(){
  const body={};['bot_token','admin_id','log_channel','usage_channel','railway_api_key'].forEach(k=>{const v=document.getElementById(k).value.trim();if(v)body[k]=v;});
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.ok){showMsg('msg','ذخیره شد',true);loadConfig();}
  else showMsg('msg',d.error||'خطا',false);
}
async function changePw(){
  const pw=document.getElementById('new_pw').value.trim();
  if(!pw){showMsg('msg2','رمز جدید را وارد کنید',false);return;}
  const r=await fetch('/api/config/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({new_password:pw})});
  const d=await r.json();
  if(d.ok){showMsg('msg2','رمز تغییر کرد',true);document.getElementById('new_pw').value='';}
  else showMsg('msg2',d.error||'خطا',false);
}
loadConfig();
</script>
""")
