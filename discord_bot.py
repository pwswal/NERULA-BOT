import asyncio
import re
from datetime import datetime, timezone

from main import (
    logger, add_bot_log, DISCORD_CONFIG, LINKS, PLANS, TRIALS,
    create_bundle, create_trial, active_group, save_state, get_host,
    fmt_bytes, is_link_allowed, _bundle_items, remove_bundle,
    set_link_active, vless_link_for_link,
)

def _token() -> str:
    return (DISCORD_CONFIG.get("token") or "").strip()

def _admins() -> set:
    raw = (DISCORD_CONFIG.get("admin_ids") or "").strip()
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()} if raw else set()

def _password() -> str:
    return (DISCORD_CONFIG.get("admin_password") or "nerula2024").strip()

def _main_ch() -> str:
    return (DISCORD_CONFIG.get("channel_id") or "").strip()

_sessions: set = set()
_pending: dict = {}
_payments: dict = {}
_receipt_ch: dict = {}
_wizard: dict = {}

PAYMENT_CARD = "6104 - 3387 - 5956 - 2107"
PAYMENT_HOLDER = "اهورا ارپناهی"
PAGE_SIZE = 6
ACCENT = 0xA855F7
SUCCESS = 0x34D399
DANGER = 0xF87171
WARNING = 0xFBBF24
_IMG_RE = re.compile(r"https?://[^\s]+\.(?:png|jpe?g|gif|webp|bmp|avif|svg)", re.I)

try:
    import discord
    from discord import app_commands, ButtonStyle, SelectOption
    from discord.ui import View, Button, Select, Modal, TextInput
    _OK = True
except Exception:
    discord = None
    _OK = False

_client = None
_task = None
_last_error = ""


def _fa(n):
    try: n = int(float(n or 0))
    except: return "—"
    if n <= 0: return "رایگان"
    return f"{n:,}".replace(",", "٬").translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")) + " تومان"

def _vol(n):
    if not n: return "نامحدود"
    gb = n / 1024**3
    if gb >= 1: return f"{gb:g} گیگ"
    mb = n / 1024**2
    if mb >= 1: return f"{mb:g} مگ"
    return f"{n/1024:g} کیلو"

def _own(uid):
    items = [(u, l) for u, l in LINKS.items() if l.get("sub_members") or not l.get("bundle_main")]
    if uid in _admins(): return items
    return [(u, l) for u, l in items if str(l.get("owner", "")) == str(uid)]

def _can(inter, uid):
    return inter.user.id in _admins() or str(LINKS.get(uid, {}).get("owner", "")) == str(inter.user.id)


if not _OK:
    pass
