"""
🛡️ Anti-Raid Discord Bot — МАКСИМАЛЬНАЯ СКОРОСТЬ
Один файл. Оптимизирован для bothost.ru.

ПРИНЦИП: сначала БАНИТЬ/СНИМАТЬ РОЛИ, потом логировать.
Удалил 1 канал = моментальный бан. Без предупреждений.
"""

import asyncio
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

# ═══════════════════════════════════════════════
# НАСТРОЙКИ
# ═══════════════════════════════════════════════
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# Твой Discord ID — ОБЯЗАТЕЛЬНО ВПИШИ, иначе бот забанит и тебя
OWNER_IDS: set[int] = set()       # {123456789}
WHITELIST_IDS: set[int] = set()

LOG_CHANNEL_NAME = "anti-raid-logs"

# Anti-Nuke: лимиты (за 60 секунд)
CHANNEL_DELETE_LIMIT = 1    # 1 канал = мгновенный бан
ROLE_DELETE_LIMIT = 2       # ролей → бан
MASS_BAN_LIMIT = 3          # банов → бан
MASS_KICK_LIMIT = 5         # киков → бан

# Anti-Raid
JOIN_FLOOD_LIMIT = 5
JOIN_FLOOD_WINDOW = 10
SUSPICIOUS_AGE_DAYS = 7
RAID_MODE_DURATION = 300

# Anti-Spam
MSG_LIMIT = 10
MSG_WINDOW = 5
DUP_LIMIT = 5
DUP_WINDOW = 10
MENTION_LIMIT = 8
MUTE_SECONDS = 300


# ═══════════════════════════════════════════════
# ТРЕКЕР
# ═══════════════════════════════════════════════
class Tracker:
    __slots__ = ("window", "limit", "_data", "_last_clean")

    def __init__(self, window: float, limit: int):
        self.window = window
        self.limit = limit
        self._data: dict[int, list[float]] = defaultdict(list)
        self._last_clean = time.monotonic()

    def record(self, uid: int) -> int:
        now = time.monotonic()
        if now - self._last_clean > 120:
            self._cleanup(now)
        lst = self._data[uid]
        lst.append(now)
        cutoff = now - self.window
        self._data[uid] = [t for t in lst if t > cutoff]
        return len(self._data[uid])

    def over(self, uid: int) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        return sum(1 for t in self._data.get(uid, []) if t > cutoff) >= self.limit

    def reset(self, uid: int) -> None:
        self._data.pop(uid, None)

    def _cleanup(self, now: float) -> None:
        self._last_clean = now
        cutoff = now - self.window
        empty = [u for u, ts in self._data.items() if not any(t > cutoff for t in ts)]
        for u in empty:
            del self._data[u]


# ═══════════════════════════════════════════════
# БОТ
# ═══════════════════════════════════════════════
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Трекеры
role_del = Tracker(60, ROLE_DELETE_LIMIT)
mass_ban = Tracker(60, MASS_BAN_LIMIT)
mass_kick = Tracker(60, MASS_KICK_LIMIT)
msg_flood = Tracker(MSG_WINDOW, MSG_LIMIT)

# Рейд
raid_mode: dict[int, float] = {}
join_times: deque[float] = deque(maxlen=50)
dup_cache: dict[int, deque[tuple[float, str]]] = defaultdict(lambda: deque(maxlen=15))

# Уже наказанные — чтобы не обрабатывать дважды
_punished: set[int] = set()
# Блокировка — пока обрабатываем одного нарушителя, не начинать заново
_processing: set[int] = set()

# Кэш audit log — не запрашивать повторно за 5 сек
_audit_cache: dict[tuple[int, str], tuple[float, discord.User | None]] = {}


# ═══════════════════════════════════════════════
# ХЕЛПЕРЫ — СКОРОСТЬ ПРЕВЫШЕ ВСЕГО
# ═══════════════════════════════════════════════
def is_safe(user_id: int) -> bool:
    return user_id == (bot.user and bot.user.id) or user_id in OWNER_IDS or user_id in WHITELIST_IDS


