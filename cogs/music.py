import asyncio
import logging
from typing import Dict, List, Optional

import discord
from discord.ext import commands
import yt_dlp

logger = logging.getLogger(__name__)

YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "ytsearch",
    "noplaylist": True,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

_ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)


class _Track:
    __slots__ = ("title", "webpage_url", "stream_url", "requester")

    def __init__(self, title: str, webpage_url: str, stream_url: str, requester: Optional[int]):
        self.title = title
        self.webpage_url = webpage_url
        self.stream_url = stream_url
        self.requester = requester


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


def _ffmpeg_source(stream_url: str) -> discord.AudioSource:
    src = discord.FFmpegPCMAudio(
        stream_url,
        before_options=FFMPEG_BEFORE,
        options=FFMPEG_OPTIONS,
    )
    return discord.PCMVolumeTransformer(src, volume=0.35)


class MusicState:
    def __init__(self):
        self.queue: asyncio.Queue[_Track] = asyncio.Queue()
        self.now_playing: Optional[_Track] = None
        self.player_task: Optional[asyncio.Task] = None
        self.volume: float = 0.35


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._states: Dict[int, MusicState] = {}

    def _state(self, guild_id: int) -> MusicState:
        st = self._states.get(guild_id)
        if st is None:
            st = MusicState()
            self._states[guild_id] = st
        return st

    async def _ensure_voice(self, ctx: commands.Context) -> Optional[discord.VoiceClient]:
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return None

        if ctx.author.voice is None or ctx.author.voice.channel is None:
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

    async def _player_loop(self, guild_id: int, text_channel_id: int):
        st = self._state(guild_id)
        while True:
            try:
                track = await st.queue.get()
            except asyncio.CancelledError:
                return

            st.now_playing = track
            channel = self.bot.get_channel(text_channel_id)
            try:
                if isinstance(channel, (discord.TextChannel, discord.Thread)):
                    title = track.title or "제목 없음"
                    await channel.send(f"재생: **{title}**")
            except Exception:
                pass

            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            if vc is None or not vc.is_connected():
                st.now_playing = None
                continue

            try:
                src = _ffmpeg_source(track.stream_url)
                if isinstance(src, discord.PCMVolumeTransformer):
                    src.volume = st.volume

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
                st.now_playing = None

    def _start_player_if_needed(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        st = self._state(ctx.guild.id)
        if st.player_task and not st.player_task.done():
            return
        st.player_task = asyncio.create_task(self._player_loop(ctx.guild.id, ctx.channel.id))

    @commands.command(name="입장", aliases=["join", "소환"])
    async def cmd_join(self, ctx: commands.Context):
        vc = await self._ensure_voice(ctx)
        if not vc:
            return
        self._start_player_if_needed(ctx)
        await ctx.send("유메 왔어. 으헤~")

    @commands.command(name="나가", aliases=["leave", "퇴장"])
    async def cmd_leave(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            await ctx.send("이미 나가있어.")
            return
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

        st = self._state(ctx.guild.id)
        if st.player_task and not st.player_task.done():
            st.player_task.cancel()

        while not st.queue.empty():
            try:
                st.queue.get_nowait()
                st.queue.task_done()
            except Exception:
                break

        st.now_playing = None
        await ctx.send("나갈게. 으헤~")

    @commands.command(name="재생", aliases=["play", "p"])
    async def cmd_play(self, ctx: commands.Context, *, query: str):
        vc = await self._ensure_voice(ctx)
        if not vc:
            return

        q = (query or "").strip()
        if not q:
            await ctx.send("사용: !재생 <검색어/URL>")
            return

        try:
            info = await _extract_info(q)
            entry = _pick_entry(info)
            if not entry:
                await ctx.send("검색 결과가 없네.")
                return

            title = str(entry.get("title") or "제목 없음")
            webpage_url = str(entry.get("webpage_url") or entry.get("original_url") or q)
            stream_url = str(entry.get("url") or "")
            if not stream_url:
                await ctx.send("스트림 주소를 못 찾았어.")
                return

            track = _Track(title=title, webpage_url=webpage_url, stream_url=stream_url, requester=ctx.author.id)
            st = self._state(ctx.guild.id)
            await st.queue.put(track)
            self._start_player_if_needed(ctx)
            await ctx.send(f"큐에 추가: **{title}**")
        except Exception as e:
            logger.warning("[Music] extract error: %s", e)
            await ctx.send("그건 재생하기가 어려워…")

    @commands.command(name="스킵", aliases=["skip", "s"])
    async def cmd_skip(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected() or not vc.is_playing():
            await ctx.send("지금 재생 중이 아니야.")
            return
        vc.stop()
        await ctx.send("넘길게. 으헤~")

    @commands.command(name="정지", aliases=["stop"])
    async def cmd_stop(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        vc = ctx.guild.voice_client
        if vc and vc.is_connected():
            try:
                vc.stop()
            except Exception:
                pass

        st = self._state(ctx.guild.id)
        while not st.queue.empty():
            try:
                st.queue.get_nowait()
                st.queue.task_done()
            except Exception:
                break
        st.now_playing = None
        await ctx.send("멈췄어. 으헤~")

    @commands.command(name="일시정지", aliases=["pause"])
    async def cmd_pause(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected() or not vc.is_playing():
            await ctx.send("지금 재생 중이 아니야.")
            return
        vc.pause()
        await ctx.send("잠깐 멈출게.")

    @commands.command(name="재개", aliases=["resume"])
    async def cmd_resume(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected() or not vc.is_paused():
            await ctx.send("멈춰있지 않아.")
            return
        vc.resume()
        await ctx.send("다시 재생할게. 으헤~")

    @commands.command(name="볼륨", aliases=["volume"])
    async def cmd_volume(self, ctx: commands.Context, vol: int):
        if ctx.guild is None:
            return
        v = max(0, min(100, int(vol)))
        st = self._state(ctx.guild.id)
        st.volume = v / 100.0

        vc = ctx.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = st.volume

        await ctx.send(f"볼륨: {v}%")

    @commands.command(name="지금", aliases=["now", "np"])
    async def cmd_now(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        st = self._state(ctx.guild.id)
        if st.now_playing is None:
            await ctx.send("지금은 재생 중이 아니야.")
            return
        await ctx.send(f"지금 재생: **{st.now_playing.title}**")

    @commands.command(name="큐", aliases=["queue", "q"])
    async def cmd_queue(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        st = self._state(ctx.guild.id)

        items: List[_Track] = []
        try:
            while not st.queue.empty() and len(items) < 10:
                items.append(st.queue.get_nowait())
        except Exception:
            pass

        for t in items:
            await st.queue.put(t)

        if not items:
            await ctx.send("큐가 비어있어.")
            return

        lines = [f"{i+1}. {t.title}" for i, t in enumerate(items)]
        await ctx.send("큐:\n" + "\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
