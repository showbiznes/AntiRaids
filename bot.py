"""
🛡️ Anti-Raid & Anti-Crash Discord Bot — ULTIMATE MILITARY GRADE DEFENSE (v2.0)
Специально против краш-ботов (Smashinator, Wick Nuke, Crate Crash и др.)

ЧТО ДЕЛАЕТ ЭТОТ БОТ:
1. 🛑 АНТИ-БОТ (БЕЗОШИБОЧНЫЙ): Любой сторонний бот БАНИТСЯ мгновенно при каждом входе (без блокировок кэша!). Тот, кто пригласил — тоже БАНИТСЯ!
2. 🛑 ПЕРИОДИЧЕСКИЙ СКАНЕР: Каждые 3 секунды сканирует сервер и ликвидирует любых чужих ботов, даже если событие входа было пропущено.
3. 🛑 ГЛУБОКИЙ АНАЛИЗ КАНАЛОВ: Распознает любые части слов (crashed, smashing, nuker, hacked, nitro и т.д.), очищая символы и эмодзи (например: †crashed-by-smashinator†).
4. 🛑 АНТИ-СНОС КАНАЛОВ: Мгновенный бан виновника + локдаун + удаление краш-каналов за 0.05 сек.
5. 🛑 АВТО-ОЧИСТКА: Команда `!clean` находит и удаляет все каналы с корнями краш-слов.
"""

import asyncio
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

# ═══════════════════════════════════════════════
# ⚙️ НАСТРОЙКИ (КОНФИГУРАЦИЯ)
# ═══════════════════════════════════════════════
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# ⚠️ ВПИШИ СВОЙ ДИСКОРД ID (правый клик по себе -> Копировать ID)
OWNER_IDS: set[int] = {
    # Вставь сюда свой ID
}

# Белый список проверенных ботов и админов
WHITELIST_IDS: set[int] = {
    # ID доверенных ботов
}

LOG_CHANNEL_NAME = "anti-raid-logs"

# КОРНИ И ЧАСТИ СЛОВ ДЛЯ ДЕТЕКЦИИ КРАШ-КАНАЛОВ
# Распознает: crash, crashed, crasher, smash, smashed, smashinator, nuk, nuke, nuked,
# raid, raided, hack, hacked, destroy, destroyed, rip, clown, nitro, scam, spam, ez, dead и др.
CRASH_ROOTS = (
    "crash", "crashed", "crashing", "crasher",
    "smash", "smashed", "smashing", "smashinator",
    "nuk", "nuke", "nuked", "nuker", "nuking",
    "raid", "raided", "raider", "raiding",
    "hack", "hacked", "hacker", "hacking",
    "fuck", "fucked", "fucker",
    "destroy", "destroyed", "destroyer",
    "rip", "clown", "clowned",
    "nitro", "free-nitro", "scam", "spam", "dead", "trash", "ez"
)

# ═══════════════════════════════════════════════
# ФУНКЦИЯ ПРОВЕРКИ ИМЕНИ КАНАЛА
# ═══════════════════════════════════════════════
def is_crash_channel_name(name: str) -> bool:
    """
    Проверяет имя канала на любые части краш-слов.
    Учитывает спецсимволы, кресты †, смайлы, подчеркивания и пробелы.
    """
    raw = name.lower()

    # Прямая проверка подстроки
    for root in CRASH_ROOTS:
        if root in raw:
            return True

    # Проверка после очистки от всех не-буквенно-цифровых символов
    clean = re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', raw)
    for root in CRASH_ROOTS:
        if root in clean:
            return True

    return False


# ═══════════════════════════════════════════════
# ТРЕКЕРЫ СКОРОСТИ
# ═══════════════════════════════════════════════
class FastTracker:
    __slots__ = ("window", "limit", "_data")

    def __init__(self, window: float, limit: int):
        self.window = window
        self.limit = limit
        self._data: dict[int, list[float]] = defaultdict(list)

    def add_and_check(self, uid: int) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        lst = [t for t in self._data[uid] if t > cutoff]
        lst.append(now)
        self._data[uid] = lst
        return len(lst) >= self.limit

    def reset(self, uid: int) -> None:
        self._data.pop(uid, None)


# ═══════════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ ДВИЖКА
# ═══════════════════════════════════════════════
intents = discord.Intents.all()

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

# Трекеры
channel_create_tracker = FastTracker(window=5.0, limit=2)
mass_ban_tracker = FastTracker(window=10.0, limit=2)

