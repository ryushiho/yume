from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from discord.ext import commands

from yume_store import (
    add_user_xp,
    get_guild_xp_config,
    get_user_xp_progress,
    get_xp_leaderboard,
    parse_id_list,
    reset_user_xp,
    set_guild_xp_config,
)

logger = logging.getLogger(__name__)

DEV_USER_ID = 1433962010785349634

# Static banner asset shipped with the bot (no extra deps needed at runtime)
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ASSETS_DIR = os.path.join(ROOT_DIR, "assets")
STATIC_BANNER_PATH = os.path.join(ASSETS_DIR, "levelup_banner.png")


def _now_ts() -> int:
    return int(time.time())


async def _is_command_message(bot: commands.Bot, message: discord.Message) -> bool:
    """Return True if this message looks like a prefix command invocation."""

    try:
        prefixes = await bot.get_prefix(message)
    except Exception:
        prefixes = "!"

    prefixes_list = [prefixes] if isinstance(prefixes, str) else list(prefixes)

    content = (message.content or "").lstrip()
    if not content:
        return False

    for pfx in prefixes_list:
        if pfx and content.startswith(str(pfx)):
            return True
    return False


def _has_any_role(member: discord.Member, role_ids: Sequence[int]) -> bool:
    if not role_ids:
        return False
    try:
        mids = {int(getattr(r, "id", 0)) for r in member.roles}
        return any(int(rid) in mids for rid in role_ids)
    except Exception:
        return False


_RE_EFFECTIVE = re.compile(r"[0-9A-Za-z가-힣]")
_RE_URL = re.compile(r"https?://", re.IGNORECASE)


def _effective_char_count(s: str) -> int:
    return len(_RE_EFFECTIVE.findall(s or ""))


def _normalize_for_repeat(s: str) -> str:
    # Lower, collapse whitespace, drop trivial punctuation spam.
    x = (s or "").strip().lower()
    x = re.sub(r"\s+", " ", x)
    x = re.sub(r"[\W_]+", " ", x)  # punctuation -> spaces
    x = re.sub(r"\s+", " ", x).strip()
    return x


def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", errors="ignore")).hexdigest()