async def audit_user(guild: discord.Guild,
                     action: discord.AuditLogAction) -> discord.User | None:
    """Быстрый audit log с кэшированием."""
    now = time.monotonic()
    key = (guild.id, action.name)

    # Проверить кэш (5 секунд)
    if key in _audit_cache:
        cached_time, cached_user = _audit_cache[key]
        if now - cached_time < 5:
            return cached_user

    dt_now = datetime.now(timezone.utc)
    user = None
    try:
        async for entry in guild.audit_logs(limit=1, action=action):
            if entry.created_at and (dt_now - entry.created_at).total_seconds() < 15:
                user = entry.user
                break
    except discord.Forbidden:
        pass

    _audit_cache[key] = (now, user)
    return user


async def log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if ch:
        return ch
    try:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(
                read_messages=True, send_messages=True, embed_links=True
            ),
        }
        return await guild.create_text_channel(
            LOG_CHANNEL_NAME, overwrites=overwrites,
            topic="🛡️ Логи Anti-Raid бота", reason="Anti-Raid: канал логов",
        )
    except discord.HTTPException:
        return None


def make_alert(title: str, user: discord.User | discord.Member,
               action: str, details: str,
               color: discord.Color = discord.Color.red()) -> discord.Embed:
    e = discord.Embed(title=title, description=f"**Действие:** {action}",
                      color=color, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Нарушитель",
                value=f"{user.mention} (`{user}` • `{user.id}`)", inline=False)
    e.add_field(name="Детали", value=details, inline=False)
    e.set_footer(text="Anti-Raid Bot")
    return e


async def log_later(guild: discord.Guild, embed: discord.Embed) -> None:
    """Логировать в фоне — НЕ блокируя основной поток."""
    ch = await log_channel(guild)
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass


# ═══════════════════════════════════════════════
# НЕЙТРАЛИЗАЦИЯ: сначала ДЕЙСТВИЕ, потом лог
# ═══════════════════════════════════════════════
async def nuke_user(guild: discord.Guild, user: discord.User | discord.Member,
                    reason: str, alert_title: str, details: str) -> None:
    """
    МГНОВЕННАЯ нейтрализация:
      1. Снять ВСЕ роли (1 API-запрос)
      2. Забанить (1 API-запрос)
      3. Только потом залогировать (не блокирует)
    """
    if user.id in _punished:
        return
    _punished.add(user.id)

    # ШАГ 1: снять все роли — моментально лишает любых прав
    member = guild.get_member(user.id)
    if member:
        removable = [r for r in member.roles if not r.is_default() and not r.managed]
        if removable:
            try:
                await member.remove_roles(*removable, reason=reason)
            except discord.HTTPException:
                pass

    # ШАГ 2: бан — параллельно, не ждём
    banned = False
    try:
        await guild.ban(user, reason=reason, delete_message_seconds=0)
        banned = True
    except discord.HTTPException:
        pass

    # ШАГ 3: лог — в фоне, не замедляет реакцию на следующие события
    status = "✅ Забанен" if banned else "❌ Не удалось забанить"
    asyncio.create_task(log_later(guild, make_alert(
        alert_title, user, reason, f"{details}\n**Статус:** {status}",
        discord.Color.dark_red()
    )))


async def instant_strip(guild: discord.Guild, user_id: int, reason: str) -> None:
    """Снять все роли моментально."""
    member = guild.get_member(user_id)
    if not member:
        return
    removable = [r for r in member.roles if not r.is_default() and not r.managed]
    if removable:
        try:
            await member.remove_roles(*removable, reason=reason)
        except discord.HTTPException:
            pass