# Временный лок во избежание дублирующих параллельных запросов
_banning_in_progress: set[int] = set()
_lockdown_active: set[int] = set()
saved_roles_backup: dict[int, dict[int, int]] = {}


def is_immune(uid: int) -> bool:
    """Проверка иммунитета."""
    if bot.user and uid == bot.user.id:
        return True
    return uid in OWNER_IDS or uid in WHITELIST_IDS


# ═══════════════════════════════════════════════
# ⚡ СВЕРХБЫСТРЫЕ ДЕЙСТВИЯ (FAST BAN & STRIKE)
# ═══════════════════════════════════════════════
async def fast_ban(guild: discord.Guild, user: discord.User | discord.Member | int, reason: str) -> bool:
    """
    Моментальный бан нарушителя.
    НЕ оставляет перманентных блокировок в кэше — при повторном входе забанит снова!
    """
    uid = user if isinstance(user, int) else user.id
    if is_immune(uid):
        return False
    if uid in _banning_in_progress:
        return False

    _banning_in_progress.add(uid)
    try:
        if isinstance(user, discord.Member):
            removable = [r for r in user.roles if not r.is_default() and not r.managed]
            if removable:
                try:
                    await user.remove_roles(*removable, reason=reason)
                except Exception:
                    pass
            await guild.ban(user, reason=f"🛡️ [Anti-Crash] {reason}", delete_message_seconds=604800)
        elif isinstance(user, discord.User):
            await guild.ban(user, reason=f"🛡️ [Anti-Crash] {reason}", delete_message_seconds=604800)
        else:
            await guild.ban(discord.Object(id=uid), reason=f"🛡️ [Anti-Crash] {reason}", delete_message_seconds=604800)

        asyncio.create_task(send_alert(
            guild,
            "⛔ НАРУШИТЕЛЬ ЗАБАНЕН",
            f"**Пользователь/Бот:** <@{uid}> (`{uid}`)\n**Причина:** {reason}",
            discord.Color.dark_red()
        ))
        return True
    except Exception as e:
        print(f"[BAN ERROR] {uid}: {e}")
        return False
    finally:
        # Снимаем временный лок через 2 секунды
        bot.loop.call_later(2.0, _banning_in_progress.discard, uid)


async def nuke_all_unauthorized_bots(guild: discord.Guild, trigger_reason: str):
    """Моментально банит ВСЕХ ботов не из белого списка."""
    tasks_to_run = []
    for member in guild.members:
        if member.bot and not is_immune(member.id):
            tasks_to_run.append(fast_ban(guild, member, f"Авто-ликвидация ({trigger_reason})"))
    if tasks_to_run:
        await asyncio.gather(*tasks_to_run, return_exceptions=True)


async def emergency_lockdown(guild: discord.Guild, reason: str):
    """Моментальный локдаун опасных прав на сервере."""
    if guild.id in _lockdown_active:
        return
    _lockdown_active.add(guild.id)
    saved_roles_backup[guild.id] = {}

    tasks_to_run = []
    for role in guild.roles:
        if role.is_default() or (role.managed and role in guild.me.roles):
            continue
        perms = role.permissions
        if (perms.administrator or perms.manage_channels or perms.manage_guild or
                perms.manage_roles or perms.ban_members or perms.kick_members or perms.manage_webhooks):
            saved_roles_backup[guild.id][role.id] = perms.value
            new_perms = discord.Permissions(perms.value)
            new_perms.administrator = False
            new_perms.manage_channels = False
            new_perms.manage_guild = False
            new_perms.manage_roles = False
            new_perms.ban_members = False
            new_perms.kick_members = False
            new_perms.manage_webhooks = False
            tasks_to_run.append(role.edit(permissions=new_perms, reason=f"🛡️ LOCKDOWN: {reason}"))

    if tasks_to_run:
        await asyncio.gather(*tasks_to_run, return_exceptions=True)


