"""
🛡️ Anti-Raid Discord Bot (Lightweight Edition)
Один файл — вся защита. Оптимизирован для bothost.ru.

Функции:
  • Anti-Nuke:  удалил 2+ каналов → бан
  • Anti-Raid:  массовый вход → рейд-режим + карантин
  • Anti-Spam:  флуд/дубликаты → мут
  • Логирование в #anti-raid-logs
"""

import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

# ═══════════════════════════════════════════════
# НАСТРОЙКИ (меняй под себя)
# ═══════════════════════════════════════════════
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# Твой Discord ID и ID доверенных админов (не попадают под проверки)
OWNER_IDS: set[int] = set()       # Пример: {123456789, 987654321}
WHITELIST_IDS: set[int] = set()

LOG_CHANNEL_NAME = "anti-raid-logs"

# Anti-Nuke: лимиты за 60 секунд
CHANNEL_DELETE_LIMIT = 2    # каналов → бан
ROLE_DELETE_LIMIT = 3       # ролей → бан
MASS_BAN_LIMIT = 3          # банов → бан
MASS_KICK_LIMIT = 5         # киков → бан

# Anti-Raid
JOIN_FLOOD_LIMIT = 5        # входов за JOIN_FLOOD_WINDOW → рейд-режим
JOIN_FLOOD_WINDOW = 10      # секунд
SUSPICIOUS_AGE_DAYS = 7     # аккаунт младше → подозрительный
RAID_MODE_DURATION = 300    # секунд рейд-режима

# Anti-Spam
MSG_LIMIT = 10              # сообщений за MSG_WINDOW → мут
MSG_WINDOW = 5              # секунд
DUP_LIMIT = 5               # одинаковых сообщений за DUP_WINDOW → мут
DUP_WINDOW = 10             # секунд
MENTION_LIMIT = 8           # упоминаний в одном сообщении → мут
MUTE_SECONDS = 300          # длительность мута


# ═══════════════════════════════════════════════
# ТРЕКЕР ДЕЙСТВИЙ (универсальный)
# ═══════════════════════════════════════════════
class Tracker:
    """Считает действия пользователя в скользящем окне."""

    __slots__ = ("window", "limit", "_data", "_last_clean")

    def __init__(self, window: float, limit: int):
        self.window = window
        self.limit = limit
        self._data: dict[int, list[float]] = defaultdict(list)
        self._last_clean = time.monotonic()

    def record(self, uid: int) -> int:
        now = time.monotonic()
        # Чистка раз в 2 минуты
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
# ИНИЦИАЛИЗАЦИЯ БОТА
# ═══════════════════════════════════════════════
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Трекеры
ch_del = Tracker(60, CHANNEL_DELETE_LIMIT)
role_del = Tracker(60, ROLE_DELETE_LIMIT)
mass_ban = Tracker(60, MASS_BAN_LIMIT)
mass_kick = Tracker(60, MASS_KICK_LIMIT)
msg_flood = Tracker(MSG_WINDOW, MSG_LIMIT)

# Рейд-режим: guild_id → время активации
raid_mode: dict[int, float] = {}

# Очередь входов для детекции рейда
join_times: deque[float] = deque(maxlen=50)

# Дубликаты сообщений: user_id → deque[(time, text)]
dup_cache: dict[int, deque[tuple[float, str]]] = defaultdict(lambda: deque(maxlen=15))

# Уже наказанные (чтобы не банить дважды)
punished: set[int] = set()


# ═══════════════════════════════════════════════
# ХЕЛПЕРЫ
# ═══════════════════════════════════════════════
def is_safe(user_id: int) -> bool:
    return user_id == bot.user.id or user_id in OWNER_IDS or user_id in WHITELIST_IDS


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
            LOG_CHANNEL_NAME,
            overwrites=overwrites,
            topic="🛡️ Логи Anti-Raid бота",
            reason="Anti-Raid: канал логов",
        )
    except discord.HTTPException:
        return None


async def log_embed(guild: discord.Guild, embed: discord.Embed) -> None:
    ch = await log_channel(guild)
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass


def alert(title: str, user: discord.User | discord.Member,
          action: str, details: str,
          color: discord.Color = discord.Color.red()) -> discord.Embed:
    e = discord.Embed(title=title, description=f"**Действие:** {action}",
                      color=color, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Нарушитель",
                value=f"{user.mention} (`{user}` • `{user.id}`)", inline=False)
    e.add_field(name="Детали", value=details, inline=False)
    e.set_footer(text="Anti-Raid Bot")
    if hasattr(user, "avatar") and user.avatar:
        e.set_thumbnail(url=user.avatar.url)
    return e


