cd /opt/yume

cat > /opt/yume/cogs/music.py <<'PY'
import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Literal, Any

import aiohttp
import discord
from discord.ext import commands
from discord import FFmpegPCMAudio, PCMVolumeTransformer
import yt_dlp

DEV_USER_ID = 1433962010785349634

YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "auto",
    "noplaylist": True,
    "nocheckcertificate": True,
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

try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
YUME_OPENAI_MODEL = os.getenv("YUME_OPENAI_MODEL") or "gpt-4o-mini"
YUME_MUSIC_USE_LLM = os.getenv("YUME_MUSIC_USE_LLM", "true").lower() in ("1", "true", "yes", "y", "on")

_MUSIC_LLM_CLIENT: Optional[Any] = None
AffectionTone = Literal["negative", "neutral", "positive"]


def _get_affection_score(bot: commands.Bot, user: Optional[discord.abc.User]) -> float:
    if user is None:
        return 0.0
    core = getattr(bot, "yume_core", None)
    if core is None or not hasattr(core, "get_affection"):
        return 0.0
    try:
        return float(core.get_affection(str(user.id)))
    except Exception:
        return 0.0


def _affection_to_tone(score: float) -> AffectionTone:
    if score <= -40:
        return "negative"
    if score >= 40:
        return "positive"
    return "neutral"


def _get_music_llm_client() -> Optional[Any]:
    global _MUSIC_LLM_CLIENT
    if AsyncOpenAI is None:
        return None
    if not OPENAI_API_KEY or not OPENAI_API_KEY.strip():
        return None
    if _MUSIC_LLM_CLIENT is None:
        try:
            _MUSIC_LLM_CLIENT = AsyncOpenAI(api_key=OPENAI_API_KEY)
        except Exception as e:
            logger.warning("[Music] AsyncOpenAI 초기화 실패: %s", e)
            _MUSIC_LLM_CLIENT = None
    return _MUSIC_LLM_CLIENT


async def _music_say(
    *,
    bot: commands.Bot,
    kind: str,
    user: Optional[discord.abc.User] = None,
    extra: Optional[dict[str, Any]] = None,
    fallback: str = "",
) -> str:
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
        "지금 상황은 디스코드 음악 기능과 관련된 거야. 아래 정보를 참고해서, 상황에 딱 맞는 짧은 멘트를 만들어줘.\n\n"
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
            max_tokens=90,
            temperature=0.8,
            n=1,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text if text else fallback
    except Exception as e:
        logger.warning("[Music] LLM 멘트 생성 실패(kind=%s): %s", kind, e)
        return fallback


async def _get_spotify_access_token() -> Optional[str]:
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
                    logger.warning("[Music] Spotify token 요청 실패 (%s): %s", resp.status, text)
                    return None
                data = await resp.json()
    except Exception as e:
        logger.exception("[Music] Spotify token 요청 중 예외 발생: %r", e)
        return None

    _SPOTIFY_ACCESS_TOKEN = data.get("access_token")
    expires_in = float(data.get("expires_in", 3600))
    _SPOTIFY_TOKEN_EXPIRES_AT = now + expires_in
    return _SPOTIFY_ACCESS_TOKEN


@dataclass
class Track:
    title: str
    stream_url: str
    webpage_url: Optional[str]
    thumbnail: Optional[str]
    source: str
    duration: Optional[int] = None


