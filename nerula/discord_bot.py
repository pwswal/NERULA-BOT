import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("NERULA_DISCORD")

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
CONFIG_FILE = DATA_DIR / "discord.json"

CARD_NUMBER = "6104-3387-5956-2107"
CARD_HOLDER = "آهورا ارپناهی"
PLANS = {
    "10gb": {"label": "10 گیگابایت", "price": "100,000 تومان", "bytes": 10 * 1024**3},
    "20gb": {"label": "20 گیگابایت", "price": "150,000 تومان", "bytes": 20 * 1024**3},
    "unlimited": {"label": "نامحدود", "price": "250,000 تومان", "bytes": 0},
}

discord_config: dict = {"bot_token": "", "admin_id": "", "channel_id": ""}
tickets: dict = {}
_bot = None
_task = None
_discord = None

def load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception as e:
        logger.warning(f"Load config: {e}")
    return {"bot_token": "", "admin_id": "", "channel_id": ""}

def save_config():
    try:
        CONFIG_FILE.write_text(json.dumps(discord_config, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        logger.warning(f"Save config: {e}")

def is_ready() -> bool:
    return _bot is not None and _bot.is_ready()

def get_bot_user_id() -> str | None:
    if _bot and _bot.is_ready() and _bot.user:
        return str(_bot.user.id)
    return None

def get_guild_channels() -> list:
    if not _bot or not _bot.is_ready():
        return []
    result = []
    for guild in _bot.guilds:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                result.append({"id": str(ch.id), "name": f"#{ch.name} ({guild.name})"})
    return result

def get_channel(channel_id):
    if not _bot or not _bot.is_ready():
        return None
    try:
        return _bot.get_channel(int(channel_id))
    except:
        return None

def make_setup_embed():
    embed = _discord.Embed(
        title="🛒 خرید کانفیگ VPN از NERULA",
        description="برای خرید کانفیگ، روی دکمه زیر کلیک کنید.",
        color=_discord.Color.green(),
    )
    embed.add_field(
        name="📦 تعرفه‌ها",
        value=(
            "• **10 گیگابایت** — 100,000 تومان\n"
            "• **20 گیگابایت** — 150,000 تومان\n"
            "• **نامحدود** — 250,000 تومان"
        ),
        inline=False,
    )
    embed.set_footer(text="NERULA VPN Panel")
    return embed

def make_buy_view():
    return BuyView()

async def _show_payment(interaction, plan_key: str):
    plan = PLANS[plan_key]
    embed = _discord.Embed(
        title="💳 اطلاعات پرداخت",
        description=(
            f"**پلن انتخابی:** {plan['label']}\n"
            f"**مبلغ:** {plan['price']}\n\n"
            f"**شماره کارت:**\n`{CARD_NUMBER}`\n\n"
            f"**به نام:** {CARD_HOLDER}"
        ),
        color=_discord.Color.gold(),
    )
    embed.set_footer(text="بعد از پرداخت، روی دکمه «پرداخت شد» کلیک کنید")
    await interaction.response.send_message(embed=embed, view=PayView(plan_key), ephemeral=True)


def _register_views(discord_module):
    global _discord, BuyView, PlanView, PayView, ApprovalView
    _discord = discord_module

    class BuyView(discord_module.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord_module.ui.button(label="🛒 خرید کانفیگ", style=discord_module.ButtonStyle.success, custom_id="nerula_buy")
        async def buy(self, interaction, button):
            embed = discord_module.Embed(
                title="📦 انتخاب پلن",
                description="پلن مورد نظر خود را انتخاب کنید:",
                color=discord_module.Color.blue(),
            )
            await interaction.response.send_message(embed=embed, view=PlanView(), ephemeral=True)

    class PlanView(discord_module.ui.View):
        def __init__(self):
            super().__init__(timeout=120)

        @discord_module.ui.button(label="10 گیگ — 100K", style=discord_module.ButtonStyle.primary, custom_id="plan_10gb")
        async def p10(self, interaction, button):
            await _show_payment(interaction, "10gb")

        @discord_module.ui.button(label="20 گیگ — 150K", style=discord_module.ButtonStyle.primary, custom_id="plan_20gb")
        async def p20(self, interaction, button):
            await _show_payment(interaction, "20gb")

        @discord_module.ui.button(label="نامحدود — 250K", style=discord_module.ButtonStyle.primary, custom_id="plan_unlimited")
        async def punl(self, interaction, button):
            await _show_payment(interaction, "unlimited")

    class PayView(discord_module.ui.View):
        def __init__(self, plan_key: str):
            super().__init__(timeout=300)
            self.plan_key = plan_key

        @discord_module.ui.button(label="✅ پرداخت شد", style=discord_module.ButtonStyle.success, custom_id="nerula_paid")
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
                admin_user = await _bot.fetch_user(int(discord_config["admin_id"]))
                overwrites[admin_user] = discord_module.PermissionOverwrite(view_channel=True, send_messages=True)
            except:
                pass
            ch = await guild.create_text_channel(
                name=f"ticket-{user.name}", overwrites=overwrites,
                reason=f"VPN ticket for {user.name}",
            )
            tickets[ch.id] = {
                "user_id": user.id,
                "plan_key": self.plan_key,
                "plan_name": plan["label"],
                "status": "waiting_receipt",
                "created_at": datetime.now().isoformat(),
            }
            embed = discord_module.Embed(
                title="📩 تیکت خرید کانفیگ",
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

        @discord_module.ui.button(label="❌ لغو", style=discord_module.ButtonStyle.danger, custom_id="nerula_cancel")
        async def cancel(self, interaction, button):
            await interaction.response.send_message("لغو شد.", ephemeral=True)
            self.stop()

    class ApprovalView(discord_module.ui.View):
        def __init__(self, ch_id: int, user_id: int):
            super().__init__(timeout=None)
            self.ch_id = ch_id
            self.user_id = user_id

        @discord_module.ui.button(label="✅ تایید", style=discord_module.ButtonStyle.success, custom_id="nerula_approve")
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
                    title="✅ پرداخت تایید شد",
                    description="کانفیگ شما به‌زودی ارسال خواهد شد.",
                    color=discord_module.Color.green(),
                ))
                try:
                    u = await _bot.fetch_user(self.user_id)
                    await u.send(embed=discord_module.Embed(
                        title="✅ سفارش تایید شد",
                        description=f"پلن: {t['plan_name']}\nکانفیگ شما آماده ارسال است.",
                        color=discord_module.Color.green(),
                    ))
                except:
                    pass
            await interaction.response.send_message("✅ تایید شد", ephemeral=True)
            self.stop()

        @discord_module.ui.button(label="❌ رد", style=discord_module.ButtonStyle.danger, custom_id="nerula_reject")
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
                    title="❌ پرداخت رد شد",
                    description="پرداخت شما تایید نشد. لطفا با ادمین تماس بگیرید.",
                    color=discord_module.Color.red(),
                ))
            await interaction.response.send_message("❌ رد شد", ephemeral=True)
            self.stop()