else:

    def _emb(title, desc, color=ACCENT, foot=""):
        e = discord.Embed(title=title, description=desc, color=color)
        if foot: e.set_footer(text=foot)
        return e

    def _menu_emb():
        e = discord.Embed(title="🤖 ربات نرولا", description="از دکمه‌های زیر انتخاب کن.", color=ACCENT)
        e.add_field(name="🧪 TEST رایگان", value="۱۰۰ مگ — فقط یک بار", inline=False)
        e.add_field(name="🛒 خرید کانفیگ", value="انتخاب پلن و پرداخت", inline=False)
        e.add_field(name="📋 لیست کانفیگ‌ها", value="کانفیگ‌های خودت", inline=False)
        return e

    def _admin_emb():
        e = _menu_emb()
        e.add_field(name="⚙️ تنظیمات", value="تنظیمات ربات", inline=False)
        e.set_footer(text="پنل ادمین")
        return e

    def _sett_emb():
        e = discord.Embed(title="⚙️ تنظیمات", color=ACCENT)
        e.add_field(name="🔑 رمز", value=f"`{_password()}`", inline=True)
        e.add_field(name="👤 ادمین‌ها", value=", ".join(str(i) for i in sorted(_admins())) or "—", inline=True)
        e.add_field(name="📢 چنل", value=f"<#{_main_ch()}>" if _main_ch() else "—", inline=True)
        e.add_field(name="🔗 توکن", value="✅" if _token() else "❌", inline=True)
        e.set_footer(text="تغییر از پنل وب")
        return e

    def _plan_lbl(p):
        return f"{p.get('emoji','')} {p.get('name','')} : {_fa(p.get('price'))}"

    def _plan_emb():
        e = discord.Embed(title="🛒 انتخاب پلن", description="پلن رو انتخاب کن (۱ ماهه).", color=ACCENT)
        for p in PLANS.values():
            e.add_field(name=_plan_lbl(p), value="", inline=False)
        return e

    def _pay_emb(plan, grp):
        e = discord.Embed(title="💳 پرداخت", description=f"{plan.get('emoji','')} **{plan.get('name','')}**", color=ACCENT)
        e.add_field(name="💰 مبلغ", value=_fa(plan.get("price")), inline=True)
        e.add_field(name="💳 کارت", value=f"```\n{PAYMENT_CARD}\n```", inline=False)
        e.add_field(name="👤 به نام", value=PAYMENT_HOLDER, inline=False)
        e.add_field(name="📦 حجم", value=_vol(plan.get("limit_bytes", 0)), inline=True)
        e.add_field(name="📅 روز", value=str(int(plan.get("days") or 30)), inline=True)
        e.set_footer(text="«✅ پرداخت کردم» رو بزن")
        return e

    def _done_emb(res):
        host = get_host()
        uid = res["main_uid"]
        e = discord.Embed(title="✅ کانفیگ ساخته شد", description=res.get("plan", ""), color=SUCCESS)
        e.add_field(name="📥 ساب", value=f"```\nhttps://{host}/sub/{uid}\n```", inline=False)
        e.add_field(name="✨ صفحه", value=f"```\nhttps://{host}/p/{uid}\n```", inline=False)
        return e

    def _list_emb(uid, pg=0):
        items = _own(uid)
        s = pg * PAGE_SIZE
        ch = items[s:s + PAGE_SIZE]
        e = discord.Embed(title="📋 کانفیگ‌ها", color=ACCENT)
        if not ch:
            e.description = "خالیه."
            return e
        e.description = f"تعداد: **{len(items)}**"
        for u, l in ch:
            dot = "🟢" if is_link_allowed(l) else "🔴"
            lim = "نامحدود" if not l.get("limit_bytes") else fmt_bytes(l["limit_bytes"])
            e.add_field(name=f"{dot} {l.get('label','?')[:40]}", value=f"`{u[:8]}…` · {fmt_bytes(l.get('used_bytes',0))}/{lim}", inline=False)
        e.set_footer(text=f"صفحه {pg+1}")
        return e

    def _det_emb(uid, l):
        s = "🟢 فعال" if is_link_allowed(l) else "🔴 غیرفعال"
        lim = "نامحدود" if not l.get("limit_bytes") else fmt_bytes(l["limit_bytes"])
        exp = (l.get("expires_at") or "—").split("T")[0]
        e = discord.Embed(title=l.get("label", "?"), description=s, color=ACCENT)
        e.add_field(name="مصرف", value=f"{fmt_bytes(l.get('used_bytes',0))} / {lim}", inline=True)
        e.add_field(name="📅 انقضا", value=exp, inline=True)
        e.add_field(name="🆔", value=f"`{uid}`", inline=False)
        return e

    def _link_emb(uid, l):
        host = get_host()
        e = discord.Embed(title=f"🔗 {l.get('label')}", color=SUCCESS)
        e.add_field(name="ساب", value=f"```\nhttps://{host}/sub/{uid}\n```", inline=False)
        e.add_field(name="صفحه", value=f"```\nhttps://{host}/p/{uid}\n```", inline=False)
        return e

    def _appr_emb(user_id, plan, ch):
        e = discord.Embed(title="🛒 خرید جدید", description=f"{ch} خرید کرده.", color=WARNING)
        e.add_field(name="📦", value=plan, inline=True)
        e.add_field(name="👤", value=f"<@{user_id}>", inline=True)
        return e

    async def _deny(inter):
        try: await inter.response.send_message("⛔ دسترسی نداری", ephemeral=True)
        except: pass

    def _cb(fn, *a):
        async def cb(inter): await fn(inter, *a)
        return cb

    # ─── Views ───────────────────────────────────────────────────────────────
    class MenuView(View):
        def __init__(self, admin=False):
            super().__init__(timeout=None)

        @discord.ui.button(label="🧪 TEST رایگان", style=ButtonStyle.secondary, row=0)
        async def _test(self, inter, _):
            if str(inter.user.id) in TRIALS:
                await inter.response.send_message("🧪 قبلاً گرفتی.", ephemeral=True); return
            await inter.response.send_modal(TrialModal())

        @discord.ui.button(label="📋 لیست کانفیگ‌ها", style=ButtonStyle.primary, row=1)
        async def _list(self, inter, _):
            await inter.response.send_message(embed=_list_emb(inter.user.id), view=ListView(0, inter.user.id), ephemeral=True)

        @discord.ui.button(label="🛒 خرید کانفیگ", style=ButtonStyle.success, row=1)
        async def _buy(self, inter, _):
            if not PLANS:
                await inter.response.send_message("پلنی نیست.", ephemeral=True); return
            _wizard[inter.user.id] = {"step": "plan", "data": {}}
            await inter.response.send_message(embed=_plan_emb(), view=PlanSel(_wizard[inter.user.id]), ephemeral=True)

        @discord.ui.button(label="⚙️ تنظیمات", style=ButtonStyle.secondary, row=2)
        async def _sett(self, inter, _):
            if inter.user.id not in _sessions:
                await inter.response.send_message("⛔ اول /login", ephemeral=True); return
            await inter.response.send_message(embed=_sett_emb(), ephemeral=True)

        @discord.ui.button(label="🔄 رفرش", style=ButtonStyle.secondary, row=2)
        async def _ref(self, inter, _):
            if inter.user.id in _sessions:
                await inter.response.edit_message(embed=_admin_emb(), view=MenuView(True))
            else:
                await inter.response.edit_message(embed=_menu_emb(), view=MenuView(False))

    class ListView(View):
        def __init__(self, pg=0, uid=None):
            super().__init__(timeout=None)
            items = _own(uid) if uid else []
            s = pg * PAGE_SIZE
            for u, l in items[s:s+PAGE_SIZE]:
                dot = "🟢" if is_link_allowed(l) else "🔴"
                b = Button(label=f"{dot} {l.get('label','?')[:28]}", style=ButtonStyle.secondary)
                b.callback = _cb(self._open, u)
                self.add_item(b)
            if s > 0:
                b = Button(label="◀", style=ButtonStyle.primary)
                b.callback = _cb(self._pg, pg - 1, uid)
                self.add_item(b)
            if s + PAGE_SIZE < len(items):
                b = Button(label="▶", style=ButtonStyle.primary)
                b.callback = _cb(self._pg, pg + 1, uid)
                self.add_item(b)
            b = Button(label="🛒 خرید", style=ButtonStyle.success)
            b.callback = _cb(self._buy, uid)
            self.add_item(b)
            b = Button(label="⬅ منو", style=ButtonStyle.secondary)
            b.callback = _cb(self._menu, uid)
            self.add_item(b)

        async def _open(self, inter, uid):
            if not _can(inter, uid): await _deny(inter); return
            l = LINKS.get(uid)
            if not l: await inter.response.edit_message(embed=_menu_emb(), view=MenuView()); return
            await inter.response.edit_message(embed=_det_emb(uid, l), view=DetailView(uid, l["active"]))

        async def _pg(self, inter, pg, uid):
            await inter.response.edit_message(embed=_list_emb(uid, pg), view=ListView(pg, uid))

        async def _buy(self, inter, uid):
            if not PLANS: await inter.response.send_message("پلنی نیست.", ephemeral=True); return
            _wizard[inter.user.id] = {"step": "plan", "data": {}}
            await inter.response.send_message(embed=_plan_emb(), view=PlanSel(_wizard[inter.user.id]), ephemeral=True)

        async def _menu(self, inter, uid):
            v = MenuView(inter.user.id in _sessions)
            await inter.response.edit_message(embed=_admin_emb() if inter.user.id in _sessions else _menu_emb(), view=v)

    class DetailView(View):
        def __init__(self, uid, active):
            super().__init__(timeout=None)
            self._uid = uid
            b = Button(label="🔗 لینک اتصال", style=ButtonStyle.primary)
            b.callback = self._show
            self.add_item(b)
            b = Button(label="🗑 حذف", style=ButtonStyle.danger)
            b.callback = self._del
            self.add_item(b)
            b = Button(label="⬅ بازگشت", style=ButtonStyle.secondary)
            b.callback = self._back
            self.add_item(b)

        async def _show(self, inter):
            if not _can(inter, self._uid): await _deny(inter); return
            l = LINKS.get(self._uid)
            if l: await inter.response.send_message(embed=_link_emb(self._uid, l), ephemeral=True)

        async def _del(self, inter):
            if not _can(inter, self._uid): await _deny(inter); return
            l = LINKS.get(self._uid)
            if not l: return
            e = _emb("❗️ حذف؟", f"«{l.get('label')}» حذف بشه?", DANGER)
            v = ConfirmDel(self._uid)
            await inter.response.edit_message(embed=e, view=v)

        async def _back(self, inter):
            await inter.response.edit_message(embed=_list_emb(inter.user.id), view=ListView(0, inter.user.id))

    class ConfirmDel(View):
        def __init__(self, uid):
            super().__init__(timeout=None)
            self._uid = uid
            b = Button(label="✅ بله", style=ButtonStyle.danger)
            b.callback = self._y
            self.add_item(b)
            b = Button(label="❌ نه", style=ButtonStyle.secondary)
            b.callback = self._n
            self.add_item(b)

        async def _y(self, inter):
            if not _can(inter, self._uid): await _deny(inter); return
            lbl = await remove_bundle(self._uid)
            await inter.response.edit_message(embed=_emb("🗑 حذف شد", f"«{lbl}» حذف شد.", SUCCESS), view=MenuView())

        async def _n(self, inter):
            if not _can(inter, self._uid): await _deny(inter); return
            l = LINKS.get(self._uid)
            if l: await inter.response.edit_message(embed=_det_emb(self._uid, l), view=DetailView(self._uid, l["active"]))

    class ApproveRejectView(View):
        def __init__(self, buyer_id):
            super().__init__(timeout=None)
            self._bid = buyer_id

        @discord.ui.button(label="✅ تایید", style=ButtonStyle.success)
        async def _ok(self, inter, _):
            if inter.user.id not in _sessions:
                await inter.response.send_message("⛔ /login", ephemeral=True); return
            ap = _pending.get(self._bid)
            if not ap or ap["s"] != "wait":
                await inter.response.send_message("منقضی شده.", ephemeral=True); return
            ap["s"] = "approved"
            ap["by"] = inter.user.id
            host = get_host()
            await inter.response.edit_message(
                embed=_emb("✅ تایید شد", f"لینک ساب:\n```\nhttps://{host}/sub/{ap['uid']}\n```\n\nحالا لینک رو **اینجا (DM)** بفرست.", SUCCESS),
                view=None,
            )

        @discord.ui.button(label="❌ رد", style=ButtonStyle.danger)
        async def _no(self, inter, _):
            if inter.user.id not in _sessions:
                await inter.response.send_message("⛔ /login", ephemeral=True); return
            ap = _pending.get(self._bid)
            if not ap or ap["s"] != "wait":
                await inter.response.send_message("منقضی شده.", ephemeral=True); return
            ch_id = ap.get("ch")
            if ch_id:
                try:
                    ch = _client.get_channel(ch_id) or await _client.fetch_channel(ch_id)
                    if ch: await ch.send("❌ خرید تایید نشد.")
                except: pass
            _pending.pop(self._bid, None)
            await inter.response.edit_message(embed=_emb("❌ رد شد", "رد شد.", DANGER), view=None)

    class PlanSel(View):
        def __init__(self, wiz):
            super().__init__(timeout=None)
            self._w = wiz
            self.sel = Select(placeholder="انتخاب پلن", options=[SelectOption(label=_plan_lbl(p), value=k) for k, p in PLANS.items()])
            self.sel.callback = self._sel
            self.add_item(self.sel)
            b = Button(label="❌ انصراف", style=ButtonStyle.secondary)
            b.callback = self._can
            self.add_item(b)

        async def _sel(self, inter):
            w = self._w
            if w.get("step") != "plan":
                await inter.response.send_message("مرحله نامعتبر.", ephemeral=True); return
            pid = self.sel.values[0]
            plan = PLANS.get(pid)
            if not plan: return
            w["data"]["pid"] = pid
            grp = active_group() or {"configs": []}
            if int(plan.get("price") or 0) <= 0:
                w["step"] = "name"
                await inter.response.send_modal(NameModal(w))
                return
            w["step"] = "confirm"
            await inter.response.edit_message(embed=_pay_emb(plan, grp), view=PayView(w))

        async def _can(self, inter):
            _wizard.pop(inter.user.id, None)
            v = MenuView(inter.user.id in _sessions)
            await inter.response.edit_message(embed=_menu_emb(), view=v)

    class PayView(View):
        def __init__(self, wiz):
            super().__init__(timeout=None)
            self._w = wiz

        async def _back(self, inter):
            v = MenuView(inter.user.id in _sessions)
            try: await inter.response.edit_message(embed=_menu_emb(), view=v)
            except: await inter.response.send_message(embed=_menu_emb(), view=v, ephemeral=True)

        @discord.ui.button(label="✅ پرداخت کردم", style=ButtonStyle.success)
        async def _paid(self, inter, _):
            w = self._w
            if w.get("step") != "confirm":
                await inter.response.send_message("مرحله نامعتبر.", ephemeral=True); return
            pid = w["data"].get("pid", "")
            plan = PLANS.get(pid)
            if not plan: return
            if inter.guild is None:
                await inter.response.send_message("فقط در سرور.", ephemeral=True); return
            try:
                ch = await inter.guild.create_text_channel(f"رسید-{inter.user.id}", topic=f"رسید {inter.user.display_name}")
                await ch.set_permissions(inter.guild.default_role, read_messages=False, send_messages=False)
                me = inter.guild.get_member(inter.client.user.id) or inter.guild.me
                if me: await ch.set_permissions(me, read_messages=True, send_messages=True)
                member = inter.guild.get_member(inter.user.id) or inter.user
                await ch.set_permissions(member, read_messages=True, send_messages=True)
            except Exception as e:
                await inter.response.send_message(f"❌ ساخت چنل ممکن نشد: {e}", ephemeral=True); return
            _payments[inter.user.id] = {"pid": pid, "ch": ch.id, "stage": "wait"}
            _receipt_ch[ch.id] = inter.user.id
            try:
                await ch.send(f"👋 {inter.user.mention}\nانتخاب: {plan.get('emoji','')} **{plan.get('name','')}** — **{_fa(plan.get('price'))}**\n\n🖼 **عکس رسید** رو همین‌جا بفرست.")
            except: pass
            _wizard.pop(inter.user.id, None)
            await self._back(inter)

        @discord.ui.button(label="❌ لغو", style=ButtonStyle.danger)
        async def _cancel(self, inter, _):
            _wizard.pop(inter.user.id, None)
            await self._back(inter)

    class NameModal(Modal):
        def __init__(self, wiz):
            super().__init__(title="اسم کانفیگ")
            self._w = wiz
            self.inp = TextInput(label="اسم", placeholder="مثلاً: من", max_length=60, required=False)
            self.add_item(self.inp)

        async def on_submit(self, inter):
            name = (self.inp.value or "").strip()
            w = self._w
            if w.get("step") not in ("name", "confirm"):
                await inter.response.send_message("مرحله نامعتبر.", ephemeral=True); return
            pid = w["data"].get("pid", "")
            plan = PLANS.get(pid)
            if not plan: return
            grp = active_group() or {"configs": []}
            res = await create_bundle(plan, grp, owner=str(inter.user.id), name=name)
            _wizard.pop(inter.user.id, None)
            try: await inter.response.send_message(embed=_done_emb(res), ephemeral=True)
            except:
                try: await inter.followup.send(embed=_done_emb(res), ephemeral=True)
                except: pass
            host = get_host()
            try: await inter.followup.send(f"📥 ساب:\n```\nhttps://{host}/sub/{res['main_uid']}\n```", ephemeral=True)
            except: pass

    class TrialModal(Modal):
        def __init__(self):
            super().__init__(title="🧪 تست رایگان")
            self.inp = TextInput(label="اسم", placeholder="اختیاری", max_length=60, required=False)
            self.add_item(self.inp)

        async def on_submit(self, inter):
            if str(inter.user.id) in TRIALS:
                await inter.response.send_message("🧪 قبلاً گرفتی.", ephemeral=True); return
            name = (self.inp.value or "").strip()
            try: res, st = await create_trial(str(inter.user.id), name=name)
            except Exception as e:
                logger.warning(f"Trial failed: {e}")
                await inter.response.send_message("❌ خطا.", ephemeral=True); return
            if st != "ok" or not res:
                await inter.response.send_message("🧪 قبلاً گرفتی.", ephemeral=True); return
            try: await inter.response.send_message(embed=_done_emb(res), ephemeral=True)
            except:
                try: await inter.followup.send(embed=_done_emb(res), ephemeral=True)
                except: pass
            host = get_host()
            try: await inter.followup.send(f"📥 ساب:\n```\nhttps://{host}/sub/{res['main_uid']}\n```", ephemeral=True)
            except: pass