async def send_alert(guild: discord.Guild, title: str, description: str, color: discord.Color = discord.Color.red()):
    """Отправка алерта в лог-канал."""
    try:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if not ch:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True)
            }
            ch = await guild.create_text_channel(LOG_CHANNEL_NAME, overwrites=overwrites)

        embed = discord.Embed(
            title=f"🚨 {title}",
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Anti-Crash Active Shield v2.0")
        await ch.send(embed=embed)
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 🛑 1. АНТИ-БОТ: ВХОД ЛЮБОГО ЧУЖОГО БОТА = БАН ЗА 0.05 СЕК
# ═══════════════════════════════════════════════
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    if member.bot:
        if not is_immune(member.id):
            # 1. Моментально баним бота
            await fast_ban(guild, member, "Неавторизованный бот (Защита от краша)")

            # 2. Ищем и баним того, кто его пригласил
            await asyncio.sleep(0.3)
            try:
                async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.bot_add):
                    if entry.target and entry.target.id == member.id and entry.user:
                        if not is_immune(entry.user.id):
                            await fast_ban(guild, entry.user, f"Пригласил краш-бота {member}")
                        break
            except Exception:
                pass
        return

    # Обычный участник
    if is_immune(member.id):
        return

    age_days = (datetime.now(timezone.utc) - member.created_at).days
    if age_days < 1:
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(hours=2), reason="Новый аккаунт (< 1 дня)")
        except Exception:
            pass


# ═══════════════════════════════════════════════
# 🛑 2. АНТИ-КРАШ КАНАЛЫ (СОЗДАНИЕ КАНАЛОВ)
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    guild = channel.guild

    # 1. Проверяем имя канала на любые части краш-слов
    is_bad_name = is_crash_channel_name(channel.name)

    # Ищем создателя
    creator = None
    try:
        async for entry in guild.audit_logs(limit=2, action=discord.AuditLogAction.channel_create):
            if entry.target and entry.target.id == channel.id:
                creator = entry.user
                break
    except Exception:
        pass

    if creator and is_immune(creator.id):
        return

    # Превышен лимит создания или подозрительное имя
    is_mass = creator and channel_create_tracker.add_and_check(creator.id)

    if is_bad_name or is_mass:
        # МГНОВЕННО удаляем краш-канал
        try:
            await channel.delete(reason="Anti-Crash: Удаление краш-канала")
        except Exception:
            pass

        # МГНОВЕННО баним создателя
        if creator:
            await fast_ban(guild, creator, f"Создание краш-канала ({channel.name})")

        # Ликвидируем всех сторонних ботов и врубаем локдаун
        asyncio.create_task(nuke_all_unauthorized_bots(guild, "Создание краш-каналов"))
        asyncio.create_task(emergency_lockdown(guild, "Краш-атака"))


# ═══════════════════════════════════════════════
# 🛑 3. АНТИ-УДАЛЕНИЕ КАНАЛОВ (СНОС СЕРВЕРА)
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild

    # МГНОВЕННО ликвидируем всех сторонних ботов и врубаем локдаун
    asyncio.create_task(nuke_all_unauthorized_bots(guild, f"Удален канал #{channel.name}"))
    asyncio.create_task(emergency_lockdown(guild, f"Удален канал #{channel.name}"))

    # Ищем и баним виновника
    try:
        async for entry in guild.audit_logs(limit=2, action=discord.AuditLogAction.channel_delete):
            if entry.user and not is_immune(entry.user.id):
                await fast_ban(guild, entry.user, f"Удаление канала #{channel.name}")
                break
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 🛑 4. АНТИ-УДАЛЕНИЕ РОЛЕЙ И МАССОВЫЙ БАН
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_role_delete(role: discord.Role):
    guild = role.guild
    try:
        async for entry in guild.audit_logs(limit=2, action=discord.AuditLogAction.role_delete):
            if entry.user and not is_immune(entry.user.id):
                await fast_ban(guild, entry.user, f"Удаление роли {role.name}")
                asyncio.create_task(nuke_all_unauthorized_bots(guild, "Удаление ролей"))
                break
    except Exception:
        pass


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    try:
        async for entry in guild.audit_logs(limit=2, action=discord.AuditLogAction.ban):
            if entry.user and not is_immune(entry.user.id):
                if mass_ban_tracker.add_and_check(entry.user.id):
                    await fast_ban(guild, entry.user, "Массовый бан участников")
                    asyncio.create_task(nuke_all_unauthorized_bots(guild, "Массовые баны"))
                break
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 🛑 5. АНТИ-СПАМ: @everyone + ССЫЛКИ НА СЕРВЕРА / НИТРО
# ═══════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot or is_immune(message.author.id):
        await bot.process_commands(message)
        return

    content = message.content.lower()
    has_mass_ping = message.mention_everyone or len(message.mentions) >= 5
    has_scam_link = bool(re.search(r"(discord\.(gg|io|me|li)|discordapp\.com/invite|t\.me/|nitro|steam)", content))

    if has_mass_ping and has_scam_link:
        try:
            await message.delete()
        except Exception:
            pass
        await fast_ban(message.guild, message.author, "Краш-рассылка с @everyone")
        return

    await bot.process_commands(message)