async def audit_user(guild: discord.Guild,
                     action: discord.AuditLogAction) -> discord.User | None:
    """Кто выполнил последнее действие (из audit log, < 10 сек)."""
    now = datetime.now(timezone.utc)
    try:
        async for entry in guild.audit_logs(limit=3, action=action):
            if entry.created_at and (now - entry.created_at).total_seconds() < 10:
                return entry.user
    except discord.Forbidden:
        pass
    return None


async def strip_and_ban(guild: discord.Guild, user: discord.User | discord.Member,
                        reason: str, alert_title: str, details: str) -> None:
    if user.id in punished:
        return
    punished.add(user.id)

    # Снять ВСЕ роли разом (убирает админку → бан/мут начинают работать)
    member = guild.get_member(user.id)
    if member:
        removable = [r for r in member.roles if not r.is_default() and not r.managed]
        if removable:
            try:
                await member.remove_roles(*removable, reason=reason)
            except discord.HTTPException:
                pass

    # Бан
    banned = False
    try:
        await guild.ban(user, reason=reason, delete_message_seconds=0)
        banned = True
    except discord.HTTPException:
        pass

    status = "✅ Забанен" if banned else "❌ Не удалось забанить"
    await log_embed(guild, alert(
        alert_title, user, reason, f"{details}\n**Статус:** {status}",
        discord.Color.dark_red()
    ))