# ─── رسید در چنل تیکت ─────────────────────────────────────────────────────
if _OK:
    async def _on_receipt(client, msg, uid):
        pay = _payments.get(uid)
        if not pay or pay.get("stage") != "wait": return
        atts = list(getattr(msg, "attachments", []) or [])
        has_img = any(getattr(e, "image", None) and e.image.url for e in (getattr(msg, "embeds", []) or []))
        if not atts and not has_img:
            try:
                fresh = await msg.channel.fetch_message(msg.id)
                atts = list(getattr(fresh, "attachments", []) or [])
            except: pass
        if not atts and not has_img and not _IMG_RE.search(msg.content or ""):
            await msg.channel.send("🖼 عکس رسید رو بفرست."); return
        pay["stage"] = "proc"
        plan = PLANS.get(pay.get("pid"))
        if not plan:
            await msg.channel.send("❌ پلن نیست."); return
        try:
            grp = active_group() or {"configs": []}
            res = await create_bundle(plan, grp, owner=str(uid), name="")
        except Exception as e:
            logger.warning(f"Bundle failed: {e}")
            pay["stage"] = "wait"
            await msg.channel.send("❌ خطا. دوباره بفرست."); return
        plan_name = f"{plan.get('emoji','')} {plan.get('name','')}"
        _pending[uid] = {"s": "wait", "pn": plan_name, "uid": res["main_uid"], "ch": msg.channel.id}
        await msg.channel.send("⏳ **در انتظار تایید ادمین**...")
        for aid in _admins():
            try:
                u = client.get_user(aid) or await client.fetch_user(aid)
                if u:
                    dm = await u.create_dm()
                    await dm.send(embed=_appr_emb(uid, plan_name, msg.channel.mention), view=ApproveRejectView(uid))
            except Exception as e:
                logger.warning(f"Approval DM {aid} failed: {e}")

    async def _on_dm(client, msg):
        uid = msg.author.id
        if uid not in _admins() or uid not in _sessions: return
        bid = None
        for k, ap in _pending.items():
            if ap.get("s") == "approved" and ap.get("by") == uid:
                bid = k; break
        if bid is None: return
        ch_id = _pending[bid].get("ch")
        if not ch_id: return
        try:
            ch = client.get_channel(ch_id) or await client.fetch_channel(ch_id)
            if not ch: await msg.author.send("❌ چنل پیدا نشد."); return
            txt = msg.content or ""
            for a in (getattr(msg, "attachments", []) or []):
                txt = f"{txt}\n{a.url}" if txt else a.url
            if not txt.strip():
                await msg.author.send("❌ لینک یا کانفیگ رو بفرست."); return
            await ch.send(f"🔗 بفرمایید لینک ساب کانفیگ تون:\n\n{txt}")
            await msg.author.send("✅ ارسال شد.")
            _pending.pop(bid, None)
        except Exception as e:
            await msg.author.send(f"❌ خطا: {e}")


