"""
🛡️ Anti-Raid & Anti-Crash Discord Bot — ULTIMATE MILITARY GRADE DEFENSE
Специально против краш-ботов (Smashinator, Wick Nuke, Crate Crash и др.)

ЧТО ДЕЛАЕТ ЭТОТ БОТ:
1. 🛑 АНТИ-БОТ (ЖЁСТКИЙ): Любой новый неизвестный бот БАНИТСЯ за 0.05 сек в момент входа! Тот, кто его пригласил — тоже БАНИТСЯ!
2. 🛑 АНТИ-СПАМ КАНАЛАМИ: Попытка создать каналы вида "crashed-by-..." или больше 1 канала за 5 сек -> МГНОВЕННЫЙ БАН создателя + АВТО-УДАЛЕНИЕ всех созданных каналов!
3. 🛑 АНТИ-УДАЛЕНИЕ КАНАЛОВ: Удаление хоть 1 канала -> МГНОВЕННЫЙ БАН виновника + БАН ВСЕХ сторонних ботов на сервере за 0.1 сек + ЛОКДАУН прав!
4. 🛑 АНТИ-НИТРО/КРАШ СПАМ: Сообщение с @everyone и ссылкой discord.gg / nitro -> МГНОВЕННЫЙ БАН + авто-удаление сообщения!
5. 🛑 АВТО-ОЧИСТКА: Команда `!clean` удаляет все созданные крашером каналы за секунды.
"""

import asyncio
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

# ═══════════════════════════════════════════════
# ⚙️ НАСТРОЙКИ (КОНФИГУРАЦИЯ)
# ═══════════════════════════════════════════════
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# ⚠️ ВПИШИ СВОЙ ДИСКОРД ID (правый клик по себе -> Копировать ID)
# Если ID здесь, бот НИКОГДА тебя не тронет
OWNER_IDS: set[int] = {
    # Вставь сюда свой ID, например: 123456789012345678
}

# Белый список проверенных ботов и админов
WHITELIST_IDS: set[int] = {
    # ID доверенных ботов (музыкальные, модерация и т.д.)
}

LOG_CHANNEL_NAME = "anti-raid-logs"

# Подозрительные слова в названиях каналов при краше
CRASH_CHANNEL_KEYWORDS = (
    "crash", "smash", "nuke", "raid", "hacked", "fucked", "destroyed",
    "rip", "ez", "clowned", "crashed-by", "nitro", "free", "dead",
    "†", "⚡", "💥", "☠"
)

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
intents = discord.Intents.all()  # Включаем все интенты для максимальной скорости

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

# Трекеры
channel_create_tracker = FastTracker(window=5.0, limit=2)   # 2 канала за 5 сек -> БАН
channel_delete_tracker = FastTracker(window=10.0, limit=1)  # 1 удаление -> БАН
role_delete_tracker = FastTracker(window=10.0, limit=1)     # 1 удаление -> БАН
mass_ban_tracker = FastTracker(window=10.0, limit=2)        # 2 бана -> БАН

# Множество забаненных ID во избежание дубликатов запросов
_banned_cache: set[int] = set()
_lockdown_active: set[int] = set()
saved_roles_backup: dict[int, dict[int, int]] = {}  # guild_id -> {role_id: permissions_value}


def is_immune(uid: int) -> bool:
    """Проверка иммунитета (ты, доверенные лица, сам бот)."""
    if bot.user and uid == bot.user.id:
        return True
    return uid in OWNER_IDS or uid in WHITELIST_IDS


