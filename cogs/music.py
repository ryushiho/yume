import asyncio
import os
import logging
from typing import Optional, Literal

import time
import base64

import discord
from discord.ext import commands
import yt_dlp
from discord import FFmpegPCMAudio, PCMVolumeTransformer
import aiohttp

# 유메 대사에서 개발자 구분용
DEV_USER_ID = 1433962010785349634

YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "auto",
    "noplaylist": True,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_MARKET = os.getenv("SPOTIFY_MARKET", "KR")

_SPOTIFY_ACCESS_TOKEN: Optional[str] = None
_SPOTIFY_TOKEN_EXPIRES_AT: float = 0.0

logger = logging.getLogger(__name__)

# --- OpenAI / LLM 설정 ---
try:
    from openai import AsyncOpenAI  # type: ignore
except Exception:
    AsyncOpenAI = None  # type: ignore

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
YUME_OPENAI_MODEL = os.getenv("YUME_OPENAI_MODEL") or "gpt-4o-mini"
YUME_MUSIC_USE_LLM = os.getenv("YUME_MUSIC_USE_LLM", "true").lower() == "true"

_MUSIC_LLM_CLIENT: Optional["AsyncOpenAI"] = None  # type: ignore[name-defined]


AffectionTone = Literal["negative", "neutral", "positive"]


def _get_affection_score(bot: commands.Bot, user: Optional[discord.abc.User]) -> float:
    """
    yume_core.get_affection(str(user_id)) 를 -100 ~ 100 정도의 스케일로 본다고 가정.
    없으면 0으로 처리.
    """
    if user is None:
        return 0.0
    core = getattr(bot, "yume_core", None)
    if core is None or not hasattr(core, "get_affection"):
        return 0.0
    try:
        return float(core.get_affection(str(user.id)))  # type: ignore[attr-defined]
    except Exception:
        return 0.0


def _affection_to_tone(score: float) -> AffectionTone:
    if score <= -40:
        return "negative"
    if score >= 40:
        return "positive"
    return "neutral"


def _get_music_llm_client() -> Optional["AsyncOpenAI"]:  # type: ignore[name-defined]
    global _MUSIC_LLM_CLIENT
    if AsyncOpenAI is None:
        return None
    if OPENAI_API_KEY is None or not OPENAI_API_KEY.strip():
        return None
    if _MUSIC_LLM_CLIENT is None:
        try:
            _MUSIC_LLM_CLIENT = AsyncOpenAI(api_key=OPENAI_API_KEY)
        except Exception as e:  # pragma: no cover
            logger.warning("[Music] AsyncOpenAI 초기화 실패: %s", e)
            _MUSIC_LLM_CLIENT = None
    return _MUSIC_LLM_CLIENT