# ─── Slash Commands ────────────────────────────────────────────────────────
if _OK:
    async def panel_cmd(interaction: discord.Interaction):
        v = MenuView(interaction.user.id in _sessions)
        await interaction.response.send_message(embed=_admin_emb() if interaction.user.id in _sessions else _menu_emb(), view=v, ephemeral=True)

    async def login_cmd(interaction: discord.Interaction, password: str):
        if interaction.user.id not in _admins():
            await interaction.response.send_message("⛔ ادمین نیستی.", ephemeral=True); return
        if password == _password():
            _sessions.add(interaction.user.id)
            await interaction.response.send_message(embed=_emb("✅ ورود!", "خوش اومدی.", SUCCESS), view=MenuView(True), ephemeral=True)
        else:
            await interaction.response.send_message("❌ رمز اشتباه.", ephemeral=True)

    async def logout_cmd(interaction: discord.Interaction):
        _sessions.discard(interaction.user.id)
        await interaction.response.send_message(embed=_emb("✅ خروج", "خارج شدی.", SUCCESS), view=MenuView(False), ephemeral=True)


# ─── Client ────────────────────────────────────────────────────────────────
if _OK:

    class Bot(discord.Client):
        def __init__(self):
            intents = discord.Intents.default()
            intents.message_content = True
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self):
            try:
                self.tree.command(name="panel", description="منوی مدیریت")(panel_cmd)
                self.tree.command(name="login", description="ورود به پنل")(login_cmd)
                self.tree.command(name="logout", description="خروج")(logout_cmd)
                await asyncio.wait_for(self.tree.sync(), timeout=15)
            except asyncio.TimeoutError:
                logger.warning("Discord sync timeout — ادامه بدون sync")
            except Exception as e:
                logger.warning(f"Discord sync error: {e}")

        async def on_ready(self):
            logger.info(f"✅ Discord: {self.user}")
            cid = _main_ch()
            if cid:
                try:
                    ch = self.get_channel(int(cid)) or await self.fetch_channel(int(cid))
                    if ch: await ch.send(embed=_menu_emb(), view=MenuView(True))
                except Exception as e:
                    logger.warning(f"Channel send failed: {e}")

        async def on_message(self, message):
            if message.author.bot: return
            if message.guild is None:
                try: await _on_dm(self, message)
                except Exception as e: logger.warning(f"DM error: {e}")
                return
            uid = _receipt_ch.get(message.channel.id)
            if uid is not None:
                try: await _on_receipt(self, message, uid)
                except Exception as e: logger.warning(f"Receipt error: {e}")