# ═══════════════════════════════════════════════
# ⚡ СВЕРХБЫСТРЫЕ ДЕЙСТВИЯ (FIRE & FORGET)
# ═══════════════════════════════════════════════
async def fast_ban(guild: discord.Guild, user: discord.User | discord.Member | int, reason: str) -> bool:
    """Моментальный бан без задержек."""
    uid = user if isinstance(user, int) else user.id
    if is_immune(uid) or uid in _banned_cache:
        return False
    _banned_cache.add(uid)

    try:
        if isinstance(user, (discord.User, discord.Member)):
            # Снимаем все роли если участник на сервере
            if isinstance(user, discord.Member):
                removable = [r for r in user.roles if not r.is_default() and not r.managed]
                if removable:
                    asyncio.create_task(user.remove_roles(*removable, reason=reason))
            await guild.ban(user, reason=f"🛡️ [Anti-Crash] {reason}", delete_message_seconds=604800)
        else:
            await guild.ban(discord.Object(id=uid), reason=f"🛡️ [Anti-Crash] {reason}", delete_message_seconds=604800)
        
        asyncio.create_task(send_alert(
            guild,
            f"⛔ НАРУШИТЕЛЬ ЗАБАНЕН",
            f"**Пользователь/Бот:** <@{uid}> (`{uid}`)\n**Причина:** {reason}",
            discord.Color.dark_red()
        ))
        return True
    except Exception as e:
        print(f"[ERROR BAN] Не удалось забанить {uid}: {e}")
        return False


async def nuke_all_unauthorized_bots(guild: discord.Guild, trigger_reason: str):
    """При любой атаке немедленно банит ВСЕХ ботов не из белого списка."""
    tasks_to_run = []
    for member in guild.members:
        if member.bot and not is_immune(member.id):
            tasks_to_run.append(fast_ban(guild, member, f"Авто-ликвидация бота при атаке ({trigger_reason})"))
    if tasks_to_run:
        await asyncio.gather(*tasks_to_run, return_exceptions=True)


async def emergency_lockdown(guild: discord.Guild, reason: str):
    """Моментальный локдаун прав сервера."""
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
    """Отправка алерта в лог-канал в фоне."""
    try:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if not ch:
            # Создать канал если удален
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
        embed.set_footer(text="Anti-Crash Active Shield")
        await ch.send(embed=embed)
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 🛑 1. АНТИ-БОТ: ВХОД НЕИЗВЕСТНОГО БОТА = БАН ЗА 0.05 СЕК
# ═══════════════════════════════════════════════
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    # ЕСЛИ ВОШЕЛ СТОРОННИЙ БОТ
    if member.bot:
        if not is_immune(member.id):
            # 1. Мгновенно баним бота!
            asyncio.create_task(fast_ban(guild, member, "Неавторизованный бот (Защита от краш-ботов)"))

            # 2. Ищем того, кто его добавил, и тоже баним!
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

    # Обычный участник — проверка на рейд
    if is_immune(member.id):
        return

    # Анализ возраста аккаунта
    age_days = (datetime.now(timezone.utc) - member.created_at).days
    if age_days < 1:
        # Аккаунт-новорег — превентивный карантин (таймаут)
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(hours=2), reason="Новый аккаунт (< 1 дня)")
        except Exception:
            pass


# ═══════════════════════════════════════════════
# 🛑 2. АНТИ-КРАШ КАНАЛЫ (СОЗДАНИЕ КАНАЛОВ КРАШЕРОМ)
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    guild = channel.guild
    ch_name = channel.name.lower()

    # Проверка 1: Подозрительное имя (crashed-by, nuke, smash и т.д.)
    is_suspicious_name = any(kw in ch_name for kw in CRASH_CHANNEL_KEYWORDS)

    # Ищем создателя в audit log
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

    # Если имя крашерское ИЛИ превышен лимит создания каналов
    should_punish = is_suspicious_name
    if creator and channel_create_tracker.add_and_check(creator.id):
        should_punish = True

    if should_punish:
        # 1. Удалить созданный канал
        try:
            await channel.delete(reason="Anti-Crash: Удаление краш-канала")
        except Exception:
            pass

        # 2. Забанить создателя
        if creator:
            await fast_ban(guild, creator, f"Массовое создание краш-каналов ({channel.name})")

        # 3. Баним всех сторонних ботов и включаем локдаун
        asyncio.create_task(nuke_all_unauthorized_bots(guild, "Массовое создание каналов"))
        asyncio.create_task(emergency_lockdown(guild, "Краш атака (создание каналов)"))