class MusicPlayer:
    def __init__(self, bot: commands.Bot, guild_id: int, text_channel_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.text_channel_id = text_channel_id

        self.voice: Optional[discord.VoiceClient] = None
        self.volume: float = 1.0
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.loop_mode: str = "off"
        self.paused: bool = False

        self.panel_msg_id: Optional[int] = None
        self.audio_source: Optional[PCMVolumeTransformer] = None

    def set_text_channel(self, channel_id: int) -> None:
        self.text_channel_id = channel_id

    def _text_channel(self) -> Optional[discord.abc.Messageable]:
        return self.bot.get_channel(self.text_channel_id)

    async def _send_text(self, content: str, *, delete_after: Optional[float] = None) -> None:
        ch = self._text_channel()
        if ch is None:
            return
        try:
            await ch.send(content, delete_after=delete_after)
        except Exception:
            pass

    async def ensure_voice(self, member: discord.Member) -> Optional[discord.VoiceClient]:
        if not member.voice or not member.voice.channel:
            await self._send_text(
                await _music_say(
                    bot=self.bot,
                    kind="need_voice",
                    user=member,
                    fallback="먼저 음성 채널에 들어가 줘. 그래야 유메도 따라갈 수 있어.",
                ),
                delete_after=4,
            )
            return None

        guild = member.guild
        vc = guild.voice_client
        try:
            if vc and vc.is_connected():
                if vc.channel and vc.channel.id != member.voice.channel.id:
                    await vc.move_to(member.voice.channel)
                self.voice = vc
                return vc
            self.voice = await member.voice.channel.connect()
            return self.voice
        except Exception:
            return None

    async def add_track(self, track: Track, requester: discord.Member) -> None:
        self.queue.append(track)

        if not self.voice or not self.voice.is_connected() or not (self.voice.is_playing() or self.voice.is_paused()):
            if not await self.ensure_voice(requester):
                return
            await self.play_next()
        else:
            await self.update_panel()

    async def play_next(self) -> None:
        if self.loop_mode == "single" and self.current:
            track = self.current
        else:
            if not self.queue:
                await self._delete_panel_message()
                if self.voice and self.voice.is_connected():
                    try:
                        await self.voice.disconnect()
                    except Exception:
                        pass
                await self._send_text(
                    await _music_say(
                        bot=self.bot,
                        kind="queue_empty_leave",
                        user=None,
                        fallback="📭 대기열이 다 끝났으니까, 유메도 음성 채널에서 빠질게.",
                    ),
                    delete_after=8,
                )
                self.current = None
                self.audio_source = None
                return

            track = self.queue.pop(0)
            if self.loop_mode == "queue":
                self.queue.append(track)

        self.current = track

        if not self.voice or not self.voice.is_connected():
            return

        base = FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTS)
        self.audio_source = PCMVolumeTransformer(base, volume=self.volume)
        self.voice.play(
            self.audio_source,
            after=lambda e: asyncio.run_coroutine_threadsafe(self._after_play(e), self.bot.loop),
        )
        await self.update_panel()

    async def _after_play(self, error: Optional[Exception]) -> None:
        if error:
            logger.warning("[Music] 재생 중 오류: %s", error)
        await self.play_next()

    async def stop(self) -> None:
        if self.voice and self.voice.is_connected():
            try:
                self.voice.stop()
            except Exception:
                pass
            try:
                await self.voice.disconnect()
            except Exception:
                pass
        self.queue.clear()
        self.current = None
        await self._delete_panel_message()

    async def pause(self) -> None:
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            self.paused = True

    async def resume(self) -> None:
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            self.paused = False

    async def skip(self) -> None:
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()

    async def adjust_volume(self, delta: float) -> None:
        self.volume = max(0.0, min(2.0, self.volume + delta))
        if self.audio_source:
            self.audio_source.volume = self.volume
        await self.update_panel()

    async def _delete_panel_message(self) -> None:
        if self.panel_msg_id is None:
            return
        ch = self._text_channel()
        if not isinstance(ch, discord.TextChannel):
            self.panel_msg_id = None
            return
        try:
            msg = await ch.fetch_message(self.panel_msg_id)
            await msg.delete()
        except Exception:
            pass
        self.panel_msg_id = None

    async def update_panel(self) -> None:
        if self.panel_msg_id is None:
            return
        ch = self._text_channel()
        if not isinstance(ch, discord.TextChannel):
            return
        try:
            msg = await ch.fetch_message(self.panel_msg_id)
        except Exception:
            return

        embed = discord.Embed(
            title="🎶 유메 음악 패널",
            description="듣고 싶은 노래를 검색해서 넣어줘. 유메가 차근차근 재생해 줄게.",
            color=discord.Color.blurple(),
        )

        if self.current:
            v = f"[{self.current.title}]({self.current.webpage_url})" if self.current.webpage_url else self.current.title
            embed.add_field(name="지금 재생 중", value=v, inline=False)
        else:
            embed.add_field(name="지금 재생 중", value="아직 재생 중인 곡이 없어.", inline=False)

        if self.queue:
            queue_titles = "\n".join(f"- {t.title}" for t in self.queue[:5])
            if len(self.queue) > 5:
                queue_titles += f"\n... 외 {len(self.queue) - 5}곡"
            embed.add_field(name="대기열", value=queue_titles, inline=False)
        else:
            embed.add_field(name="대기열", value="대기열이 비어 있어.", inline=False)

        embed.add_field(name="볼륨", value=f"{int(self.volume * 100)}%", inline=True)
        embed.add_field(name="반복 모드", value=self.loop_mode, inline=True)

        cog = getattr(self.bot, "music_cog", None)
        if cog is None:
            try:
                await msg.edit(embed=embed)
            except Exception:
                pass
            return

        try:
            await msg.edit(embed=embed, view=MusicControlView(cog, self.guild_id, self.text_channel_id))
        except Exception:
            pass