# ═══════════════════════════════════════════════
# ANTI-NUKE: удаление каналов = МГНОВЕННЫЙ БАН
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild

    # Быстрая проверка — если уже обрабатываем, выход
    user = await audit_user(guild, discord.AuditLogAction.channel_delete)
    if not user or is_safe(user.id):
        return
    if user.id in _processing or user.id in _punished:
        return
    _processing.add(user.id)

    try:
        # МГНОВЕННО: снять роли + бан (2 API-вызова, ~100мс)
        await nuke_user(
            guild, user,
            f"Anti-Nuke: удаление канала #{channel.name}",
            "🔥 УДАЛЕНИЕ КАНАЛА — МГНОВЕННЫЙ БАН",
            f"Удалил канал `#{channel.name}` ({channel.type})\n"
            f"**Реакция: мгновенная** — снятие ролей + бан"
        )
    finally:
        _processing.discard(user.id)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    user = await audit_user(role.guild, discord.AuditLogAction.role_delete)
    if not user or is_safe(user.id):
        return

    count = role_del.record(user.id)

    # Первое удаление — превентивно снять роли
    if count == 1:
        await instant_strip(role.guild, user.id, "Anti-Nuke: превентивно (удаление роли)")

    if role_del.over(user.id):
        if user.id not in _processing:
            _processing.add(user.id)
            try:
                await nuke_user(
                    role.guild, user,
                    f"Anti-Nuke: удаление {count} ролей",
                    "🔥 МАССОВОЕ УДАЛЕНИЕ РОЛЕЙ",
                    f"Удалено **{count}** ролей за 60 сек.\nПоследняя: `{role.name}`"
                )
            finally:
                _processing.discard(user.id)
        role_del.reset(user.id)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    banner = await audit_user(guild, discord.AuditLogAction.ban)
    if not banner or is_safe(banner.id):
        return

    count = mass_ban.record(banner.id)
    if count == 1:
        await instant_strip(guild, banner.id, "Anti-Nuke: превентивно (бан)")

    if mass_ban.over(banner.id):
        await nuke_user(
            guild, banner,
            f"Anti-Nuke: массовый бан ({count})",
            "🔥 МАССОВЫЙ БАН",
            f"Забанил **{count}** людей за 60 сек."
        )
        mass_ban.reset(banner.id)


# ═══════════════════════════════════════════════
# ANTI-BOT: отслеживание добавления ботов
# ═══════════════════════════════════════════════
@bot.event
async def on_member_join(member: discord.Member):
    # ── Бот добавлен — снять роли превентивно ──
    if member.bot:
        guild = member.guild
        inviter = await audit_user(guild, discord.AuditLogAction.bot_add)
        if inviter and not is_safe(inviter.id):
            await instant_strip(guild, member.id, "Anti-Nuke: новый бот — превентивно")
            asyncio.create_task(log_later(guild, make_alert(
                "🤖 НОВЫЙ БОТ", member,
                f"Добавил: {inviter.mention}",
                "Боту превентивно сняты все роли.",
                discord.Color.orange()
            )))
        return

    # ── Обычный пользователь ──
    guild = member.guild
    now = time.monotonic()
    join_times.append(now)

    # Массовый вход → рейд-режим
    recent = sum(1 for t in join_times if now - t < JOIN_FLOOD_WINDOW)
    if recent >= JOIN_FLOOD_LIMIT and guild.id not in raid_mode:
        raid_mode[guild.id] = now
        asyncio.create_task(log_later(guild, discord.Embed(
            title="🚨 РЕЙД-РЕЖИМ АКТИВИРОВАН",
            description=(
                f"**{JOIN_FLOOD_LIMIT}+** входов за **{JOIN_FLOOD_WINDOW}** сек.\n"
                f"Новые участники будут кикнуты.\n"
                f"`!raidmode off` для отключения"
            ),
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )))

    # Рейд → кик
    if guild.id in raid_mode:
        try:
            await member.kick(reason="Anti-Raid: рейд-режим")
        except discord.HTTPException:
            pass
        return

    # Анализ подозрительности
    age = (datetime.now(timezone.utc) - member.created_at).days
    score = 0
    reasons = []

    if age < 1:
        score += 50
        reasons.append(f"🆕 Аккаунт < 1 дня ({age} д.)")
    elif age < 3:
        score += 35
        reasons.append(f"🆕 Аккаунт < 3 дней ({age} д.)")
    elif age < SUSPICIOUS_AGE_DAYS:
        score += 20
        reasons.append(f"🆕 Аккаунт < {SUSPICIOUS_AGE_DAYS} дней ({age} д.)")

    name = member.name
    if len(name) >= 15:
        digit_ratio = sum(c.isdigit() for c in name) / len(name)
        if digit_ratio > 0.4:
            score += 15
            reasons.append("🤖 Подозрительное имя")

    if score >= 40:
        try:
            await member.timeout(
                discord.utils.utcnow() + timedelta(hours=1),
                reason=f"Anti-Raid: подозрительный ({score}/100)"
            )
        except discord.HTTPException:
            pass
        asyncio.create_task(log_later(guild, make_alert(
            "⚠️ КАРАНТИН", member, "Авто-мут 1 час",
            f"**Подозрительность:** {score}/100\n" +
            "\n".join(f"  {r}" for r in reasons),
            discord.Color.red()
        )))
    elif score >= 15:
        asyncio.create_task(log_later(guild, make_alert(
            "ℹ️ Подозрительный вход", member, "Наблюдение",
            f"**Подозрительность:** {score}/100\n" +
            "\n".join(f"  {r}" for r in reasons),
            discord.Color.yellow()
        )))