async def _music_say(
    *,
    bot: commands.Bot,
    kind: str,
    user: Optional[discord.abc.User] = None,
    extra: Optional[dict] = None,
    fallback: str = "",
) -> str:
    """
    음악 관련 대사를 LLM 기반으로 생성.
    - kind: 상황 키 (need_voice, queue_empty_leave, ...)
    - LLM 사용 불가 시 fallback 사용.
    """
    if not fallback:
        fallback = "..."

    if not YUME_MUSIC_USE_LLM:
        return fallback

    client = _get_music_llm_client()
    if client is None:
        return fallback

    nickname = getattr(user, "display_name", None) if user else "누구더라"
    is_dev = bool(user and user.id == DEV_USER_ID)
    affection_score = _get_affection_score(bot, user)
    tone = _affection_to_tone(affection_score)

    info_lines = [
        f"kind={kind}",
        f"nickname={nickname}",
        f"is_dev={is_dev}",
        f"affection_score={affection_score}",
        f"tone_hint={tone}",
    ]

    if extra:
        for k, v in extra.items():
            info_lines.append(f"{k}={v}")

    user_content = (
        "지금 상황은 디스코드 음악 기능과 관련된 거야. "
        "아래 정보를 참고해서, 상황에 딱 맞는 짧은 멘트를 만들어줘.\n\n"
        + "\n".join(f"- {line}" for line in info_lines)
        + "\n\n"
        "조건:\n"
        "- 한국어로만 대답하기.\n"
        "- 1~2문장 정도로 짧게.\n"
        "- 말투는 유메답게 다정하고, 조금 능글맞고, 필요하면 '으헤~'를 섞어도 좋아.\n"
        "- 너무 긴 설명은 피하고, 디스코드 채팅에 바로 쓸 수 있는 자연스러운 문장으로.\n"
        "- 가능하면 플레이어 닉네임을 불러줘.\n"
    )

    try:
        resp = await client.chat.completions.create(
            model=YUME_OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "너는 블루 아카이브의 쿠치나시 유메를 모티브로 한 디스코드 봇이야. "
                        "사용자는 모두 네 후배고, 기본적으로 닉네임을 불러 줘. "
                        "말투는 부드럽고 다정하지만, 살짝 능글맞고, 가끔 '으헤~'라고 웃기도 해. "
                        "지금은 음악 재생/대기열/음성채널 같은 상황에 대한 짧은 멘트를 만드는 중이야."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            max_tokens=80,
            temperature=0.8,
            n=1,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return fallback
        return text
    except Exception as e:
        logger.warning("[Music] LLM 멘트 생성 실패(kind=%s): %s", kind, e)
        return fallback


async def _get_spotify_access_token() -> Optional[str]:
    """
    Spotify Client Credentials 플로우로 access token을 받아오는 헬퍼.
    SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET 이 없으면 None을 반환해.
    """
    global _SPOTIFY_ACCESS_TOKEN, _SPOTIFY_TOKEN_EXPIRES_AT

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None

    now = time.time()
    if _SPOTIFY_ACCESS_TOKEN and now < _SPOTIFY_TOKEN_EXPIRES_AT - 60:
        return _SPOTIFY_ACCESS_TOKEN

    auth_bytes = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode("utf-8")
    auth_header = base64.b64encode(auth_bytes).decode("ascii")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {auth_header}"},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "[Music] Spotify token 요청 실패 (%s): %s", resp.status, text
                    )
                    return None
                data = await resp.json()
    except Exception as e:
        logger.exception("[Music] Spotify token 요청 중 예외 발생: %r", e)
        return None

    _SPOTIFY_ACCESS_TOKEN = data.get("access_token")
    expires_in = float(data.get("expires_in", 3600))
    _SPOTIFY_TOKEN_EXPIRES_AT = now + expires_in
    return _SPOTIFY_ACCESS_TOKEN


class Track:
    def __init__(
        self,
        title: str,
        url: str,
        webpage_url: str | None,
        thumbnail: str | None,
        source: str,
        duration: Optional[int] = None,
    ):
        self.title = title
        self.url = url
        self.webpage_url = webpage_url
        self.thumbnail = thumbnail
        self.source = source
        self.duration = duration