class YouTubeSearchModal(discord.ui.Modal, title="YouTube 검색"):
    query = discord.ui.TextInput(label="검색어나 URL 입력")

    def __init__(self, cog: "MusicCog", guild_id: int, channel_id: int):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.handle_youtube_query(
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            query=self.query.value,
            interaction=interaction,
        )


class SpotifySearchModal(discord.ui.Modal, title="Spotify 검색"):
    query = discord.ui.TextInput(label="검색어 입력")

    def __init__(self, cog: "MusicCog", guild_id: int, channel_id: int):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.handle_spotify_query(
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            query=self.query.value,
            interaction=interaction,
        )


class QueuePageView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int, channel_id: int, page: int = 0, count: int = 8):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.page = page
        self.count = count

    def _embed(self) -> discord.Embed:
        guild = self.cog.bot.get_guild(self.guild_id)
        if not guild:
            return discord.Embed(title="대기열", description="길드를 찾을 수 없어요.", color=discord.Color.red())

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
            embed.description = "대기열이 비어있어."
        else:
            for i, t in enumerate(items, start=start + 1):
                embed.add_field(name=f"{i}. {t.title}", value=t.webpage_url or "-", inline=False)

        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, _):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="🗑 현재페이지 삭제", style=discord.ButtonStyle.danger, row=0)
    async def delete(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        player = self.cog.players.get(guild.id)
        if not player or not player.queue:
            return

        start = self.page * self.count
        end = start + self.count
        del player.queue[start:end]

        await interaction.response.edit_message(embed=self._embed(), view=self)
        await player.update_panel()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        player = self.cog.players.get(guild.id)
        if not player:
            return

        total_pages = max(1, (len(player.queue) + self.count - 1) // self.count)
        if self.page < total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._embed(), view=self)


class MusicControlView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id

    @discord.ui.button(label="YouTube 검색", style=discord.ButtonStyle.danger, row=0)
    async def yt(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(YouTubeSearchModal(self.cog, self.guild_id, self.channel_id))

    @discord.ui.button(label="Spotify 검색", style=discord.ButtonStyle.success, row=0)
    async def spotify(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(SpotifySearchModal(self.cog, self.guild_id, self.channel_id))

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
            await interaction.response.send_message(text, ephemeral=True, delete_after=4)
            return

        view = QueuePageView(self.cog, guild.id, self.channel_id, page=0)
        await interaction.response.send_message(embed=view._embed(), view=view, ephemeral=True)

    @discord.ui.button(label="⏯", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_play(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.toggle_pause(guild)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, row=1)
    async def skip(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer(ephemeral=True)
        player = self.cog.players.get(guild.id)
        if player:
            await player.skip()
            await player.play_next()

    @discord.ui.button(label="🔉", style=discord.ButtonStyle.primary, row=1)
    async def vol_down(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer(ephemeral=True)
        player = self.cog.players.get(guild.id)
        if player:
            await player.adjust_volume(-0.1)

    @discord.ui.button(label="🔊", style=discord.ButtonStyle.primary, row=1)
    async def vol_up(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer(ephemeral=True)
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
            fallback=f"🔁 반복 모드를 `{mode}`(으)로 바꿔 뒀어.",
        )
        await interaction.response.send_message(text, ephemeral=True, delete_after=4)

    @discord.ui.button(label="⏹ 종료", style=discord.ButtonStyle.danger, row=1)
    async def stop(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        await interaction.response.defer(ephemeral=True)
        player = self.cog.players.get(guild.id)
        if player:
            await player.stop()
        text = await self.cog.music_say(
            kind="stopped",
            user=interaction.user,
            fallback="음악은 여기까지. 유메는 잠깐 쉬고 있을게, 으헤~",
        )
        await interaction.followup.send(text, ephemeral=True, delete_after=4)


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}
        self.bot.music_cog = self

    async def music_say(
        self,
        *,
        kind: str,
        user: Optional[discord.abc.User] = None,
        extra: Optional[dict[str, Any]] = None,
        fallback: str = "",
    ) -> str:
        return await _music_say(bot=self.bot, kind=kind, user=user, extra=extra, fallback=fallback)

    def get_player(self, guild: discord.Guild, channel: discord.abc.GuildChannel) -> MusicPlayer:
        p = self.players.get(guild.id)
        if p is None:
            p = MusicPlayer(self.bot, guild.id, channel.id)
            self.players[guild.id] = p
        else:
            p.set_text_channel(channel.id)
        return p

    async def toggle_pause(self, guild: discord.Guild) -> None:
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

    async def _extract_youtube(self, query: str) -> Optional[dict[str, Any]]:
        def _run():
            return ytdl.extract_info(query, download=False)
        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            logger.warning("[Music] yt_dlp 실패: %s", e)
            return None

    def _info_to_track(self, info: dict[str, Any], source: str) -> Optional[Track]:
        if not info:
            return None
        if "entries" in info and isinstance(info.get("entries"), list) and info["entries"]:
            info = info["entries"][0] or {}
        if not info:
            return None

        title = info.get("title") or "Unknown"
        webpage_url = info.get("webpage_url") or info.get("original_url") or info.get("url")
        thumbnail = info.get("thumbnail")
        duration = info.get("duration")

        stream_url = info.get("url")
        if not stream_url:
            formats = info.get("formats") or []
            for f in formats:
                if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url"):
                    stream_url = f["url"]
                    break
        if not stream_url:
            return None

        return Track(
            title=str(title),
            stream_url=str(stream_url),
            webpage_url=str(webpage_url) if webpage_url else None,
            thumbnail=str(thumbnail) if thumbnail else None,
            source=source,
            duration=int(duration) if isinstance(duration, (int, float)) else None,
        )

    async def _add_single_youtube(self, url: str, source: str) -> Optional[Track]:
        info = await self._extract_youtube(url)
        if not info:
            return None
        return self._info_to_track(info, source)

    async def _youtube_quick_add(self, query: str) -> Optional[Track]:
        info = await self._extract_youtube(f"ytsearch1:{query}")
        if not info:
            return None
        return self._info_to_track(info, "YouTube(검색)")

    async def _resolve_member(self, guild: discord.Guild, user: discord.abc.User) -> Optional[discord.Member]:
        if isinstance(user, discord.Member):
            return user
        try:
            return await guild.fetch_member(user.id)
        except Exception:
            return None

    async def handle_youtube_query(self, *, guild_id: int, channel_id: int, query: str, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild or guild.id != guild_id:
            await interaction.followup.send(
                await self.music_say(kind="not_guild_context", user=interaction.user, fallback="여긴 서버가 아니라서, 유메가 이 기능은 못 써."),
                ephemeral=True,
                delete_after=4,
            )
            return

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.GuildChannel):
            await interaction.followup.send("채널을 찾지 못했어.", ephemeral=True, delete_after=3)
            return

        member = await self._resolve_member(guild, interaction.user)
        if member is None:
            await interaction.followup.send("유저 정보를 못 가져왔어.", ephemeral=True, delete_after=3)
            return

        player = self.get_player(guild, channel)
        q = (query or "").strip()
        if not q:
            await interaction.followup.send("검색어를 비워두면 안 돼.", ephemeral=True, delete_after=3)
            return

        lowered = q.lower()
        track: Optional[Track]

        if "youtube.com" in lowered or "youtu.be" in lowered:
            track = await self._add_single_youtube(q, "YouTube(URL)")
        else:
            track = await self._youtube_quick_add(q)

        if not track:
            await interaction.followup.send(
                await self.music_say(kind="add_fail", user=interaction.user, extra={"query": q}, fallback="검색 결과를 못 가져왔어. 잠깐 뒤에 다시 해볼래?"),
                ephemeral=True,
                delete_after=4,
            )
            return

        await player.add_track(track, requester=member)
        await player.update_panel()
        await interaction.followup.send(
            await self.music_say(kind="add_success", user=interaction.user, extra={"title": track.title}, fallback=f"✅ **{track.title}** 넣어 뒀어."),
            ephemeral=True,
            delete_after=4,
        )

    async def handle_spotify_query(self, *, guild_id: int, channel_id: int, query: str, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild or guild.id != guild_id:
            await interaction.followup.send(
                await self.music_say(kind="not_guild_context", user=interaction.user, fallback="여긴 서버가 아니라서, 유메가 이 기능은 못 써."),
                ephemeral=True,
                delete_after=4,
            )
            return

        token = await _get_spotify_access_token()
        if not token:
            await interaction.followup.send(
                await self.music_say(kind="spotify_not_configured", user=interaction.user, fallback="Spotify 설정이 아직 안 돼서, 지금은 YouTube 검색만 쓸 수 있어."),
                ephemeral=True,
                delete_after=5,
            )
            return

        try:
            async with aiohttp.ClientSession() as session:
                params = {"q": query, "type": "track", "limit": 1, "market": SPOTIFY_MARKET}
                async with session.get(
                    "https://api.spotify.com/v1/search",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                ) as resp:
                    if resp.status != 200:
                        await interaction.followup.send(
                            await self.music_say(kind="spotify_search_fail", user=interaction.user, extra={"query": query}, fallback="Spotify 검색 중에 문제가 생겼어. 잠시 뒤에 다시 시도해 줄래?"),
                            ephemeral=True,
                            delete_after=5,
                        )
                        return
                    data = await resp.json()
        except Exception:
            logger.exception("[Music] Spotify 검색 요청 중 예외 발생")
            await interaction.followup.send(
                await self.music_say(kind="spotify_search_exception", user=interaction.user, fallback="Spotify 쪽이 잠깐 삐끗했어. 조금만 있다가 다시 해볼래?"),
                ephemeral=True,
                delete_after=5,
            )
            return

        items = (((data or {}).get("tracks") or {}).get("items")) or []
        if not items:
            await interaction.followup.send(
                await self.music_say(kind="spotify_no_result", user=interaction.user, extra={"query": query}, fallback="Spotify에서 딱 맞는 곡을 못 찾았어."),
                ephemeral=True,
                delete_after=5,
            )
            return

        t0 = items[0] or {}
        name = t0.get("name") or query
        artists = t0.get("artists") or []
        artist_name = artists[0].get("name") if artists and isinstance(artists[0], dict) else ""
        yt_query = f"{name} {artist_name} audio".strip()

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.GuildChannel):
            await interaction.followup.send("채널을 찾지 못했어.", ephemeral=True, delete_after=3)
            return

        member = await self._resolve_member(guild, interaction.user)
        if member is None:
            await interaction.followup.send("유저 정보를 못 가져왔어.", ephemeral=True, delete_after=3)
            return

        player = self.get_player(guild, channel)
        track = await self._youtube_quick_add(yt_query)
        if not track:
            await interaction.followup.send(
                await self.music_say(kind="spotify_to_youtube_fail", user=interaction.user, extra={"title": name, "artist": artist_name}, fallback="Spotify 곡은 찾았는데, 유튜브 쪽에서 재생 정보를 못 가져왔어…"),
                ephemeral=True,
                delete_after=5,
            )
            return

        await player.add_track(track, requester=member)
        await player.update_panel()
        await interaction.followup.send(
            await self.music_say(kind="spotify_added", user=interaction.user, extra={"title": track.title}, fallback=f"✅ **{track.title}** 넣어 뒀어."),
            ephemeral=True,
            delete_after=4,
        )

    @commands.command(name="음악", aliases=["뮤직", "music", "음악패널"])
    async def cmd_music_panel(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return
        if not isinstance(ctx.channel, discord.abc.GuildChannel):
            await ctx.send("이 채널에서는 쓸 수 없어.")
            return

        player = self.get_player(ctx.guild, ctx.channel)
        embed = discord.Embed(
            title="🎶 유메 음악 패널",
            description="버튼으로 검색해서 대기열에 넣어줘.",
            color=discord.Color.blurple(),
        )
        msg = await ctx.send(embed=embed, view=MusicControlView(self, ctx.guild.id, ctx.channel.id))
        player.panel_msg_id = msg.id
        await player.update_panel()

    @commands.command(name="음악종료", aliases=["음악끄기", "음악정지", "음악스탑", "stopmusic"])
    async def cmd_music_stop(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self.players.get(ctx.guild.id)
        if not player:
            await ctx.send("지금은 재생 중이 아니야.")
            return
        await player.stop()
        await ctx.send(await self.music_say(kind="stopped", user=ctx.author, fallback="음악은 여기까지. 유메는 잠깐 쉬고 있을게, 으헤~"), delete_after=6)

    @commands.command(name="음악반복", aliases=["반복"])
    async def cmd_music_loop(self, ctx: commands.Context):
        if not ctx.guild:
            return
        mode = self.toggle_loop(ctx.guild)
        await ctx.send(await self.music_say(kind="loop_changed_cmd", user=ctx.author, extra={"mode": mode}, fallback=f"🔁 반복 모드를 `{mode}`로 바꿨어."), delete_after=6)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
PY

/opt/yume/venv/bin/python -m py_compile /opt/yume/cogs/music.py
sudo systemctl restart yume.service
sudo journalctl -u yume.service -n 80 --no-pager