# ═══════════════════════════════════════════════
# БЫСТРАЯ НЕЙТРАЛИЗАЦИЯ — снять роли моментально
# ═══════════════════════════════════════════════
async def instant_strip(guild: discord.Guild, user_id: int, reason: str) -> None:
    """Моментально снять ВСЕ роли у пользователя (превентивно)."""
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
# ANTI-NUKE: удаление каналов / ролей / массовые баны
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    user = await audit_user(channel.guild, discord.AuditLogAction.channel_delete)
    if not user or is_safe(user.id):
        return

    count = ch_del.record(user.id)

    # ⚡ ПЕРВОЕ удаление — СРАЗУ снять все роли (превентивно!)
    if count == 1:
        await instant_strip(
            channel.guild, user.id,
            "Anti-Nuke: превентивное снятие ролей (удаление канала)"
        )
        await log_embed(channel.guild, alert(
            "⚡ ПРЕВЕНТИВНАЯ ЗАЩИТА", user,
            f"Удалил #{channel.name} — роли сняты!",
            f"Все роли сняты превентивно.\n"
            f"Ещё **{CHANNEL_DELETE_LIMIT - count}** удаление = **бан**.",
            discord.Color.orange()
        ))

    # 2+ удалений — БАН
    if ch_del.over(user.id):
        await strip_and_ban(
            channel.guild, user,
            f"Anti-Nuke: удаление {count} каналов",
            "🔥 МАССОВОЕ УДАЛЕНИЕ КАНАЛОВ",
            f"Удалено **{count}** каналов за 60 сек.\nПоследний: `#{channel.name}`"
        )
        ch_del.reset(user.id)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    user = await audit_user(role.guild, discord.AuditLogAction.role_delete)
    if not user or is_safe(user.id):
        return
    count = role_del.record(user.id)

    # ⚡ Первое удаление роли — превентивное снятие ролей
    if count == 1:
        await instant_strip(
            role.guild, user.id,
            "Anti-Nuke: превентивное снятие ролей (удаление роли)"
        )

    if role_del.over(user.id):
        await strip_and_ban(
            role.guild, user,
            f"Anti-Nuke: удаление {count} ролей",
            "🔥 МАССОВОЕ УДАЛЕНИЕ РОЛЕЙ",
            f"Удалено **{count}** ролей за 60 сек.\nПоследняя: `{role.name}`"
        )
        role_del.reset(user.id)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    banner = await audit_user(guild, discord.AuditLogAction.ban)
    if not banner or is_safe(banner.id):
        return
    count = mass_ban.record(banner.id)

    # ⚡ Первый бан — превентивное снятие ролей
    if count == 1:
        await instant_strip(guild, banner.id, "Anti-Nuke: превентивно (массовый бан)")

    if mass_ban.over(banner.id):
        await strip_and_ban(
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
    # ── Если добавлен БОТ — проверить кто добавил ──
    if member.bot:
        guild = member.guild
        entry = await audit_user(guild, discord.AuditLogAction.bot_add)
        if entry and not is_safe(entry.id):
            # Снять все роли у добавленного бота
            await instant_strip(guild, member.id, "Anti-Nuke: новый бот — превентивно")
            await log_embed(guild, alert(
                "🤖 НОВЫЙ БОТ ДОБАВЛЕН", member,
                f"Добавлен пользователем: {entry.mention}",
                f"Боту превентивно сняты все роли.\n"
                f"Если бот начнёт удалять каналы — будет забанен.",
                discord.Color.orange()
            ))
        return

    guild = member.guild
    now = time.monotonic()
    join_times.append(now)

    # Проверка массового входа
    recent = sum(1 for t in join_times if now - t < JOIN_FLOOD_WINDOW)
    if recent >= JOIN_FLOOD_LIMIT and guild.id not in raid_mode:
        raid_mode[guild.id] = now
        await log_embed(guild, discord.Embed(
            title="🚨 РЕЙД-РЕЖИМ АКТИВИРОВАН",
            description=(
                f"**{JOIN_FLOOD_LIMIT}+** входов за **{JOIN_FLOOD_WINDOW}** сек.\n"
                f"Новые участники будут кикнуты.\n"
                f"Отключение через **{RAID_MODE_DURATION // 60}** мин. "
                f"или `!raidmode off`"
            ),
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        ))

    # Рейд-режим → кик
    if guild.id in raid_mode:
        try:
            await member.kick(reason="Anti-Raid: рейд-режим")
        except discord.HTTPException:
            pass
        await log_embed(guild, alert(
            "🚨 Рейд-кик", member, "Авто-кик",
            "Вошёл во время рейд-режима.", discord.Color.red()
        ))
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
            reasons.append("🤖 Подозрительное имя (много цифр)")

    if score >= 40:
        # Карантин: мут на 1 час
        try:
            await member.timeout(
                discord.utils.utcnow() + timedelta(hours=1),
                reason=f"Anti-Raid: подозрительный ({score}/100)"
            )
        except discord.HTTPException:
            pass
        await log_embed(guild, alert(
            "⚠️ КАРАНТИН", member, "Авто-мут 1 час",
            f"**Подозрительность:** {score}/100\n" +
            "\n".join(f"  {r}" for r in reasons),
            discord.Color.red()
        ))
    elif score >= 15:
        await log_embed(guild, alert(
            "ℹ️ Подозрительный вход", member, "Наблюдение",
            f"**Подозрительность:** {score}/100\n" +
            "\n".join(f"  {r}" for r in reasons),
            discord.Color.yellow()
        ))

    # Лог входа
    await log_embed(guild, discord.Embed(
        title="📥 Новый участник",
        description=f"{member.mention} • `{member}` • ID: `{member.id}`\n"
                    f"Возраст аккаунта: **{age}** дн.",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    ))


# ═══════════════════════════════════════════════
# ANTI-SPAM: флуд + дубликаты + массовые пинги
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

    # 1) Скорость сообщений
    count = msg_flood.record(uid)
    if msg_flood.over(uid):
        muted = True
        reason = f"Anti-Spam: {count} сообщений за {MSG_WINDOW} сек."
        details = f"Отправлено **{count}** сообщений за **{MSG_WINDOW}** сек."
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
            details = (f"**{dups}** одинаковых сообщений за **{DUP_WINDOW}** сек.\n"
                       f"Текст: `{message.content[:80]}…`")

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
        # Если у нарушителя есть админка — снять ВСЕ роли, иначе timeout не сработает
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
        await log_embed(message.guild, alert(
            "🔇 Авто-мут", member, reason,
            f"{details}\nМут: **{MUTE_SECONDS // 60}** мин.",
            discord.Color.orange()
        ))

    await bot.process_commands(message)


# ═══════════════════════════════════════════════
# ANTI-ESCALATION: отслеживание выдачи опасных прав
# ═══════════════════════════════════════════════
@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    """Если кто-то добавил опасные права в роль — откатить."""
    if is_safe(after.guild.owner_id or 0):
        pass  # владелец сервера может всё

    dangerous = {"administrator", "manage_guild", "manage_channels",
                 "ban_members", "manage_roles"}

    added_perms = []
    for perm in dangerous:
        if not getattr(before.permissions, perm) and getattr(after.permissions, perm):
            added_perms.append(perm)

    if not added_perms:
        return

    # Кто изменил роль?
    changer = await audit_user(after.guild, discord.AuditLogAction.role_update)
    if not changer or is_safe(changer.id):
        return

    # Откатить права
    try:
        await after.edit(permissions=before.permissions,
                         reason="Anti-Nuke: откат опасных прав")
    except discord.HTTPException:
        pass

    # Снять роли у того, кто это сделал
    await instant_strip(after.guild, changer.id,
                        "Anti-Nuke: попытка эскалации прав")

    await log_embed(after.guild, alert(
        "🚫 ЭСКАЛАЦИЯ ПРАВ ЗАБЛОКИРОВАНА", changer,
        f"Попытался добавить: {', '.join(added_perms)}",
        f"Роль: `{after.name}`\n"
        f"Права откачены, роли нарушителя сняты.",
        discord.Color.dark_red()
    ))


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Если кому-то выдали роль с админкой — проверить."""
    if before.roles == after.roles:
        return
    if is_safe(after.id):
        return

    added_roles = set(after.roles) - set(before.roles)
    dangerous_added = [r for r in added_roles
                       if r.permissions.administrator or r.permissions.manage_guild]

    if not dangerous_added:
        return

    # Кто выдал роль?
    changer = await audit_user(after.guild, discord.AuditLogAction.member_role_update)
    if not changer or is_safe(changer.id):
        return

    # Снять выданные опасные роли
    try:
        await after.remove_roles(*dangerous_added,
                                 reason="Anti-Nuke: несанкционированная выдача админки")
    except discord.HTTPException:
        pass

    # Снять роли у того, кто выдал
    await instant_strip(after.guild, changer.id,
                        "Anti-Nuke: несанкционированная выдача админки")

    roles_str = ", ".join(f"`{r.name}`" for r in dangerous_added)
    await log_embed(after.guild, alert(
        "🚫 ВЫДАЧА АДМИНКИ ЗАБЛОКИРОВАНА", changer,
        f"Выдал {roles_str} → {after.mention}",
        f"Роли отобраны у получателя, права нарушителя сняты.",
        discord.Color.dark_red()
    ))


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
            await log_embed(guild, discord.Embed(
                title="✅ Рейд-режим деактивирован",
                description="Сервер вернулся в нормальный режим.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            ))


@raid_check.before_loop
async def before_raid_check():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════
@bot.command(name="raidmode")
@commands.has_permissions(administrator=True)
async def raidmode_cmd(ctx: commands.Context, mode: str = "status"):
    """Управление рейд-режимом: !raidmode [on/off/status]"""
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
    """Статус бота: !status"""
    e = discord.Embed(title="🛡️ Anti-Raid Bot", color=discord.Color.green(),
                      timestamp=datetime.now(timezone.utc))
    e.add_field(name="Anti-Nuke", value=(
        f"Каналы: **{CHANNEL_DELETE_LIMIT}** → бан\n"
        f"Роли: **{ROLE_DELETE_LIMIT}** → бан\n"
        f"Массовый бан: **{MASS_BAN_LIMIT}** → бан"
    ), inline=True)
    e.add_field(name="Anti-Raid", value=(
        f"Входы: **{JOIN_FLOOD_LIMIT}** / {JOIN_FLOOD_WINDOW}с → режим\n"
        f"Подозрит.: **<{SUSPICIOUS_AGE_DAYS}** дн."
    ), inline=True)
    e.add_field(name="Anti-Spam", value=(
        f"Флуд: **{MSG_LIMIT}** / {MSG_WINDOW}с → мут\n"
        f"Пинги: **{MENTION_LIMIT}** → мут"
    ), inline=True)
    rm = "🚨 АКТИВЕН" if ctx.guild.id in raid_mode else "✅ Нет"
    e.add_field(name="Рейд-режим", value=rm, inline=False)
    await ctx.send(embed=e)


@bot.command(name="wl")
@commands.has_permissions(administrator=True)
async def whitelist_cmd(ctx: commands.Context, action: str = "list",
                        member: discord.Member | None = None):
    """Белый список: !wl [add/remove/list] @user"""
    if action == "add" and member:
        WHITELIST_IDS.add(member.id)
        await ctx.send(f"✅ {member.mention} добавлен в белый список.")
    elif action == "remove" and member:
        WHITELIST_IDS.discard(member.id)
        await ctx.send(f"✅ {member.mention} убран из белого списка.")
    else:
        if WHITELIST_IDS:
            names = ", ".join(f"`{uid}`" for uid in WHITELIST_IDS)
            await ctx.send(f"📋 Белый список: {names}")
        else:
            await ctx.send("📋 Белый список пуст.")


# ═══════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"🛡️ {bot.user.name} запущен | Серверов: {len(bot.guilds)}")
    raid_check.start()
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="за безопасностью 🛡️"
        )
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Нет прав.")
    elif isinstance(error, commands.CommandNotFound):
        pass


if not TOKEN:
    print("❌ Установи переменную окружения DISCORD_BOT_TOKEN!")
    raise SystemExit(1)

bot.run(TOKEN)