def _safe_int(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        v = cfg.get(key)
        if v is None:
            return int(default)
        return int(v)
    except Exception:
        return int(default)


def _safe_str(cfg: Dict[str, Any], key: str, default: str) -> str:
    try:
        v = cfg.get(key)
        return str(v) if v is not None else str(default)
    except Exception:
        return str(default)


def _pick_cmd_tier(module_name: str) -> str:
    """Map command module to an XP tier.

    tiers: system | game | social | chat | default
    """

    m = module_name or ""

    # system/admin
    if m in ("cogs.admin", "cogs.noise_settings", "cogs.channel_settings", "cogs.rule_maker", "cogs.aby_environment"):
        return "system"

    # game-ish
    if m in ("cogs.aby_mini_game", "cogs.aby_workshop", "cogs.aby_quest_board", "cogs.survival_cooking"):
        return "game"

    # chat/story
    if m in ("cogs.yume_chat", "cogs.yume_diary"):
        return "chat"

    # social/fun
    if m in ("cogs.yume_fun", "cogs.social", "cogs.stamps"):
        return "social"

    return "default"


class LevelingCog(commands.Cog):
    """서버 채팅/유메 기능 사용으로 경험치를 쌓고 레벨업하는 시스템."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # repeat-spam guard (NOT a cooldown): same message rapidly repeated -> no XP
        self._last_msg_sig: Dict[Tuple[int, int], Tuple[int, str]] = {}

    # -------------------------
    # Ignore rules
    # -------------------------

    def _should_ignore(
        self,
        cfg: Dict[str, Any],
        *,
        channel_id: int,
        author: discord.abc.User,
        member: Optional[discord.Member],
    ) -> bool:
        ignore_channels = parse_id_list(str(cfg.get("ignore_channel_ids") or ""))
        if int(channel_id) in ignore_channels:
            return True

        ignore_roles = parse_id_list(str(cfg.get("ignore_role_ids") or ""))
        if member is not None and ignore_roles:
            if _has_any_role(member, ignore_roles):
                return True

        # bot/webhook
        if getattr(author, "bot", False):
            return True
        return False

    # -------------------------
    # XP calculation
    # -------------------------

    def _calc_chat_xp(self, cfg: Dict[str, Any], message: discord.Message, now: int) -> int:
        content = (message.content or "").strip()

        min_chars = _safe_int(cfg, "chat_min_chars", 0)
        if _effective_char_count(content) < max(0, min_chars):
            return 0

        # repeat filter (same normalized message within repeat_window_sec)
        repeat_window = _safe_int(cfg, "chat_repeat_window_sec", 0)
        norm = _normalize_for_repeat(content)
        if norm:
            sig = _sha1(norm)
            k = (int(message.guild.id), int(message.author.id))
            prev = self._last_msg_sig.get(k)
            if prev is not None:
                prev_ts, prev_sig = prev
                if repeat_window > 0 and prev_sig == sig and (now - int(prev_ts)) <= max(0, repeat_window):
                    return 0
            self._last_msg_sig[k] = (int(now), sig)

        base_min = _safe_int(cfg, "chat_xp_min", 15)
        base_max = _safe_int(cfg, "chat_xp_max", 25)
        if base_max < base_min:
            base_max = base_min

        base = random.randint(max(0, base_min), max(0, base_max))

        # Length bonus: +1 per N chars, capped
        step = max(1, _safe_int(cfg, "chat_len_step", 30))
        cap = max(0, _safe_int(cfg, "chat_len_cap", 10))
        length_bonus = min(cap, max(0, len(content) // step))

        attach_bonus = 0
        if getattr(message, "attachments", None):
            if len(message.attachments) > 0:
                attach_bonus = max(0, _safe_int(cfg, "chat_attach_bonus", 3))

        link_bonus = 0
        if _RE_URL.search(content or ""):
            link_bonus = max(0, _safe_int(cfg, "chat_link_bonus", 0))

        total_cap = max(1, _safe_int(cfg, "chat_total_cap", 50))

        delta = base + length_bonus + attach_bonus + link_bonus
        return int(min(total_cap, max(0, delta)))

    def _calc_cmd_xp(self, cfg: Dict[str, Any], ctx: commands.Context) -> int:
        # Determine module
        module = ""
        try:
            if ctx.command is not None and ctx.command.callback is not None:
                module = str(getattr(ctx.command.callback, "__module__", "") or "")
        except Exception:
            module = ""

        tier = _pick_cmd_tier(module)

        if tier == "system":
            return max(0, _safe_int(cfg, "cmd_xp_system", 0))
        if tier == "game":
            return max(0, _safe_int(cfg, "cmd_xp_game", 12))
        if tier == "chat":
            return max(0, _safe_int(cfg, "cmd_xp_chat", 8))
        if tier == "social":
            return max(0, _safe_int(cfg, "cmd_xp_social", 8))

        return max(0, _safe_int(cfg, "cmd_xp", 5))

    def _calc_interaction_xp(self, cfg: Dict[str, Any], interaction: discord.Interaction) -> int:
        # Component(button/select) vs Modal
        try:
            if interaction.type == discord.InteractionType.modal_submit:
                return max(0, _safe_int(cfg, "interaction_xp_modal", 3))
            # component interactions are the usual ones here
            return max(0, _safe_int(cfg, "interaction_xp_component", 2))
        except Exception:
            return max(0, _safe_int(cfg, "interaction_xp_component", 2))

    # -------------------------
    # Level-up announce
    # -------------------------

    def _pick_announce_target(
        self,
        cfg: Dict[str, Any],
        guild: discord.Guild,
        fallback: Optional[discord.abc.Messageable],
    ) -> Optional[discord.abc.Messageable]:
        target = fallback
        ann_ch = cfg.get("announce_channel_id")
        if ann_ch:
            ch = guild.get_channel(int(ann_ch))
            if ch is not None:
                target = ch
        return target

    def _get_static_banner_file(self) -> Optional[discord.File]:
        try:
            if os.path.exists(STATIC_BANNER_PATH):
                with open(STATIC_BANNER_PATH, "rb") as f:
                    data = f.read()
                bio = BytesIO(data)
                bio.seek(0)
                return discord.File(bio, filename="levelup.png")
        except Exception:
            return None
        return None

    async def _get_dynamic_banner_file(
        self,
        *,
        user: discord.abc.User,
        old_level: int,
        new_level: int,
    ) -> Optional[discord.File]:
        """Try to generate a personalized banner.

        This is optional; if Pillow is missing, we just return None.
        """

        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore
        except Exception:
            return None

        # base canvas
        W, H = 960, 240
        img = Image.new("RGBA", (W, H), (18, 18, 24, 255))
        dr = ImageDraw.Draw(img)

        # soft stripes
        for x in range(-H, W, 24):
            dr.line([(x, 0), (x + H, H)], fill=(30, 30, 44, 255), width=10)

        # avatar
        try:
            av_bytes = await user.display_avatar.read()  # type: ignore
            av = Image.open(BytesIO(av_bytes)).convert("RGBA")
            av = av.resize((160, 160))

            # circle mask
            mask = Image.new("L", (160, 160), 0)
            mdr = ImageDraw.Draw(mask)
            mdr.ellipse((0, 0, 160, 160), fill=255)
            img.paste(av, (32, 40), mask)
            # outline
            dr.ellipse((32, 40, 32 + 160, 40 + 160), outline=(235, 235, 245, 90), width=3)
        except Exception:
            pass

        # font selection
        font_paths = [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        font_big = None
        font_mid = None
        font_small = None
        for p in font_paths:
            try:
                if os.path.exists(p):
                    font_big = ImageFont.truetype(p, 48)
                    font_mid = ImageFont.truetype(p, 34)
                    font_small = ImageFont.truetype(p, 22)
                    break
            except Exception:
                continue
        if font_big is None:
            try:
                font_big = ImageFont.load_default()
                font_mid = ImageFont.load_default()
                font_small = ImageFont.load_default()
            except Exception:
                return None

        # text
        name = getattr(user, "display_name", None) or getattr(user, "name", "USER")
        dr.text((220, 44), "LEVEL UP", font=font_big, fill=(240, 240, 250, 255))
        dr.text((220, 112), str(name), font=font_mid, fill=(240, 240, 250, 220))
        dr.text((220, 162), f"Lv. {old_level}  →  Lv. {new_level}", font=font_small, fill=(240, 240, 250, 200))

        # export
        out = BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return discord.File(out, filename="levelup.png")

    async def _announce_levelup(
        self,
        *,
        cfg: Dict[str, Any],
        guild: discord.Guild,
        channel: Optional[discord.abc.Messageable],
        user: discord.abc.User,
        old_level: int,
        new_level: int,
        xp_into_level: int,
        xp_to_next: int,
        total_xp: int,
    ) -> None:
        if not int(cfg.get("announce_levelup", 1) or 0):
            return

        target = self._pick_announce_target(cfg, guild, channel)
        if target is None:
            return

        style = _safe_str(cfg, "announce_style", "banner").lower().strip()
        ping = int(cfg.get("announce_ping", 1) or 0)
        mention = user.mention if ping else ""

        # text-only
        if style in ("text", "plain"):
            try:
                msg = f"🎉 {mention} 레벨 {old_level} → {new_level}!"
                await target.send(msg.strip())
            except Exception as e:
                logger.debug("levelup announce failed(text): %s", e)
            return

        # banner
        file = await self._get_dynamic_banner_file(user=user, old_level=old_level, new_level=new_level)
        if file is None:
            file = self._get_static_banner_file()

        if file is None:
            # fallback
            try:
                msg = f"🎉 {mention} 레벨 {old_level} → {new_level}!"
                await target.send(msg.strip())
            except Exception as e:
                logger.debug("levelup announce failed(fallback): %s", e)
            return

        try:
            embed = discord.Embed(
                title="레벨업!",
                description=f"Lv. **{old_level}** → **{new_level}**\nXP **{xp_into_level}** / **{xp_to_next}**  (총 {total_xp})",
            )
            embed.set_image(url="attachment://levelup.png")
            embed.set_thumbnail(url=str(user.display_avatar.url))

            content = mention if mention else None
            await target.send(
                content=content,
                embed=embed,
                file=file,
                allowed_mentions=discord.AllowedMentions(users=bool(ping), roles=False, everyone=False),
            )
        except Exception as e:
            logger.debug("levelup announce failed(banner): %s", e)

    # -------------------------
    # Events
    # -------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 길드 채팅만
        if message.guild is None:
            return
        if message.author is None or getattr(message.author, "bot", False):
            return

        cfg = get_guild_xp_config(int(message.guild.id))
        if not int(cfg.get("enabled", 1) or 0):
            return

        member: Optional[discord.Member]
        if isinstance(message.author, discord.Member):
            member = message.author
        else:
            member = message.guild.get_member(int(message.author.id))

        if self._should_ignore(
            cfg,
            channel_id=int(message.channel.id),
            author=message.author,
            member=member,
        ):
            return

        # 커맨드 메시지는 on_command_completion에서 따로 처리
        if await _is_command_message(self.bot, message):
            return

        now = _now_ts()
        delta = self._calc_chat_xp(cfg, message, now)
        if delta <= 0:
            return

        res = add_user_xp(
            guild_id=int(message.guild.id),
            user_id=int(message.author.id),
            delta=int(delta),
            kind="chat",
            now=now,
        )

        if int(res.get("leveled_up", 0) or 0):
            await self._announce_levelup(
                cfg=cfg,
                guild=message.guild,
                channel=message.channel,
                user=message.author,
                old_level=int(res.get("before_level", 1) or 1),
                new_level=int(res.get("after_level", 1) or 1),
                xp_into_level=int(res.get("xp_into_level", 0) or 0),
                xp_to_next=int(res.get("xp_to_next", 0) or 0),
                total_xp=int(res.get("after_total_xp", 0) or 0),
            )

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        if ctx.author is None or getattr(ctx.author, "bot", False):
            return

        cfg = get_guild_xp_config(int(ctx.guild.id))
        if not int(cfg.get("enabled", 1) or 0):
            return

        member = ctx.author if isinstance(ctx.author, discord.Member) else ctx.guild.get_member(int(ctx.author.id))
        if self._should_ignore(
            cfg,
            channel_id=int(ctx.channel.id) if ctx.channel else 0,
            author=ctx.author,
            member=member,
        ):
            return

        now = _now_ts()
        delta = self._calc_cmd_xp(cfg, ctx)
        if delta <= 0:
            return

        res = add_user_xp(
            guild_id=int(ctx.guild.id),
            user_id=int(ctx.author.id),
            delta=int(delta),
            kind="cmd",
            now=now,
        )

        if int(res.get("leveled_up", 0) or 0):
            await self._announce_levelup(
                cfg=cfg,
                guild=ctx.guild,
                channel=ctx.channel,
                user=ctx.author,
                old_level=int(res.get("before_level", 1) or 1),
                new_level=int(res.get("after_level", 1) or 1),
                xp_into_level=int(res.get("xp_into_level", 0) or 0),
                xp_to_next=int(res.get("xp_to_next", 0) or 0),
                total_xp=int(res.get("after_total_xp", 0) or 0),
            )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # 버튼/모달 같은 상호작용도 "유메 이용"으로 간주
        if interaction.guild is None:
            return
        if interaction.user is None or getattr(interaction.user, "bot", False):
            return

        # 슬래시 명령(/유메전달)은 경험치 제외
        if interaction.type == discord.InteractionType.application_command:
            return

        cfg = get_guild_xp_config(int(interaction.guild.id))
        if not int(cfg.get("enabled", 1) or 0):
            return

        member = interaction.guild.get_member(int(interaction.user.id))

        ch_id = 0
        try:
            if interaction.channel is not None:
                ch_id = int(getattr(interaction.channel, "id", 0) or 0)
        except Exception:
            ch_id = 0

        if self._should_ignore(
            cfg,
            channel_id=ch_id,
            author=interaction.user,
            member=member,
        ):
            return

        now = _now_ts()
        delta = self._calc_interaction_xp(cfg, interaction)
        if delta <= 0:
            return

        res = add_user_xp(
            guild_id=int(interaction.guild.id),
            user_id=int(interaction.user.id),
            delta=int(delta),
            kind="interaction",
            now=now,
        )

        if int(res.get("leveled_up", 0) or 0):
            await self._announce_levelup(
                cfg=cfg,
                guild=interaction.guild,
                channel=interaction.channel,
                user=interaction.user,
                old_level=int(res.get("before_level", 1) or 1),
                new_level=int(res.get("after_level", 1) or 1),
                xp_into_level=int(res.get("xp_into_level", 0) or 0),
                xp_to_next=int(res.get("xp_to_next", 0) or 0),
                total_xp=int(res.get("after_total_xp", 0) or 0),
            )

    # -------------------------
    # Commands
    # -------------------------

    @commands.command(name="레벨")
    async def level_command(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """내 레벨/경험치를 확인."""

        if ctx.guild is None:
            await ctx.send("서버 안에서만 확인할 수 있어.", delete_after=6)
            return

        target = user or (ctx.author if isinstance(ctx.author, discord.Member) else None)
        if target is None:
            await ctx.send("대상을 찾을 수 없어.", delete_after=6)
            return

        st = get_user_xp_progress(int(ctx.guild.id), int(target.id))

        lvl = int(st.get("level", 1) or 1)
        total = int(st.get("total_xp", 0) or 0)
        in_lvl = int(st.get("xp_into_level", 0) or 0)
        need = int(st.get("xp_to_next", 0) or 0)

        await ctx.send(
            f"{target.mention} | Lv {lvl} | XP {in_lvl}/{need} (총 {total})",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @commands.command(name="랭킹")
    async def ranking_command(self, ctx: commands.Context, page: int = 1):
        """서버 경험치 랭킹(상위 10명)."""

        if ctx.guild is None:
            await ctx.send("서버 안에서만 볼 수 있어.", delete_after=6)
            return

        page = max(1, int(page))
        per_page = 10
        offset = (page - 1) * per_page

        rows = get_xp_leaderboard(int(ctx.guild.id), limit=per_page, offset=offset)
        if not rows:
            await ctx.send("랭킹 데이터가 없어.", delete_after=6)
            return

        lines: List[str] = []
        for i, r in enumerate(rows, start=offset + 1):
            uid = int(r.get("user_id", 0) or 0)
            lvl = int(r.get("level", 1) or 1)
            xp = int(r.get("total_xp", 0) or 0)

            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"{uid}"
            lines.append(f"{i}. {name}  |  Lv {lvl}  |  {xp} XP")

        embed = discord.Embed(title=f"경험치 랭킹 (page {page})", description="\n".join(lines))
        await ctx.send(embed=embed)

    # -------------------------
    # Admin helpers
    # -------------------------

    def _is_admin(self, ctx: commands.Context) -> bool:
        if ctx.author and int(getattr(ctx.author, "id", 0)) == DEV_USER_ID:
            return True
        if isinstance(ctx.author, discord.Member):
            return bool(ctx.author.guild_permissions.manage_guild)
        return False

    @commands.command(name="경험치")
    async def xp_config_command(self, ctx: commands.Context, mode: Optional[str] = None):
        """경험치 시스템 on/off 및 현재 설정 확인."""

        if ctx.guild is None:
            await ctx.send("서버 안에서만 설정할 수 있어.", delete_after=6)
            return

        cfg = get_guild_xp_config(int(ctx.guild.id))

        if mode is not None:
            if not self._is_admin(ctx):
                await ctx.send("권한이 없어.", delete_after=6)
                return

            m = mode.strip().lower()
            if m in ("on", "enable", "1", "켜기"):
                set_guild_xp_config(int(ctx.guild.id), enabled=1)
            elif m in ("off", "disable", "0", "끄기"):
                set_guild_xp_config(int(ctx.guild.id), enabled=0)
            else:
                await ctx.send("`!경험치 on` 또는 `!경험치 off`", delete_after=8)
                return

            cfg = get_guild_xp_config(int(ctx.guild.id))

        enabled = int(cfg.get("enabled", 1) or 0)
        ann = cfg.get("announce_channel_id")
        ann_s = f"<#{ann}>" if ann else "(현재 채널)"

        # short summary
        await ctx.send(
            f"경험치: {'ON' if enabled else 'OFF'} | 채팅 {cfg.get('chat_xp_min')}-{cfg.get('chat_xp_max')} | 커맨드 기본 {cfg.get('cmd_xp')} | 레벨업알림 {ann_s}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="경험치세부")
    async def xp_detail_command(self, ctx: commands.Context):
        """경험치 세부 설정 보기."""

        if ctx.guild is None:
            return

        cfg = get_guild_xp_config(int(ctx.guild.id))

        ann = cfg.get("announce_channel_id")
        ann_s = f"<#{ann}>" if ann else "(현재 채널)"

        msg = (
            f"채팅: {cfg.get('chat_xp_min')}-{cfg.get('chat_xp_max')} | 길이보너스 step={cfg.get('chat_len_step', 30)} cap={cfg.get('chat_len_cap', 10)} | 첨부+{cfg.get('chat_attach_bonus', 3)} | 링크+{cfg.get('chat_link_bonus', 0)} | 최소문자={cfg.get('chat_min_chars', 4)} | 반복윈도우={cfg.get('chat_repeat_window_sec', 5)} | 총cap={cfg.get('chat_total_cap', 50)}\n"
            f"커맨드: default={cfg.get('cmd_xp', 5)} | game={cfg.get('cmd_xp_game', 12)} | chat={cfg.get('cmd_xp_chat', 8)} | social={cfg.get('cmd_xp_social', 8)} | system={cfg.get('cmd_xp_system', 0)}\n"
            f"상호작용: component={cfg.get('interaction_xp_component', 2)} | modal={cfg.get('interaction_xp_modal', 3)}\n"
            f"레벨업: style={cfg.get('announce_style', 'banner')} | ping={cfg.get('announce_ping', 1)} | 채널={ann_s}"
        )
        await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="경험치설정")
    async def xp_set_command(self, ctx: commands.Context, key: Optional[str] = None, *values: str):
        """경험치 세부 설정 변경(관리자).

        사용:
          - !경험치설정                 -> 키 목록/현재값 보기
          - !경험치설정 <key> <value>    -> 값 변경

        주요 key:
          chat_min, chat_max, chat_len_step, chat_len_cap, chat_attach_bonus, chat_link_bonus,
          chat_min_chars, chat_repeat_window, chat_total_cap,
          cmd_default, cmd_game, cmd_chat, cmd_social, cmd_system,
          inter_component, inter_modal,
          announce_style(text/banner), announce_ping(0/1)
        """

        if ctx.guild is None:
            return
        if not self._is_admin(ctx):
            await ctx.send("권한이 없어.", delete_after=6)
            return

        cfg = get_guild_xp_config(int(ctx.guild.id))

        if not key:
            await ctx.send(
                "설정 키 예시: `!경험치설정 chat_min 10`, `!경험치설정 cmd_game 15`, `!경험치설정 announce_style banner`\n"
                "보기: `!경험치세부`",
                delete_after=12,
            )
            return

        k = key.strip().lower()
        v = " ".join(values).strip() if values else ""

        def _need_value() -> bool:
            return v == ""

        # chat
        if k in ("chat_min", "chat_xp_min"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_xp_min=int(v))
        elif k in ("chat_max", "chat_xp_max"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_xp_max=int(v))
        elif k in ("chat_len_step", "len_step"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_len_step=int(v))
        elif k in ("chat_len_cap", "len_cap"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_len_cap=int(v))
        elif k in ("chat_attach_bonus", "attach"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_attach_bonus=int(v))
        elif k in ("chat_link_bonus", "link"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_link_bonus=int(v))
        elif k in ("chat_min_chars", "min_chars"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_min_chars=int(v))
        elif k in ("chat_repeat_window", "repeat_window"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_repeat_window_sec=int(v))
        elif k in ("chat_total_cap", "total_cap"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), chat_total_cap=int(v))

        # command tiers
        elif k in ("cmd_default", "cmd"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), cmd_xp=int(v))
        elif k in ("cmd_game", "game"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), cmd_xp_game=int(v))
        elif k in ("cmd_chat", "chat"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), cmd_xp_chat=int(v))
        elif k in ("cmd_social", "social"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), cmd_xp_social=int(v))
        elif k in ("cmd_system", "system"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), cmd_xp_system=int(v))

        # interactions
        elif k in ("inter_component", "interaction_component"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), interaction_xp_component=int(v))
        elif k in ("inter_modal", "interaction_modal"):
            if _need_value():
                await ctx.send("값을 넣어줘.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), interaction_xp_modal=int(v))

        # announce
        elif k in ("announce_style", "style"):
            if _need_value():
                await ctx.send("text 또는 banner", delete_after=6)
                return
            val = v.strip().lower()
            if val not in ("text", "banner"):
                await ctx.send("text 또는 banner", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), announce_style=val)
        elif k in ("announce_ping", "ping"):
            if _need_value():
                await ctx.send("0 또는 1", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), announce_ping=int(v))
        else:
            await ctx.send("알 수 없는 key야. `!경험치세부`로 확인해줘.", delete_after=8)
            return

        cfg2 = get_guild_xp_config(int(ctx.guild.id))
        await ctx.send("설정 반영 완료. `!경험치세부`로 확인해줘.", delete_after=8)

    @commands.command(name="경험치채널", aliases=["레벨업채널"])
    async def xp_channel_command(self, ctx: commands.Context, *args: str):
        """레벨업 알림 채널 설정/조회.

        사용:
          - !경험치채널                  -> 현재 설정 보기
          - !경험치채널 here             -> 현재 채널로 설정
          - !경험치채널 #채널            -> 지정 채널로 설정(멘션)
          - !경험치채널 채널이름         -> 이름으로 찾기(정확/부분)
          - !경험치채널 off              -> 해제(현재 채널에 표시)
        """

        if ctx.guild is None:
            return
        if not self._is_admin(ctx):
            await ctx.send("권한이 없어.", delete_after=6)
            return

        cfg = get_guild_xp_config(int(ctx.guild.id))

        # 조회 모드
        if not args:
            ann = cfg.get("announce_channel_id")
            ann_s = f"<#{ann}>" if ann else "(현재 채널)"
            await ctx.send(
                f"레벨업 알림 채널: {ann_s}\n"
                f"설정: `!경험치채널 here` 또는 `!경험치채널 #채널` / 해제: `!경험치채널 off`",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        raw = " ".join(args).strip()
        low = raw.lower()

        # 해제: set_guild_xp_config는 announce_channel_id=None이면 '변경 없음'이므로 0으로 클리어한다.
        if low in ("off", "none", "0", "해제", "끄기"):
            set_guild_xp_config(int(ctx.guild.id), announce_channel_id=0)
            await ctx.send("레벨업 알림 채널: (현재 채널)", delete_after=6)
            return

        # 현재 채널로 지정
        if low in ("here", "this", "현재", "여기"):
            if ctx.channel is None:
                await ctx.send("현재 채널을 찾을 수 없어.", delete_after=6)
                return
            set_guild_xp_config(int(ctx.guild.id), announce_channel_id=int(ctx.channel.id))
            await ctx.send(f"레벨업 알림 채널: <#{ctx.channel.id}>", delete_after=6)
            return

        # 채널 파싱: 멘션(<#id>) / 숫자(id) / 이름
        ch = None
        m = re.search(r"<#(\d+)>", raw)
        if m:
            ch = ctx.guild.get_channel(int(m.group(1)))
        elif raw.isdigit():
            ch = ctx.guild.get_channel(int(raw))
        else:
            name = raw.lstrip("#").strip()
            # 정확 매칭 우선
            for c in ctx.guild.text_channels:
                if c.name == name:
                    ch = c
                    break
            # 부분 매칭
            if ch is None:
                for c in ctx.guild.text_channels:
                    if name and name in c.name:
                        ch = c
                        break

        if ch is None:
            await ctx.send("채널을 찾을 수 없어. `#채널` 멘션이나 채널 ID를 넣어줘.", delete_after=8)
            return

        set_guild_xp_config(int(ctx.guild.id), announce_channel_id=int(ch.id))
        await ctx.send(f"레벨업 알림 채널: <#{ch.id}>", delete_after=6)

    @commands.command(name="경험치초기화")
    async def xp_reset_command(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """유저 경험치 초기화(관리자)."""

        if ctx.guild is None:
            return
        if not self._is_admin(ctx):
            await ctx.send("권한이 없어.", delete_after=6)
            return

        if user is None:
            await ctx.send("대상을 멘션해줘.", delete_after=6)
            return

        reset_user_xp(int(ctx.guild.id), int(user.id))
        await ctx.send(f"초기화 완료: {user.mention}", delete_after=6)


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))