async def start_bot():
    global _bot, _task
    try:
        import discord as discord_module
        from discord.ext import commands
    except ImportError:
        logger.warning("discord.py not installed, skipping bot")
        return

    if _bot and _bot.is_ready():
        return

    discord_config.update(load_config())
    token = discord_config.get("bot_token", "")
    if not token:
        logger.info("No Discord token configured")
        return

    _register_views(discord_module)

    intents = discord_module.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True

    _bot = commands.Bot(command_prefix="!", intents=intents)

    @_bot.event
    async def on_ready():
        logger.info(f"Discord bot ready: {_bot.user} (ID: {_bot.user.id})")
        try:
            _bot.add_view(BuyView())
        except:
            pass
        try:
            await _bot.tree.sync()
            logger.info("Slash commands synced")
        except Exception as e:
            logger.warning(f"Sync error: {e}")

    @_bot.tree.command(name="setup", description="ارسال پیام خرید در کانال")
    async def cmd_setup(interaction):
        if str(interaction.user.id) != str(discord_config.get("admin_id", "")):
            await interaction.response.send_message("فقط ادمین.", ephemeral=True)
            return
        ch = interaction.channel
        await ch.send(embed=make_setup_embed(), view=BuyView())
        await interaction.response.send_message("✅ ارسال شد.", ephemeral=True)

    @_bot.tree.command(name="status", description="وضعیت ربات")
    async def cmd_status(interaction):
        await interaction.response.send_message(
            f"✅ ربات فعال است\nتیکت‌ها: {len(tickets)}", ephemeral=True,
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
                    admin_user = await _bot.fetch_user(int(discord_config["admin_id"]))
                    embed = discord_module.Embed(
                        title="📩 رسید جدید",
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
                    await message.channel.send("⚠️ ارسال رسید به ادمین با خطا مواجه شد.")
                    return
                t["status"] = "receipt_sent"
                await message.channel.send(embed=discord_module.Embed(
                    title="✅ رسید دریافت شد",
                    description="رسید شما برای ادمین ارسال شد. منتظر تایید باشید.",
                    color=discord_module.Color.green(),
                ))
        try:
            await _bot.process_commands(message)
        except:
            pass

    _task = asyncio.create_task(_start(token))


async def _start(token):
    global _bot
    try:
        await _bot.start(token)
    except Exception as e:
        logger.error(f"Discord start error: {e}")
        _bot = None


async def stop_bot():
    global _bot, _task
    if _bot:
        try:
            await _bot.close()
        except:
            pass
        _bot = None
    if _task:
        _task = None