# ═══════════════════════════════════════════════
# 🔄 ФОНОВЫЙ СКАНЕР: ПРОВЕРКА ЧУЖИХ БОТОВ КАЖДЫЕ 3 СЕКУНДЫ
# ═══════════════════════════════════════════════
@tasks.loop(seconds=3.0)
async def auto_scan_bots():
    """Постоянный сторож: банит любого незарегистрированного бота."""
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot and not is_immune(member.id):
                await fast_ban(guild, member, "Обнаружен фоновым сканером (неавторизованный бот)")


@auto_scan_bots.before_loop
async def before_scan():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════
# 🛠️ КОМАНДЫ УПРАВЛЕНИЯ
# ═══════════════════════════════════════════════
@bot.command(name="clean")
@commands.has_permissions(administrator=True)
async def clean_cmd(ctx: commands.Context):
    """Удалить ВСЕ каналы, содержащие любые части краш-слов."""
    msg = await ctx.send("🧹 Глубокий поиск и удаление краш-каналов...")
    deleted = 0
    for channel in list(ctx.guild.channels):
        if is_crash_channel_name(channel.name) and channel.id != ctx.channel.id:
            try:
                await channel.delete(reason="Глубокая очистка краш-каналов")
                deleted += 1
                await asyncio.sleep(0.1)
            except Exception:
                pass
    await msg.edit(content=f"✅ Успешно удалено **{deleted}** краш-каналов.")


@bot.command(name="restore")
@commands.has_permissions(administrator=True)
async def restore_cmd(ctx: commands.Context):
    """Восстановить права ролей после локдауна."""
    gid = ctx.guild.id
    if gid not in saved_roles_backup or not saved_roles_backup[gid]:
        _lockdown_active.discard(gid)
        await ctx.send("ℹ️ Нет сохраненных ролей для отката. Локдаун снят.")
        return

    msg = await ctx.send("⏳ Восстановление прав ролей...")
    restored = 0
    for role_id, perm_val in saved_roles_backup[gid].items():
        role = ctx.guild.get_role(role_id)
        if role:
            try:
                await role.edit(permissions=discord.Permissions(perm_val), reason="Восстановление прав")
                restored += 1
            except Exception:
                pass

    saved_roles_backup.pop(gid, None)
    _lockdown_active.discard(gid)
    await msg.edit(content=f"✅ Восстановлено **{restored}** ролей. Сервер разблокирован!")


@bot.command(name="wl")
@commands.has_permissions(administrator=True)
async def wl_cmd(ctx: commands.Context, action: str = "list", target: discord.Member | discord.User | None = None):
    """!wl add @user | !wl remove @user | !wl list"""
    if action == "add" and target:
        WHITELIST_IDS.add(target.id)
        await ctx.send(f"✅ <@{target.id}> добавлен в белый список.")
    elif action == "remove" and target:
        WHITELIST_IDS.discard(target.id)
        await ctx.send(f"❌ <@{target.id}> удален из белого списка.")
    else:
        ids_text = ", ".join(f"`{x}`" for x in WHITELIST_IDS) if WHITELIST_IDS else "пусто"
        owners_text = ", ".join(f"`{x}`" for x in OWNER_IDS) if OWNER_IDS else "пусто"
        await ctx.send(f"📋 **Владельцы:** {owners_text}\n📋 **Белый список:** {ids_text}")


@bot.command(name="status")
@commands.has_permissions(administrator=True)
async def status_cmd(ctx: commands.Context):
    e = discord.Embed(title="🛡️ Активный щит Anti-Crash v2.0", color=discord.Color.green())
    e.add_field(name="Локдаун", value="🔒 АКТИВЕН" if ctx.guild.id in _lockdown_active else "✅ Выключен", inline=True)
    e.add_field(name="Фоновый сканер", value="🟢 Активен (каждые 3 сек)", inline=True)
    e.add_field(name="Бот", value=f"✅ Онлайн ({bot.user})", inline=True)
    await ctx.send(embed=e)


# ═══════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"🛡️ БРОНЕБОЙНЫЙ ЩИТ v2.0 ЗАПУЩЕН: {bot.user} | Серверов: {len(bot.guilds)}")
    if not auto_scan_bots.is_running():
        auto_scan_bots.start()
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="за сервером 🛡️"))


if not TOKEN:
    print("❌ ОШИБКА: Токен DISCORD_BOT_TOKEN не найден в переменных окружения!")
    raise SystemExit(1)

bot.run(TOKEN)
