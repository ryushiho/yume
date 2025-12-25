from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import discord
from discord.ext import commands
import yt_dlp

logger = logging.getLogger(__name__)


# ==============================
# YouTube / ffmpeg 설정
# ==============================

YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "ytsearch",
    "noplaylist": True,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

_ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)


# ==============================
# 내부 데이터 구조
# ==============================


@dataclass
class _Track:
    title: str
    webpage_url: str
    stream_url: str
    requester_id: Optional[int] = None


async def _extract_info(query: str) -> dict:
    loop = asyncio.get_running_loop()

    def _run():
        return _ytdl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _run)


def _pick_entry(info: dict) -> dict:
    if not info:
        return {}
    if "entries" in info and isinstance(info["entries"], list):
        for e in info["entries"]:
            if e:
                return e
        return {}
    return info


def _ffmpeg_source(stream_url: str, volume: float) -> discord.AudioSource:
    src = discord.FFmpegPCMAudio(
        stream_url,
        before_options=FFMPEG_BEFORE,
        options=FFMPEG_OPTIONS,
    )
    return discord.PCMVolumeTransformer(src, volume=volume)


class MusicState:
    def __init__(self):
        self.queue: asyncio.Queue[_Track] = asyncio.Queue()
        self.now_playing: Optional[_Track] = None
        self.player_task: Optional[asyncio.Task] = None

        self.volume: float = 0.35
        self.loop_all: bool = False

        # 버튼 액션으로 트랙을 멈췄을 때(스킵/정지) 루프 재큐잉을 한 번 막는다.
        self._suppress_requeue_once: bool = False


# ==============================
# UI (패널 / 버튼)
# ==============================