# ─── Lifecycle ─────────────────────────────────────────────────────────────
async def _runner(token):
    global _client, _task, _last_error
    try:
        _last_error = ""
        await _client.start(token)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _last_error = str(e)
        logger.error(f"Discord connect failed: {e}")
    finally:
        try: await _client.close()
        except: pass
        _client = None
        _task = None


async def start_bot():
    global _client, _task
    if not _OK:
        logger.warning("discord.py نصب نیست."); return
    tok = _token()
    if not tok:
        logger.info("Discord: توکن نیست."); return
    logger.info(f"Discord: شروع اتصال...")
    _client = Bot()
    _task = asyncio.create_task(_runner(tok))


async def stop_bot():
    global _client, _task
    if _client:
        try: await _client.close()
        except: pass
        _client = None
    if _task:
        try: _task.cancel()
        except: pass
        _task = None


# ─── API ───────────────────────────────────────────────────────────────────
def _invite_url():
    cid = None
    if _client and _client.user: cid = _client.user.id
    if not cid and _token():
        try:
            import base64
            p = _token().split(".")[0]
            cid = int.from_bytes(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)), "big")
        except: pass
    if not cid: return None
    return f"https://discord.com/oauth2/authorize?client_id={cid}&permissions=534723950656&scope=bot"