# ═══════════════════════════════════════════════
# ANTI-SPAM
# ═══════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    if (message.author.bot or not message.guild
            or is_safe(message.author.id)):
        await bot.process_commands(message)
        return

    member = message.guild.get_member(message.author.id)
    if not member:
        await bot.process_commands(message)
        return

    uid = message.author.id
    muted = False
    reason = ""
    details = ""

    # 1) Скорость сообщений
    count = msg_flood.record(uid)
    if msg_flood.over(uid):
        muted = True
        reason = f"Anti-Spam: {count} сообщений за {MSG_WINDOW} сек."
        details = f"**{count}** сообщений за **{MSG_WINDOW}** сек."
        msg_flood.reset(uid)

    # 2) Дубликаты
    if not muted and message.content:
        now = time.monotonic()
        cache = dup_cache[uid]
        cache.append((now, message.content.lower().strip()))
        cutoff = now - DUP_WINDOW
        target = message.content.lower().strip()
        dups = sum(1 for t, c in cache if t > cutoff and c == target)
        if dups >= DUP_LIMIT:
            muted = True
            reason = f"Anti-Spam: {dups} одинаковых сообщений"
            details = f"**{dups}** дубликатов за **{DUP_WINDOW}** сек."

    # 3) Массовые упоминания
    if not muted:
        mentions = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            mentions += 10
        if mentions >= MENTION_LIMIT:
            muted = True
            reason = f"Anti-Spam: {mentions} упоминаний"
            details = f"**{mentions}** упоминаний в одном сообщении."
            try:
                await message.delete()
            except discord.HTTPException:
                pass

    if muted:
        # Снять роли если админ, потом мут
        if member.guild_permissions.administrator:
            removable = [r for r in member.roles if not r.is_default() and not r.managed]
            if removable:
                try:
                    await member.remove_roles(*removable, reason=reason)
                except discord.HTTPException:
                    pass
        try:
            await member.timeout(
                discord.utils.utcnow() + timedelta(seconds=MUTE_SECONDS),
                reason=reason,
            )
        except discord.HTTPException:
            pass
        asyncio.create_task(log_later(message.guild, make_alert(
            "🔇 Авто-мут", member, reason,
            f"{details}\nМут: **{MUTE_SECONDS // 60}** мин.",
            discord.Color.orange()
        )))

    await bot.process_commands(message)


# ═══════════════════════════════════════════════
# ANTI-ESCALATION: выдача опасных прав
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    dangerous = {"administrator", "manage_guild", "manage_channels",
                 "ban_members", "manage_roles"}
    added = [p for p in dangerous
             if not getattr(before.permissions, p) and getattr(after.permissions, p)]
    if not added:
        return

    changer = await audit_user(after.guild, discord.AuditLogAction.role_update)
    if not changer or is_safe(changer.id):
        return

    # Откатить + снять роли нарушителя
    try:
        await after.edit(permissions=before.permissions,
                         reason="Anti-Nuke: откат опасных прав")
    except discord.HTTPException:
        pass
    await instant_strip(after.guild, changer.id, "Anti-Nuke: эскалация прав")
    asyncio.create_task(log_later(after.guild, make_alert(
        "🚫 ЭСКАЛАЦИЯ ЗАБЛОКИРОВАНА", changer,
        f"Добавил: {', '.join(added)} в роль `{after.name}`",
        "Права откачены, роли нарушителя сняты.",
        discord.Color.dark_red()
    )))


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.roles == after.roles or is_safe(after.id):
        return
    added = set(after.roles) - set(before.roles)
    danger = [r for r in added if r.permissions.administrator or r.permissions.manage_guild]
    if not danger:
        return

    changer = await audit_user(after.guild, discord.AuditLogAction.member_role_update)
    if not changer or is_safe(changer.id):
        return

    try:
        await after.remove_roles(*danger, reason="Anti-Nuke: несанкционированная админка")
    except discord.HTTPException:
        pass
    await instant_strip(after.guild, changer.id, "Anti-Nuke: выдача админки")
    asyncio.create_task(log_later(after.guild, make_alert(
        "🚫 ВЫДАЧА АДМИНКИ ЗАБЛОКИРОВАНА", changer,
        f"Выдал {', '.join(r.name for r in danger)} → {after.mention}",
        "Роли отобраны, нарушитель обезврежен.",
        discord.Color.dark_red()
    )))