class MusicAddModal(discord.ui.Modal):
    def __init__(self, cog: "MusicCog"):
        super().__init__(title="🎵 노래 추가")
        self.cog = cog

        self.query = discord.ui.TextInput(
            label="유튜브 검색어 또는 URL",
            placeholder="예: Blue Archive OST / https://youtu.be/...",
            required=True,
            max_length=200,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        q = (self.query.value or "").strip()
        await self.cog._enqueue_from_interaction(interaction, q)


class MusicPanelView(discord.ui.View):
    """패널은 재부팅 이후에도 버튼이 살아있도록(퍼시스턴트) timeout=None로 유지."""

    def __init__(self, cog: "MusicCog"):
        super().__init__(timeout=None)
        self.cog = cog

    # ➕ 추가
    @discord.ui.button(label="추가", style=discord.ButtonStyle.primary, emoji="➕", custom_id="yume_music_add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await interaction.response.send_modal(MusicAddModal(self.cog))

    # ⏯ 일시정지/재개
    @discord.ui.button(label="재생/일시정지", style=discord.ButtonStyle.secondary, emoji="⏯", custom_id="yume_music_toggle")
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_pause(interaction)

    # ⏭ 스킵
    @discord.ui.button(label="스킵", style=discord.ButtonStyle.secondary, emoji="⏭", custom_id="yume_music_skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._skip(interaction)

    # ⏹ 정지
    @discord.ui.button(label="정지", style=discord.ButtonStyle.danger, emoji="⏹", custom_id="yume_music_stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._stop(interaction)

    # 🔁 반복 토글
    @discord.ui.button(label="반복", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="yume_music_loop")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_loop(interaction)

    # 🔀 셔플
    @discord.ui.button(label="셔플", style=discord.ButtonStyle.secondary, emoji="🔀", custom_id="yume_music_shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._shuffle(interaction)

    # 🔉 볼륨 -
    @discord.ui.button(label="-", style=discord.ButtonStyle.secondary, emoji="🔉", custom_id="yume_music_voldown")
    async def voldown_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._change_volume(interaction, delta=-0.05)

    # 🔊 볼륨 +
    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, emoji="🔊", custom_id="yume_music_volup")
    async def volup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._change_volume(interaction, delta=+0.05)

    # 🚪 나가기
    @discord.ui.button(label="나가기", style=discord.ButtonStyle.secondary, emoji="🚪", custom_id="yume_music_leave")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._leave(interaction)


# ==============================
# Cog
# ==============================


class MusicCog(commands.Cog):
    """
    음악은 이제 **!음악** 하나로만 연다.
    - !음악: 유메 음성채널 입장 + 음악 패널(임베드 + 버튼) 표시
    - 노래 추가/컨트롤은 전부 패널 버튼으로 처리
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._states: Dict[int, MusicState] = {}

        # 재부팅 후에도 버튼이 살아있도록 등록할 퍼시스턴트 뷰
        self.panel_view = MusicPanelView(self)

    # -------------------------------
    # State
    # -------------------------------
    def _state(self, guild_id: int) -> MusicState:
        st = self._states.get(guild_id)
        if st is None:
            st = MusicState()
            self._states[guild_id] = st
        return st

    # -------------------------------
    # Voice connect helpers
    # -------------------------------
    async def _ensure_voice_ctx(self, ctx: commands.Context) -> Optional[discord.VoiceClient]:
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return None
        if not isinstance(ctx.author, discord.Member) or ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("먼저 음성 채널에 들어가줘.")
            return None

        vc = ctx.guild.voice_client
        try:
            if vc and vc.is_connected():
                if vc.channel and vc.channel.id != ctx.author.voice.channel.id:
                    await vc.move_to(ctx.author.voice.channel)
            else:
                vc = await ctx.author.voice.channel.connect()
        except Exception as e:
            logger.warning("[Music] voice connect error: %s", e)
            await ctx.send("음성 채널에 연결하지 못했어.")
            return None

        return vc

    async def _ensure_voice_interaction(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        if interaction.guild is None:
            # 상위 호출부에서 응답(또는 defer) 여부가 달라질 수 있으니,
            # 여기서는 조용히 None만 반환하고 메시지는 호출부에서 처리한다.
            return None

        if not isinstance(interaction.user, discord.Member) or interaction.user.voice is None or interaction.user.voice.channel is None:
            return None

        vc = interaction.guild.voice_client
        try:
            if vc and vc.is_connected():
                # 버튼 누른 사람이 다른 채널이면 그 채널로 이동
                if vc.channel and vc.channel.id != interaction.user.voice.channel.id:
                    await vc.move_to(interaction.user.voice.channel)
            else:
                vc = await interaction.user.voice.channel.connect()
        except Exception as e:
            logger.warning("[Music] voice connect error: %s", e)
            return None

        return vc

    # -------------------------------
    # Player loop
    # -------------------------------
    async def _player_loop(self, guild_id: int, text_channel_id: int):
        st = self._state(guild_id)

        while True:
            try:
                track = await st.queue.get()
            except asyncio.CancelledError:
                return

            st.now_playing = track

            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None

            # 보이스가 없으면 트랙 버리고 다음
            if vc is None or not vc.is_connected():
                st.now_playing = None
                continue

            # 패널 업데이트(재생 시작)
            await self._try_refresh_panel(text_channel_id)

            try:
                src = _ffmpeg_source(track.stream_url, volume=st.volume)
                done = asyncio.Event()

                def _after(err: Optional[Exception]):
                    if err:
                        logger.warning("[Music] playback error: %s", err)
                    try:
                        self.bot.loop.call_soon_threadsafe(done.set)
                    except Exception:
                        pass

                vc.play(src, after=_after)
                await done.wait()

            except Exception as e:
                logger.warning("[Music] play error: %s", e)

            finally:
                finished = st.now_playing
                st.now_playing = None

                # 루프(큐 반복) 옵션: 스킵/정지로 멈춘 경우엔 한 번 재큐잉을 막는다.
                if st.loop_all and finished is not None and not st._suppress_requeue_once:
                    try:
                        await st.queue.put(finished)
                    except Exception:
                        pass
                st._suppress_requeue_once = False

                await self._try_refresh_panel(text_channel_id)

    def _start_player_if_needed(self, guild_id: int, text_channel_id: int):
        st = self._state(guild_id)
        if st.player_task and not st.player_task.done():
            return
        st.player_task = asyncio.create_task(self._player_loop(guild_id, text_channel_id))

    # -------------------------------
    # Panel render/update
    # -------------------------------
    def _build_embed(self, guild: discord.Guild) -> discord.Embed:
        st = self._state(guild.id)
        vc = guild.voice_client

        now_title = st.now_playing.title if st.now_playing else "없음"
        now_url = st.now_playing.webpage_url if st.now_playing else None

        embed = discord.Embed(
            title="🎵 유메 음악 패널",
            description=(
                "버튼으로 조작해줘.\n"
                "- ➕ **추가**: 유튜브 검색어/URL로 큐에 넣기\n"
                "- ⏯: 재생/일시정지\n"
                "- ⏭: 다음 곡\n"
                "- ⏹: 정지(큐 비움)\n"
                "- 🔁: 큐 반복 토글\n"
                "- 🔀: 큐 셔플\n"
                "- 🔉/🔊: 볼륨 조절\n"
                "- 🚪: 나가기"
            ),
            color=discord.Color.blurple(),
        )

        if now_url:
            embed.add_field(name="지금 재생", value=f"[{now_title}]({now_url})", inline=False)
        else:
            embed.add_field(name="지금 재생", value=now_title, inline=False)

        embed.add_field(name="큐 길이", value=str(st.queue.qsize()), inline=True)
        embed.add_field(name="반복", value="ON" if st.loop_all else "OFF", inline=True)
        embed.add_field(name="볼륨", value=f"{int(st.volume * 100)}%", inline=True)

        if vc and vc.is_connected() and vc.channel:
            embed.add_field(name="음성 채널", value=vc.channel.name, inline=False)
        else:
            embed.add_field(name="음성 채널", value="(연결 안 됨)", inline=False)

        return embed

    async def _try_refresh_panel(self, channel_id: int):
        """패널 메시지들(최근 1개)만 찾아서 갱신한다. 실패해도 조용히 무시."""
        ch = self.bot.get_channel(channel_id)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        guild = ch.guild
        embed = self._build_embed(guild)

        # 최근 메시지 20개 안에서 "유메 음악 패널"을 찾아 갱신 (패널 중복 방지용)
        if not self.bot.user:
            return

        try:
            async for msg in ch.history(limit=20):
                if msg.author.id != self.bot.user.id:
                    continue
                if msg.embeds and msg.embeds[0].title == "🎵 유메 음악 패널":
                    await msg.edit(embed=embed, view=self.panel_view)
                    break
        except Exception:
            pass

    async def _refresh_from_interaction(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        embed = self._build_embed(interaction.guild)
        try:
            await interaction.message.edit(embed=embed, view=self.panel_view)
        except Exception:
            pass

    # -------------------------------
    # Queue operations
    # -------------------------------
    async def _enqueue_from_interaction(self, interaction: discord.Interaction, query: str):
        # 모달 제출에서 호출될 수 있으므로 우선 defer 후 followup로 처리한다.
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            # 이미 응답된 경우 등은 무시
            pass

        if interaction.guild is None:
            return

        q = (query or "").strip()
        if not q:
            try:
                await interaction.followup.send("검색어/URL이 비어있어.", ephemeral=True)
            except Exception:
                pass
            return

        vc = await self._ensure_voice_interaction(interaction)
        if not vc:
            try:
                await interaction.followup.send("먼저 음성 채널에 들어가줘.", ephemeral=True)
            except Exception:
                pass
            return

        try:
            info = await _extract_info(q)
            entry = _pick_entry(info)
            if not entry:
                await interaction.followup.send("검색 결과가 없네.", ephemeral=True)
                return

            title = str(entry.get("title") or "제목 없음")
            webpage_url = str(entry.get("webpage_url") or entry.get("original_url") or q)
            stream_url = str(entry.get("url") or "")
            if not stream_url:
                await interaction.followup.send("스트림 주소를 못 찾았어.", ephemeral=True)
                return

            track = _Track(title=title, webpage_url=webpage_url, stream_url=stream_url, requester_id=interaction.user.id)

            st = self._state(interaction.guild.id)
            await st.queue.put(track)
            self._start_player_if_needed(interaction.guild.id, interaction.channel_id)

            await interaction.followup.send(f"큐에 추가: **{title}**", ephemeral=True)
            await self._refresh_from_interaction(interaction)
        except Exception as e:
            logger.warning("[Music] extract error: %s", e)
            try:
                await interaction.followup.send("그건 재생하기가 어려워…", ephemeral=True)
            except Exception:
                pass

    # -------------------------------
    # Button actions
    # -------------------------------
    async def _toggle_pause(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        vc = interaction.guild.voice_client
        try:
            if vc and vc.is_connected() and vc.is_playing():
                vc.pause()
                await interaction.response.send_message("잠깐 멈출게.", ephemeral=True)
            elif vc and vc.is_connected() and vc.is_paused():
                vc.resume()
                await interaction.response.send_message("다시 재생할게. 으헤~", ephemeral=True)
            else:
                await interaction.response.send_message("지금 재생 중이 아니야.", ephemeral=True)
        except Exception:
            try:
                await interaction.response.send_message("지금은 조작이 잘 안 돼…", ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _skip(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        vc = interaction.guild.voice_client
        st = self._state(interaction.guild.id)

        if not vc or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("넘길 곡이 없어.", ephemeral=True)
            return

        st._suppress_requeue_once = True
        try:
            vc.stop()
        except Exception:
            pass

        try:
            await interaction.response.send_message("넘길게. 으헤~", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _stop(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return

        st = self._state(interaction.guild.id)
        st._suppress_requeue_once = True

        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            try:
                vc.stop()
            except Exception:
                pass

        # 큐 비우기
        try:
            while not st.queue.empty():
                st.queue.get_nowait()
        except Exception:
            pass

        st.now_playing = None

        try:
            await interaction.response.send_message("멈췄어. 으헤~", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _toggle_loop(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)
        st.loop_all = not st.loop_all
        try:
            await interaction.response.send_message(
                f"반복: {'ON' if st.loop_all else 'OFF'}",
                ephemeral=True,
            )
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _shuffle(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)

        # asyncio.Queue는 직접 섞을 수 없어서 잠깐 빼서 섞고 다시 넣는다.
        items: List[_Track] = []
        try:
            while not st.queue.empty():
                items.append(st.queue.get_nowait())
        except Exception:
            pass

        if not items:
            await interaction.response.send_message("셔플할 큐가 비어있어.", ephemeral=True)
            return

        import random

        random.shuffle(items)
        for t in items:
            await st.queue.put(t)

        try:
            await interaction.response.send_message("큐를 섞었어.", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _change_volume(self, interaction: discord.Interaction, *, delta: float):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)
        st.volume = max(0.0, min(1.0, st.volume + delta))

        vc = interaction.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = st.volume

        try:
            await interaction.response.send_message(f"볼륨: {int(st.volume * 100)}%", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _leave(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message("이미 나가있어.", ephemeral=True)
            return

        st = self._state(interaction.guild.id)
        st._suppress_requeue_once = True

        try:
            vc.stop()
        except Exception:
            pass

        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

        if st.player_task and not st.player_task.done():
            st.player_task.cancel()

        try:
            while not st.queue.empty():
                st.queue.get_nowait()
        except Exception:
            pass

        st.now_playing = None

        try:
            await interaction.response.send_message("나갈게. 으헤~", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    # -------------------------------
    # Command
    # -------------------------------
    @commands.command(name="음악")
    async def music_panel(self, ctx: commands.Context):
        """!음악: 유메를 음성 채널로 부르고 음악 패널을 띄운다."""
        vc = await self._ensure_voice_ctx(ctx)
        if not vc or ctx.guild is None:
            return

        self._start_player_if_needed(ctx.guild.id, ctx.channel.id)

        embed = self._build_embed(ctx.guild)
        await ctx.send(embed=embed, view=self.panel_view)


async def setup(bot: commands.Bot):
    cog = MusicCog(bot)
    await bot.add_cog(cog)

    # 퍼시스턴트 뷰 등록 (재부팅 후에도 버튼이 동작)
    try:
        bot.add_view(cog.panel_view)
    except Exception:
        pass