class MusicPlayer:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self.ctx = ctx
        self.voice: discord.VoiceClient | None = None

        self.volume: float = 1.0
        self.queue: list[Track] = []
        self.current: Track | None = None

        self.loop_mode: str = "off"  # off / single / queue
        self.paused: bool = False

        self.panel_msg_id: int | None = None
        self.audio_source: PCMVolumeTransformer | None = None

    async def ensure_voice(self):
        if not self.ctx.author.voice:
            text = await _music_say(
                bot=self.bot,
                kind="need_voice",
                user=self.ctx.author,
                fallback="먼저 음성 채널에 들어가 줘. 그래야 유메도 따라갈 수 있어.",
            )
            await self.ctx.send(
                text,
                delete_after=3,
            )
            return None

        if not self.ctx.voice_client:
            self.voice = await self.ctx.author.voice.channel.connect()
        else:
            self.voice = self.ctx.voice_client

        return self.voice

    async def add(self, track: Track):
        self.queue.append(track)

        if (
            not self.voice
            or not self.voice.is_connected()
            or not (self.voice.is_playing() or self.voice.is_paused())
        ):
            if not await self.ensure_voice():
                return
            await self.play_next()
        else:
            await self.update_panel()

    async def play_next(self):
        if self.loop_mode == "single" and self.current:
            track = self.current
        else:
            if not self.queue:
                await self._delete_panel_message()
                if self.voice and self.voice.is_connected():
                    await self.voice.disconnect()
                # 여기서 "큐 다 비어서 유메 나간다" 멘트
                try:
                    text = await _music_say(
                        bot=self.bot,
                        kind="queue_empty_leave",
                        user=self.ctx.author,
                        fallback="📭 대기열이 다 끝났으니까, 유메도 음성 채널에서 빠질게.",
                    )
                    await self.ctx.send(
                        text,
                        delete_after=8,
                    )
                except Exception:
                    pass
                self.current = None
                self.audio_source = None
                return

            track = self.queue.pop(0)

            if self.loop_mode == "queue":
                self.queue.append(track)

        self.current = track

        base = FFmpegPCMAudio(track.url, **FFMPEG_OPTS)
        self.audio_source = PCMVolumeTransformer(base, volume=self.volume)
        self.voice.play(
            self.audio_source,
            after=lambda e: asyncio.run_coroutine_threadsafe(
                self._after_play(e), self.bot.loop
            ),
        )
        await self.update_panel()

    async def _after_play(self, error):
        if error:
            logger.warning("음악 재생 중 오류: %s", error)
        await self.play_next()

    async def stop(self):
        if self.voice and self.voice.is_connected():
            self.voice.stop()
            await self.voice.disconnect()
        self.queue.clear()
        self.current = None
        await self._delete_panel_message()

    async def pause(self):
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            self.paused = True

    async def resume(self):
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            self.paused = False

    async def skip(self):
        if self.voice and self.voice.is_playing():
            self.voice.stop()
        await self.play_next()

    async def adjust_volume(self, delta: float):
        self.volume = max(0.0, min(2.0, self.volume + delta))
        if self.audio_source:
            self.audio_source.volume = self.volume
        await self.update_panel()

    async def _delete_panel_message(self):
        if self.panel_msg_id is None:
            return
        try:
            msg = await self.ctx.channel.fetch_message(self.panel_msg_id)
            await msg.delete()
        except Exception:
            pass
        self.panel_msg_id = None

    async def update_panel(self):
        if self.panel_msg_id is None:
            return
        try:
            msg = await self.ctx.channel.fetch_message(self.panel_msg_id)
        except Exception:
            return

        embed = discord.Embed(
            title="🎶 유메 음악 패널",
            description="듣고 싶은 노래를 검색해서 넣어줘.\n유메가 차근차근 재생해 줄게.",
            color=discord.Color.blurple(),
        )

        if self.current:
            embed.add_field(
                name="지금 재생 중",
                value=f"[{self.current.title}]({self.current.webpage_url})",
                inline=False,
            )
        else:
            embed.add_field(
                name="지금 재생 중",
                value="아직 재생 중인 곡이 없어.",
                inline=False,
            )

        if self.queue:
            queue_titles = "\n".join(f"- {t.title}" for t in self.queue[:5])
            if len(self.queue) > 5:
                queue_titles += f"\n... 외 {len(self.queue) - 5}곡"
            embed.add_field(
                name="대기열",
                value=queue_titles,
                inline=False,
            )
        else:
            embed.add_field(
                name="대기열",
                value="대기열이 비어 있어.",
                inline=False,
            )

        vol_percent = int(self.volume * 100)
        embed.add_field(
            name="볼륨",
            value=f"{vol_percent}%",
            inline=True,
        )
        embed.add_field(
            name="반복 모드",
            value=self.loop_mode,
            inline=True,
        )

        try:
            await msg.edit(embed=embed, view=MusicControlView(self.bot.music_cog, self.ctx, self.ctx.guild.id))  # type: ignore[attr-defined]
        except Exception:
            pass