# ═══════════════════════════════════════════════
# ФОНОВАЯ ЗАДАЧА: авто-отключение рейд-режима
# ═══════════════════════════════════════════════
@tasks.loop(seconds=30)
async def raid_check():
    now = time.monotonic()
    expired = [g for g, t in raid_mode.items() if now - t > RAID_MODE_DURATION]
    for gid in expired:
        del raid_mode[gid]
        guild = bot.get_guild(gid)
        if guild:
            asyncio.create_task(log_later(guild, discord.Embed(
                title="✅ Рейд-режим деактивирован",
                description="Сервер в нормальном режиме.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )))


@raid_check.before_loop
async def before_raid_check():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════
@bot.command(name="raidmode")
@commands.has_permissions(administrator=True)
async def raidmode_cmd(ctx: commands.Context, mode: str = "status"):
    if not ctx.guild:
        return
    mode = mode.lower()
    if mode == "on":
        raid_mode[ctx.guild.id] = time.monotonic()
        await ctx.send("🚨 **Рейд-режим активирован.**")
    elif mode == "off":
        raid_mode.pop(ctx.guild.id, None)
        await ctx.send("✅ **Рейд-режим выключен.**")
    else:
        s = "🚨 АКТИВЕН" if ctx.guild.id in raid_mode else "✅ Неактивен"
        await ctx.send(f"Рейд-режим: **{s}**")


@bot.command(name="status")
@commands.has_permissions(administrator=True)
async def status_cmd(ctx: commands.Context):
    e = discord.Embed(title="🛡️ Anti-Raid Bot", color=discord.Color.green(),
                      timestamp=datetime.now(timezone.utc))
    e.add_field(name="Anti-Nuke", value=(
        f"Каналы: **{CHANNEL_DELETE_LIMIT}** удал. → бан\n"
        f"Роли: **{ROLE_DELETE_LIMIT}** → бан\n"
        f"Масс-бан: **{MASS_BAN_LIMIT}** → бан"
    ), inline=True)
    e.add_field(name="Anti-Raid", value=(
        f"Входы: **{JOIN_FLOOD_LIMIT}** / {JOIN_FLOOD_WINDOW}с\n"
        f"Подозрит.: **<{SUSPICIOUS_AGE_DAYS}** дн."
    ), inline=True)
    e.add_field(name="Anti-Spam", value=(
        f"Флуд: **{MSG_LIMIT}** / {MSG_WINDOW}с\n"
        f"Пинги: **{MENTION_LIMIT}** → мут"
    ), inline=True)
    rm = "🚨 АКТИВЕН" if ctx.guild.id in raid_mode else "✅ Нет"
    e.add_field(name="Рейд-режим", value=rm, inline=False)
    await ctx.send(embed=e)


@bot.command(name="wl")
@commands.has_permissions(administrator=True)
async def whitelist_cmd(ctx: commands.Context, action: str = "list",
                        member: discord.Member | None = None):
    if action == "add" and member:
        WHITELIST_IDS.add(member.id)
        await ctx.send(f"✅ {member.mention} в белом списке.")
    elif action == "remove" and member:
        WHITELIST_IDS.discard(member.id)
        await ctx.send(f"✅ {member.mention} убран из белого списка.")
    else:
        if WHITELIST_IDS:
            await ctx.send(f"📋 Белый список: {', '.join(f'`{u}`' for u in WHITELIST_IDS)}")
        else:
            await ctx.send("📋 Белый список пуст.")


# ═══════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"🛡️ {bot.user.name} запущен | Серверов: {len(bot.guilds)}")
    if not raid_check.is_running():
        raid_check.start()
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="за безопасностью 🛡️"
        )
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Нет прав.")
    elif not isinstance(error, commands.CommandNotFound):
        print(f"[ERR] {error}")


if not TOKEN:
    print("❌ Установи DISCORD_BOT_TOKEN!")
    raise SystemExit(1)

bot.run(TOKEN)
