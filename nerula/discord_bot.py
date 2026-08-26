import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("NERULA_DISCORD")

DATA_DIR = Path(os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "nerula_data")))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path("/tmp/nerula_data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

CARD_NUMBER = "6104-3387-5956-2107"
CARD_HOLDER = "آهورا ارپناهی"
PLANS = {
    "10gb": {"label": "10 گیگابایت", "price": "100,000 تومان"},
    "20gb": {"label": "20 گیگابایت", "price": "150,000 تومان"},
    "unlimited": {"label": "نامحدود", "price": "250,000 تومان"},
}

discord_config: dict = {}
tickets: dict = {}
_bot = None
_task = None
_discord = None
_BuyView = None
_PlanView = None
_PayView = None
_ApprovalView = None


def is_ready() -> bool:
    return _bot is not None and _bot.is_ready() and not _bot.is_closed()


def get_bot_user_id() -> str | None:
    if _bot and _bot.is_ready() and _bot.user:
        return str(_bot.user.id)
    return None


def get_guild_channels() -> list:
    if not _bot or not _bot.is_ready():
        return []
    result = []
    for guild in _bot.guilds:
        try:
            for ch in guild.text_channels:
                perms = ch.permissions_for(guild.me)
                if perms.send_messages and perms.view_channel:
                    result.append({"id": str(ch.id), "name": f"#{ch.name} ({guild.name})"})
        except Exception as e:
            logger.warning(f"Error listing guild {guild.name}: {e}")
    return result


def get_channel(channel_id):
    if not _bot or not _bot.is_ready():
        return None
    try:
        return _bot.get_channel(int(channel_id))
    except Exception:
        return None


def make_setup_embed():
    return _discord.Embed(
        title="خرید کانفیگ VPN از NERULA",
        description="برای خرید کانفیگ، روی دکمه زیر کلیک کنید.",
        color=_discord.Color.green(),
        fields=[
            _discord.EmbedField(
                name="تعرفه‌ها",
                value=(
                    "• **10 گیگابایت** — 100,000 تومان\n"
                    "• **20 گیگابایت** — 150,000 تومان\n"
                    "• **نامحدود** — 250,000 تومان"
                ),
            )
        ],
        footer=_discord.EmbedFooter(text="NERULA VPN Panel"),
    )


def make_buy_view():
    return _BuyView()


async def _show_payment(interaction, plan_key: str):
    plan = PLANS[plan_key]
    embed = _discord.Embed(
        title="اطلاعات پرداخت",
        description=(
            f"**پلن انتخابی:** {plan['label']}\n"
            f"**مبلغ:** {plan['price']}\n\n"
            f"**شماره کارت:**\n`{CARD_NUMBER}`\n\n"
            f"**به نام:** {CARD_HOLDER}"
        ),
        color=_discord.Color.gold(),
        footer=_discord.EmbedFooter(text="بعد از پرداخت، روی دکمه «پرداخت شد» کلیک کنید"),
    )
    await interaction.response.send_message(embed=embed, view=_PayView(plan_key), ephemeral=True)


def _register_views(discord_module):
    global _discord, _BuyView, _PlanView, _PayView, _ApprovalView
    _discord = discord_module

    class BuyView(discord_module.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord_module.ui.button(label="خرید کانفیگ", style=discord_module.ButtonStyle.success, custom_id="nerula_buy_v2")
        async def buy(self, interaction, button):
            embed = discord_module.Embed(
                title="انتخاب پلن",
                description="پلن مورد نظر خود را انتخاب کنید:",
                color=discord_module.Color.blue(),
            )
            await interaction.response.send_message(embed=embed, view=PlanView(), ephemeral=True)

    class PlanView(discord_module.ui.View):
        def __init__(self):
            super().__init__(timeout=120)

        @discord_module.ui.button(label="10 گیگ — 100K", style=discord_module.ButtonStyle.primary, custom_id="plan_10gb_v2")
        async def p10(self, interaction, button):
            await _show_payment(interaction, "10gb")

        @discord_module.ui.button(label="20 گیگ — 150K", style=discord_module.ButtonStyle.primary, custom_id="plan_20gb_v2")
        async def p20(self, interaction, button):
            await _show_payment(interaction, "20gb")

        @discord_module.ui.button(label="نامحدود — 250K", style=discord_module.ButtonStyle.primary, custom_id="plan_unlimited_v2")
        async def punl(self, interaction, button):
            await _show_payment(interaction, "unlimited")

    class PayView(discord_module.ui.View):
        def __init__(self, plan_key: str):
            super().__init__(timeout=300)
            self.plan_key = plan_key

        @discord_module.ui.button(label="پرداخت شد", style=discord_module.ButtonStyle.success, custom_id="nerula_paid_v2")
        async def paid(self, interaction, button):
            guild = interaction.guild
            user = interaction.user
            if not guild:
                await interaction.response.send_message("خطا: در سرور اجرا کنید.", ephemeral=True)
                return
            plan = PLANS[self.plan_key]
            overwrites = {
                guild.default_role: discord_module.PermissionOverwrite(view_channel=False),
                user: discord_module.PermissionOverwrite(view_channel=True, send_messages=True),
                guild.me: discord_module.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            try:
                admin_user = await _bot.fetch_user(int(discord_config.get("admin_id", "0")))
                overwrites[admin_user] = discord_module.PermissionOverwrite(view_channel=True, send_messages=True)
            except Exception:
                pass
            try:
                ch = await guild.create_text_channel(
                    name=f"ticket-{user.name}", overwrites=overwrites,
                    reason=f"VPN ticket for {user.name}",
                )
            except Exception as e:
                await interaction.response.send_message(f"خطا در ساخت کانال: {e}", ephemeral=True)
                return
            tickets[ch.id] = {
                "user_id": user.id,
                "plan_key": self.plan_key,
                "plan_name": plan["label"],
                "status": "waiting_receipt",
                "created_at": datetime.now().isoformat(),
            }
            embed = discord_module.Embed(
                title="تیکت خرید کانفیگ",
                description=(
                    f"{user.mention} خوش آمدید!\n\n"
                    f"**پلن:** {plan['label']}\n"
                    f"**مبلغ:** {plan['price']}\n\n"
                    f"لطفا **اسکرین‌شات رسید پرداخت** را همینجا ارسال کنید."
                ),
                color=discord_module.Color.blue(),
            )
            await ch.send(embed=embed)
            await interaction.response.send_message(f"تیکت شما ساخته شد: {ch.mention}", ephemeral=True)

        @discord_module.ui.button(label="لغو", style=discord_module.ButtonStyle.danger, custom_id="nerula_cancel_v2")
        async def cancel(self, interaction, button):
            await interaction.response.send_message("لغو شد.", ephemeral=True)
            self.stop()

    class ApprovalView(discord_module.ui.View):
        def __init__(self, ch_id: int, user_id: int):
            super().__init__(timeout=None)
            self.ch_id = ch_id
            self.user_id = user_id

        @discord_module.ui.button(label="تایید", style=discord_module.ButtonStyle.success, custom_id="nerula_approve_v2")
        async def approve(self, interaction, button):
            if str(interaction.user.id) != str(discord_config.get("admin_id", "")):
                await interaction.response.send_message("فقط ادمین.", ephemeral=True)
                return
            t = tickets.get(self.ch_id)
            if not t:
                await interaction.response.send_message("تیکت یافت نشد.", ephemeral=True)
                return
            t["status"] = "approved"
            ch = _bot.get_channel(self.ch_id)
            if ch:
                await ch.send(embed=discord_module.Embed(
                    title="پرداخت تایید شد",
                    description="کانفیگ شما به‌زودی ارسال خواهد شد.",
                    color=discord_module.Color.green(),
                ))
                try:
                    u = await _bot.fetch_user(self.user_id)
                    await u.send(embed=discord_module.Embed(
                        title="سفارش تایید شد",
                        description=f"پلن: {t['plan_name']}\nکانفیگ شما آماده ارسال است.",
                        color=discord_module.Color.green(),
                    ))
                except Exception:
                    pass
            await interaction.response.send_message("تایید شد", ephemeral=True)
            self.stop()

        @discord_module.ui.button(label="رد", style=discord_module.ButtonStyle.danger, custom_id="nerula_reject_v2")
        async def reject(self, interaction, button):
            if str(interaction.user.id) != str(discord_config.get("admin_id", "")):
                await interaction.response.send_message("فقط ادمین.", ephemeral=True)
                return
            t = tickets.get(self.ch_id)
            if not t:
                await interaction.response.send_message("تیکت یافت نشد.", ephemeral=True)
                return
            t["status"] = "rejected"
            ch = _bot.get_channel(self.ch_id)
            if ch:
                await ch.send(embed=discord_module.Embed(
                    title="پرداخت رد شد",
                    description="پرداخت شما تایید نشد. لطفا با ادمین تماس بگیرید.",
                    color=discord_module.Color.red(),
                ))
            await interaction.response.send_message("رد شد", ephemeral=True)
            self.stop()

    _BuyView = BuyView
    _PlanView = PlanView
    _PayView = PayView
    _ApprovalView = ApprovalView


async def start_bot():
    global _bot, _task, discord_config

    try:
        import discord as discord_module
        from discord.ext import commands
    except ImportError:
        logger.error("discord.py not installed!")
        return

    if _bot:
        if _bot.is_closed():
            _bot = None
        elif _bot.is_ready():
            logger.info("Bot already running")
            return
        else:
            logger.info("Bot is starting, please wait")
            return

    token = discord_config.get("bot_token", "").strip()
    if not token:
        logger.warning("No bot_token configured")
        return

    logger.info("Registering views...")
    _register_views(discord_module)

    intents = discord_module.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True

    _bot = commands.Bot(command_prefix="!", intents=intents)

    @_bot.event
    async def on_ready():
        logger.info(f"Bot logged in as {_bot.user} (ID: {_bot.user.id})")
        logger.info(f"Connected to {len(_bot.guilds)} guild(s)")
        for g in _bot.guilds:
            logger.info(f"  Guild: {g.name} (ID: {g.id}) - {len(g.text_channels)} text channels")
        try:
            _bot.add_view(BuyView())
        except Exception as e:
            logger.warning(f"add_view error: {e}")
        try:
            synced = await _bot.tree.sync()
            logger.info(f"Synced {len(synced)} slash commands")
        except Exception as e:
            logger.error(f"Slash sync error: {e}")

    @_bot.tree.command(name="setup", description="ارسال پیام خرید در کانال")
    async def cmd_setup(interaction):
        if str(interaction.user.id) != str(discord_config.get("admin_id", "")):
            await interaction.response.send_message("فقط ادمین.", ephemeral=True)
            return
        await interaction.response.send_message("ارسال شد.", ephemeral=True)
        await interaction.channel.send(embed=make_setup_embed(), view=BuyView())

    @_bot.tree.command(name="status", description="وضعیت ربات")
    async def cmd_status(interaction):
        await interaction.response.send_message(
            f"ربات فعال | تیکت‌ها: {len(tickets)} | سرورها: {len(_bot.guilds)}",
            ephemeral=True,
        )

    @_bot.event
    async def on_message(message):
        if message.author.bot:
            return
        if message.channel.id in tickets:
            t = tickets[message.channel.id]
            if t["status"] == "waiting_receipt" and message.author.id == t["user_id"] and message.attachments:
                att = message.attachments[0]
                try:
                    admin_user = await _bot.fetch_user(int(discord_config.get("admin_id", "0")))
                    embed = discord_module.Embed(
                        title="رسید جدید",
                        description=(
                            f"**کاربر:** {message.author.mention}\n"
                            f"**پلن:** {t['plan_name']}\n"
                            f"**کانال:** {message.channel.mention}"
                        ),
                        color=discord_module.Color.gold(),
                    )
                    await admin_user.send(embed=embed)
                    await admin_user.send(file=await att.to_file())
                    await admin_user.send(view=ApprovalView(message.channel.id, t["user_id"]))
                except Exception as e:
                    logger.error(f"Admin DM error: {e}")
                    await message.channel.send("ارسال رسید به ادمین با خطا مواجه شد. لطفا دایرکت بزنید.")
                    return
                t["status"] = "receipt_sent"
                await message.channel.send(embed=discord_module.Embed(
                    title="رسید دریافت شد",
                    description="رسید شما برای ادمین ارسال شد. منتظر تایید باشید.",
                    color=discord_module.Color.green(),
                ))
        try:
            await _bot.process_commands(message)
        except Exception:
            pass

    try:
        logger.info("Starting bot...")
        _task = asyncio.create_task(_bot.start(token))
        await _task
    except Exception as e:
        logger.error(f"Bot start failed: {e}")
        _bot = None


async def stop_bot():
    global _bot, _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
    if _bot:
        try:
            if not _bot.is_closed():
                await _bot.close()
        except Exception:
            pass
        _bot = None
    logger.info("Bot stopped")