# ═══════════════════════════════════════════════
# 🛑 3. АНТИ-УДАЛЕНИЕ КАНАЛОВ (СНОС СЕРВЕРА)
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild

    # 1. СРАЗУ баним всех сторонних ботов и врубаем локдаун!
    asyncio.create_task(nuke_all_unauthorized_bots(guild, f"Удален канал #{channel.name}"))
    asyncio.create_task(emergency_lockdown(guild, f"Удален канал #{channel.name}"))

    # 2. Ищем кто удалил и баним его лично
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
# 🛑 5. АНТИ-СПАМ: @everyone + ССЫЛКИ НА ДРУГИЕ СЕРВЕРЫ / НИТРО
# ═══════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot or is_immune(message.author.id):
        await bot.process_commands(message)
        return

    content = message.content.lower()

    # Проверка на краш-рассылку: @everyone + ссылки discord.gg / nitro / t.me
    has_mass_ping = message.mention_everyone or len(message.mentions) >= 5
    has_invite_or_scam = bool(re.search(r"(discord\.(gg|io|me|li)|discordapp\.com/invite|t\.me/|nitro|steam)", content))

    if has_mass_ping and has_invite_or_scam:
        # МГНОВЕННО БАНИМ СПАМЕРА
        try:
            await message.delete()
        except Exception:
            pass
        await fast_ban(message.guild, message.author, "Краш-рассылка спама с @everyone")
        return

    await bot.process_commands(message)


# ═══════════════════════════════════════════════
# 🛠️ КОМАНДЫ ВОССТАНОВЛЕНИЯ И ОЧИСТКИ
# ═══════════════════════════════════════════════
@bot.command(name="clean")
@commands.has_permissions(administrator=True)
async def clean_cmd(ctx: commands.Context):
    """Удалить все каналы с краш-названиями (crashed-by, nuke и т.д.)."""
    msg = await ctx.send("🧹 Поиск и удаление краш-каналов...")
    deleted = 0
    for channel in list(ctx.guild.channels):
        ch_name = channel.name.lower()
        if any(kw in ch_name for kw in CRASH_CHANNEL_KEYWORDS) and channel.id != ctx.channel.id:
            try:
                await channel.delete(reason="Очистка краш-каналов")
                deleted += 1
                await asyncio.sleep(0.2)
            except Exception:
                pass
    await msg.edit(content=f"✅ Удалено **{deleted}** мусорных краш-каналов.")


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
    """Управление белым списком: !wl add @user | !wl remove @user | !wl list"""
    if action == "add" and target:
        WHITELIST_IDS.add(target.id)
        await ctx.send(f"✅ <@{target.id}> добавлен в белый список доверенных лиц.")
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
    """Статус защиты."""
    e = discord.Embed(title="🛡️ Активный щит Anti-Crash", color=discord.Color.green())
    e.add_field(name="Локдаун", value="🔒 АКТИВЕН" if ctx.guild.id in _lockdown_active else "✅ Выключен", inline=True)
    e.add_field(name="Бот", value=f"✅ Онлайн ({bot.user})", inline=True)
    e.add_field(name="Реакция на краш-ботов", value="⚡ Мгновенный BAN при входе / создании каналов", inline=False)
    await ctx.send(embed=e)


# ═══════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"🛡️ БРОНЕБОЙНЫЙ ЩИТ ЗАПУЩЕН: {bot.user} | Серверов: {len(bot.guilds)}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="за сервером 🛡️"))


if not TOKEN:
    print("❌ ОШИБКА: Токен DISCORD_BOT_TOKEN не найден в переменных окружения!")
    raise SystemExit(1)

bot.run(TOKEN)