def get_status():
    run = _client is not None and not _client.is_closed()
    rdy = run and _client.is_ready()
    return {
        "configured": bool(_token()),
        "running": run,
        "ready": rdy,
        "bot_name": str(_client.user) if (rdy and _client.user) else None,
        "admins": sorted(_admins()),
        "admin_password": _password(),
        "channel_id": _main_ch() or None,
        "invite_url": _invite_url(),
        "last_error": _last_error or None,
    }

async def list_channels():
    if not _client or _client.is_closed() or not _client.is_ready():
        return {"ok": False, "error": "ربات متصل نیست", "guilds": []}
    guilds = []
    for g in list(_client.guilds):
        chs = list(g.channels)
        if not chs:
            try: chs = await asyncio.wait_for(g.fetch_channels(), timeout=8)
            except: chs = []
        txts = [{"id": str(c.id), "name": f"#{c.name}"} for c in chs if getattr(c, "type", None) in (discord.ChannelType.text, discord.ChannelType.news)]
        guilds.append({"id": str(g.id), "name": g.name, "channels": txts})
    return {"ok": True, "guilds": guilds}

async def send_panel(cid):
    if not _client or _client.is_closed():
        return {"ok": False, "error": "روشن نیست"}
    try:
        ch = _client.get_channel(int(cid)) or await _client.fetch_channel(int(cid))
        if not ch: return {"ok": False, "error": "چنل نیست"}
        await ch.send(embed=_menu_emb(), view=MenuView(True))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def apply_config(cfg):
    tok = (cfg.get("token") or "").strip()
    if tok == "••••••••": tok = _token()
    DISCORD_CONFIG["token"] = tok
    DISCORD_CONFIG["admin_ids"] = (cfg.get("admin_ids") or "").strip()
    DISCORD_CONFIG["admin_password"] = (cfg.get("admin_password") or "nerula2024").strip()
    DISCORD_CONFIG["channel_id"] = (cfg.get("channel_id") or "").strip()
    await save_state()
    await stop_bot()
    if _token(): await start_bot()
    return {"ok": True}