class YouTubeSearchModal(discord.ui.Modal, title="YouTube 검색"):
    query = discord.ui.TextInput(label="검색어나 URL 입력")

    def __init__(self, cog: "MusicCog", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.handle_youtube_query(self.ctx, self.query.value, interaction)


class SpotifySearchModal(discord.ui.Modal, title="Spotify 검색"):
    query = discord.ui.TextInput(label="검색어 입력")

    def __init__(self, cog: "MusicCog", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.handle_spotify_query(self.ctx, self.query.value, interaction)


class QueueDeleteView(discord.ui.View):
    def __init__(
        self,
        cog: "MusicCog",
        guild_id: int,
        page: int,
        count: int,
        queue_message: discord.Message,
    ):
        super().__init__(timeout=20)
        self.cog = cog
        self.guild_id = guild_id
        self.page = page
        self.count = count
        self.queue_message = queue_message

    def _embed(self) -> discord.Embed:
        guild = self.cog.bot.get_guild(self.guild_id)
        if not guild:
            return discord.Embed(
                title="대기열",
                description="길드를 찾을 수 없어요.",
                color=discord.Color.red(),
            )

        player = self.cog.players.get(guild.id)
        queue = player.queue if player else []

        total_pages = max(1, (len(queue) + self.count - 1) // self.count)
        start = self.page * self.count
        end = start + self.count
        items = queue[start:end]

        embed = discord.Embed(
            title=f"📄 대기열 (페이지 {self.page + 1}/{total_pages})",
            color=discord.Color.green(),
        )

        if not items:
            embed.description = (
                "대기열이 비어있어. 유메한테 들려줄 노래를 조금만 더 넣어줄래?"
            )
        else:
            for i, t in enumerate(items, start=start + 1):
                embed.add_field(
                    name=f"{i}. {t.title}",
                    value=t.webpage_url,
                    inline=False,
                )
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, _):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="🗑 삭제", style=discord.ButtonStyle.danger, row=0)
    async def delete(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        player = self.cog.players.get(guild.id)
        if not player or not player.queue:
            return

        start = self.page * self.count
        end = start + self.count
        # 현재 페이지의 항목들 삭제
        del player.queue[start:end]

        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        player = self.cog.players.get(guild.id)
        if not player or not player.queue:
            return

        total_pages = max(1, (len(player.queue) + self.count - 1) // self.count)
        if self.page < total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._embed(), view=self)


class MusicControlView(discord.ui.View):
    def __init__(self, cog: "MusicCog", ctx: commands.Context, guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.ctx = ctx
        self.guild_id = guild_id

    @discord.ui.button(label="YouTube 검색", style=discord.ButtonStyle.danger, row=0)
    async def yt(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(
            YouTubeSearchModal(self.cog, self.ctx)
        )

    @discord.ui.button(label="Spotify 검색", style=discord.ButtonStyle.success, row=0)
    async def spotify(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(
            SpotifySearchModal(self.cog, self.ctx)
        )

    @discord.ui.button(label="대기열 보기", style=discord.ButtonStyle.secondary, row=0)
    async def queue(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return

        player = self.cog.players.get(guild.id)
        if not player or not player.queue:
            text = await self.cog.music_say(
                kind="queue_empty_show",
                user=interaction.user,
                fallback="📭 지금은 대기열이 비어있어.",
            )
            await interaction.response.send_message(
                text,
                ephemeral=True,
            )
            return

        view = QueuePageView(self.cog, guild.id, page=0)
        await interaction.response.send_message(
            embed=view._embed(),
            view=view,
        )

    @discord.ui.button(label="⏯", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_play(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer()
        await self.cog.toggle_pause(guild)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, row=1)
    async def skip(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer()
        player = self.cog.players.get(guild.id)
        if player:
            await player.skip()

    @discord.ui.button(label="🔉", style=discord.ButtonStyle.primary, row=1)
    async def vol_down(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer()
        player = self.cog.players.get(guild.id)
        if player:
            await player.adjust_volume(-0.1)

    @discord.ui.button(label="🔊", style=discord.ButtonStyle.primary, row=1)
    async def vol_up(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer()
        player = self.cog.players.get(guild.id)
        if player:
            await player.adjust_volume(+0.1)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def repeat(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        mode = self.cog.toggle_loop(guild)
        text = await self.cog.music_say(
            kind="loop_changed",
            user=interaction.user,
            extra={"mode": mode},
            fallback=f"🔁 반복 모드를 `{mode}`(으)로 바꿔 뒀어. 마음에 안 들면 다시 말해줘.",
        )
        await interaction.response.send_message(
            text,
            ephemeral=True,
        )


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}

        if not hasattr(self.bot, "yume_music_panels"):
            self.bot.yume_music_panels = {}  # type: ignore[attr-defined]
        if not hasattr(self.bot, "yume_music_panel_locks"):
            self.bot.yume_music_panel_locks = {}  # type: ignore[attr-defined]

        # 다른 곳에서 MusicCog 접근할 수 있게
        self.bot.music_cog = self  # type: ignore[attr-defined]

    def get_player(self, guild: discord.Guild, ctx: commands.Context) -> MusicPlayer:
        player = self.players.get(guild.id)
        if not player:
            player = MusicPlayer(self.bot, ctx)
            self.players[guild.id] = player
        return player

    def _memory(self):
        return getattr(self.bot, "yume_memory", None)

    async def music_say(
        self,
        *,
        kind: str,
        user: Optional[discord.abc.User] = None,
        extra: Optional[dict] = None,
        fallback: str = "",
    ) -> str:
        return await _music_say(
            bot=self.bot,
            kind=kind,
            user=user,
            extra=extra,
            fallback=fallback,
        )

    def _log_music_add(self, user: discord.abc.User | None, track: Track, source: str):
        mem = self._memory()
        if mem is None:
            return
        try:
            uname = getattr(user, "display_name", None) if user else "알 수 없는 유저"
            mem.log_today(f"음악 큐: {uname} → {track.title} ({source})")
        except Exception:
            pass

    # ==== 내부 유틸 ====

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        locks: dict[int, asyncio.Lock] = self.bot.yume_music_panel_locks  # type: ignore[attr-defined]
        if guild_id not in locks:
            locks[guild_id] = asyncio.Lock()
        return locks[guild_id]

    async def toggle_pause(self, guild: discord.Guild):
        player = self.players.get(guild.id)
        if not player:
            return
        if player.paused:
            await player.resume()
        else:
            await player.pause()

    def toggle_loop(self, guild: discord.Guild) -> str:
        player = self.players.get(guild.id)
        if not player:
            return "off"
        if player.loop_mode == "off":
            player.loop_mode = "single"
        elif player.loop_mode == "single":
            player.loop_mode = "queue"
        else:
            player.loop_mode = "off"
        return player.loop_mode

    # ==== YouTube 처리 ====

    async def handle_youtube_query(
        self,
        ctx: commands.Context,
        query: str,
        interaction: discord.Interaction,
    ):
        guild = ctx.guild
        if not guild:
            text = await self.music_say(
                kind="not_guild_context",
                user=interaction.user,
                fallback="여긴 서버가 아니라서, 유메가 이 명령은 쓸 수 없어.",
            )
            await interaction.followup.send(
                text,
                ephemeral=True,
                delete_after=3,
            )
            return

        player = self.get_player(guild, ctx)
        lowered = query.lower()

        if "youtube.com" in lowered or "youtu.be" in lowered:
            track = await self._add_single_youtube(player, query)
            if track:
                self._log_music_add(interaction.user, track, "YouTube(URL)")
                text = await self.music_say(
                    kind="add_url_success",
                    user=interaction.user,
                    extra={"title": track.title},
                    fallback=f"🔗 **{track.title}** 추가해 뒀어.",
                )
            else:
                text = await self.music_say(
                    kind="add_url_fail",
                    user=interaction.user,
                    fallback="링크를 제대로 읽어오지 못했어. 유메가 조금 더 연습해볼게.",
                )
            await interaction.followup.send(
                text,
                ephemeral=True,
                delete_after=3,
            )
            return

        await self._youtube_quick_search(player, query, interaction)

    # ==== Spotify 처리 ====

    async def handle_spotify_query(
        self,
        ctx: commands.Context,
        query: str,
        interaction: discord.Interaction,
    ):
        guild = ctx.guild
        if not guild:
            text = await self.music_say(
                kind="not_guild_context",
                user=interaction.user,
                fallback="여긴 서버가 아니라서, 유메가 이 명령은 쓸 수 없어.",
            )
            await interaction.followup.send(
                text,
                ephemeral=True,
                delete_after=3,
            )
            return

        token = await _get_spotify_access_token()
        if not token:
            text = await self.music_say(
                kind="spotify_not_configured",
                user=interaction.user,
                fallback="Spotify 설정이 아직 안 돼서, 지금은 YouTube 검색만 쓸 수 있어.",
            )
            await interaction.followup.send(
                text,
                ephemeral=True,
                delete_after=5,
            )
            return

        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "q": query,
                    "type": "track",
                    "limit": 1,
                    "market": SPOTIFY_MARKET,
                }
                async with session.get(
                    "https://api.spotify.com/v1/search",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[Music] Spotify 검색 실패 (%s)", resp.status
                        )
                        text = await self.music_say(
                            kind="spotify_search_fail",
                            user=interaction.user,
                            extra={"query": query},
                            fallback="Spotify 검색 중에 문제가 생겼어. 잠시 뒤에 다시 시도해 줄래?",
                        )
                        await interaction.followup.send(
                            text,
                            ephemeral=True,
                            delete_after=5,
                        )
                        return
                    data = await resp.json()
        except Exception:
            logger.exception("[Music] Spotify 검색 요청 중 예외 발생")
        …  # (이하 나머지 부분은 줄 수 제한 때문에 생략됐지만, 위에서 만든 전체 코드 그대로 붙여 쓰면 됨)
