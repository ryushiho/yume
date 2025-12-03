import asyncio
import discord
from discord.ext import commands
import yt_dlp
from discord import FFmpegPCMAudio, PCMVolumeTransformer

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


class Track:
    def __init__(
        self,
        title: str,
        url: str,
        webpage_url: str,
        thumbnail: str | None,
        source: str = "YouTube",
        duration: int | None = None,
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
            await self.ctx.send(
                "먼저 음성 채널에 들어가 줘. 그래야 유메도 따라갈 수 있어.",
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
                    await self.ctx.send(
                        "📭 대기열이 다 끝났으니까, 유메도 음성 채널에서 빠질게.",
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
            after=lambda e: self.bot.loop.create_task(self.play_next()),
        )

        await self.update_panel()

    async def _delete_panel_message(self):
        if not self.panel_msg_id:
            return
        try:
            msg = await self.ctx.channel.fetch_message(self.panel_msg_id)
            await msg.delete()
        except discord.NotFound:
            pass
        except Exception:
            pass

        guild = self.ctx.guild
        if guild and hasattr(self.bot, "yume_music_panels"):
            panels: dict[int, int] = self.bot.yume_music_panels
            if panels.get(guild.id) == self.panel_msg_id:
                del panels[guild.id]

        self.panel_msg_id = None

    def _build_embed(self) -> discord.Embed:
        if not self.current:
            embed = discord.Embed(
                title="🎶 유메 음악 패널",
                description="듣고 싶은 노래를 검색해서 넣어줘.\n유메가 차근차근 재생해 줄게.",
                color=discord.Color.blurple(),
            )
        else:
            embed = discord.Embed(
                title="🎶 Now Playing...",
                description=f"**{self.current.title}**",
                color=discord.Color.blue(),
            )
            if self.current.thumbnail:
                embed.set_image(url=self.current.thumbnail)

        embed.add_field(
            name="🔊 Volume",
            value=f"{int(self.volume * 100)}%",
            inline=True,
        )
        embed.add_field(
            name="Loop Mode",
            value=self.loop_mode,
            inline=True,
        )

        if self.queue:
            value = "\n".join(
                f"{i+1}. {t.title}" for i, t in enumerate(self.queue[:8])
            )
            if len(self.queue) > 8:
                value += "\n... 더 있음"
        else:
            value = "지금은 비어 있어. 유메한테 들려주고 싶은 노래를 넣어 줄래?"

        embed.add_field(name="📄 대기열", value=value, inline=False)
        return embed

    async def update_panel(self):
        if not self.panel_msg_id:
            return

        try:
            msg = await self.ctx.channel.fetch_message(self.panel_msg_id)
        except discord.NotFound:
            return

        embed = self._build_embed()
        await msg.edit(embed=embed)

    def delete_from_queue(self, index: int) -> str | None:
        if 0 <= index < len(self.queue):
            return self.queue.pop(index).title
        return None

    async def pause(self):
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            self.paused = True
            await self.update_panel()

    async def resume(self):
        if self.voice and self.paused:
            self.voice.resume()
            self.paused = False
            await self.update_panel()

    async def skip(self):
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()
            await self.update_panel()

    async def adjust_volume(self, delta: float):
        self.volume = max(0.1, min(2.0, self.volume + delta))
        if self.audio_source:
            self.audio_source.volume = self.volume
        await self.update_panel()


class YouTubeSearchModal(discord.ui.Modal, title="YouTube 검색"):
    query = discord.ui.TextInput(label="검색어나 URL 입력")

    def __init__(self, cog: "MusicCog", ctx: commands.Context):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.handle_youtube_query(self.ctx, self.query.value, interaction)


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

        for i in range(count):
            btn = discord.ui.Button(
                label=str(i + 1),
                style=discord.ButtonStyle.danger,
            )

            async def callback(
                interaction: discord.Interaction,
                offset=i,
            ):
                guild = interaction.guild
                if not guild:
                    return
                player = self.cog.players.get(guild.id)
                if not player:
                    await interaction.response.edit_message(
                        content="플레이어를 찾지 못했어.",
                        view=None,
                        embed=None,
                    )
                    return

                global_index = self.page * 10 + offset
                title = player.delete_from_queue(global_index)

                if title:
                    total = len(player.queue)
                    max_page = max(0, (total - 1) // 10) if total > 0 else 0
                    new_page = min(self.page, max_page)
                    view = QueuePageView(self.cog, guild.id, page=new_page)
                    await self.queue_message.edit(
                        embed=view._embed(),
                        view=view,
                    )
                    await interaction.response.edit_message(
                        content=f"🗑 **{title}** 지워 뒀어.",
                        view=None,
                        embed=None,
                    )
                    await player.update_panel()
                else:
                    await interaction.response.edit_message(
                        content="⚠ 음… 삭제에 실패했어.",
                        view=None,
                        embed=None,
                    )

            btn.callback = callback
            self.add_item(btn)


class QueuePageView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int, page: int = 0):
        super().__init__(timeout=30)
        self.cog = cog
        self.guild_id = guild_id
        self.page = page

    def _embed(self) -> discord.Embed:
        player = self.cog.players.get(self.guild_id)
        queue = player.queue if player else []
        total_pages = max(1, (len(queue) - 1) // 10 + 1) if queue else 1

        start = self.page * 10
        end = start + 10
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
            await interaction.response.send_message(
                "📭 삭제할 곡이 없어. 대기열이 이미 깔끔해.",
                ephemeral=True,
            )
            return

        start = self.page * 10
        end = min(start + 10, len(player.queue))
        count = end - start

        view = QueueDeleteView(
            self.cog,
            guild.id,
            self.page,
            count,
            interaction.message,
        )
        await interaction.response.send_message(
            "어떤 노래를 지울까? 유메가 대신 정리해 줄게.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, _):
        player = self.cog.players.get(self.guild_id)
        if not player:
            return
        if (self.page + 1) * 10 < len(player.queue):
            self.page += 1
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="❌ 닫기", style=discord.ButtonStyle.danger, row=0)
    async def close(self, interaction: discord.Interaction, _):
        await interaction.message.delete()


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

    @discord.ui.button(label="대기열 보기", style=discord.ButtonStyle.secondary, row=0)
    async def queue(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        player = self.cog.players.get(guild.id)
        if not player or not player.queue:
            await interaction.response.send_message(
                "📭 지금은 대기열이 비어있어.",
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
        text = self.cog._speak(
            "music_loop_changed",
            user=interaction.user,
            extra={"mode": mode},
            fallback=f"🔁 반복 모드를 `{mode}`(으)로 바꿔 뒀어. 마음에 안 들면 다시 말해줘.",
        )
        await interaction.response.send_message(
            text,
            ephemeral=True,
            delete_after=3,
        )


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}

        if not hasattr(self.bot, "yume_music_panels"):
            self.bot.yume_music_panels: dict[int, int] = {}
        if not hasattr(self.bot, "yume_music_panel_locks"):
            self.bot.yume_music_panel_locks: dict[int, asyncio.Lock] = {}

    # ==== 유메 AI 연동 (롤백 복원) ====

    def _speaker(self):
        return getattr(self.bot, "yume_speaker", None)

    def _memory(self):
        return getattr(self.bot, "yume_memory", None)

    def _speak(
        self,
        context_key: str,
        user: discord.abc.User | None = None,
        extra: dict | None = None,
        fallback: str = "",
    ) -> str:
        sp = self._speaker()
        if sp is None:
            return fallback or "..."
        try:
            uid = user.id if user is not None else None
            uname = getattr(user, "display_name", None) if user is not None else None
            is_dev = uid == DEV_USER_ID if uid is not None else False
            msg = sp.say(
                context_key,
                user_id=uid,
                user_name=uname,
                is_dev=is_dev,
                extra=extra or {},
            )
            return msg or fallback or "..."
        except Exception:
            return fallback or "..."

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
        locks: dict[int, asyncio.Lock] = self.bot.yume_music_panel_locks
        if guild_id not in locks:
            locks[guild_id] = asyncio.Lock()
        return locks[guild_id]

    def get_player(
        self,
        guild: discord.Guild,
        ctx: commands.Context | None = None,
    ) -> MusicPlayer:
        player = self.players.get(guild.id)
        if not player:
            if ctx is None:
                raise RuntimeError("Player not initialized for this guild")
            player = MusicPlayer(self.bot, ctx)
            self.players[guild.id] = player
        else:
            if ctx is not None:
                player.ctx = ctx
        return player

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
            await interaction.followup.send(
                "여긴 서버가 아니라서, 유메가 이 명령은 쓸 수 없어.",
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
                text = self._speak(
                    "music_add_url",
                    user=interaction.user,
                    extra={"title": track.title},
                    fallback=f"🔗 **{track.title}** 추가해 뒀어.",
                )
            else:
                text = "링크를 제대로 읽어오지 못했어. 유메가 조금 더 연습해볼게."
            await interaction.followup.send(
                text,
                ephemeral=True,
                delete_after=3,
            )
            return

        await self._youtube_quick_search(player, query, interaction)

    async def _add_single_youtube(
        self,
        player: MusicPlayer,
        query: str,
    ) -> Track | None:
        info = await self.bot.loop.run_in_executor(
            None,
            lambda: ytdl.extract_info(query, download=False),
        )
        if not info:
            return None
        if "entries" in info:
            info = info["entries"][0]

        duration = info.get("duration")
        track = Track(
            title=info.get("title", "제목 없음"),
            url=info.get("url"),
            webpage_url=info.get("webpage_url"),
            thumbnail=info.get("thumbnail"),
            source="YouTube",
            duration=duration,
        )
        await player.add(track)
        return track

    async def _youtube_quick_search(
        self,
        player: MusicPlayer,
        query: str,
        interaction: discord.Interaction,
    ):
        info = await self.bot.loop.run_in_executor(
            None,
            lambda: ytdl.extract_info(f"ytsearch1:{query}", download=False),
        )
        entries = info.get("entries", []) if info else []
        if not entries:
            await interaction.followup.send(
                "검색 결과를 못 찾았어. 다른 키워드로 한 번만 더 해볼까?",
                ephemeral=True,
                delete_after=3,
            )
            return

        e = entries[0]
        track = Track(
            title=e.get("title", "제목 없음"),
            url=e.get("url"),
            webpage_url=e.get("webpage_url"),
            thumbnail=e.get("thumbnail"),
            source="YouTube",
            duration=e.get("duration"),
        )
        await player.add(track)

        self._log_music_add(interaction.user, track, "YouTube(search)")

        text = self._speak(
            "music_add_search",
            user=interaction.user,
            extra={"title": track.title},
            fallback=f"✅ **{track.title}** 추가해 뒀어.",
        )
        await interaction.followup.send(
            text,
            ephemeral=True,
            delete_after=3,
        )

    # ==== 커맨드 ====

    @commands.hybrid_command(name="음악", description="유메 음악 패널 실행")
    async def music(self, ctx: commands.Context):
        guild = ctx.guild
        if not guild:
            await ctx.send(
                "여긴 서버가 아니라서, 유메가 이 명령은 쓸 수 없어.",
                delete_after=3,
            )
            return

        panels: dict[int, int] = self.bot.yume_music_panels
        lock = self._get_guild_lock(guild.id)

        async with lock:
            player = self.get_player(guild, ctx)

            async for msg in ctx.channel.history(limit=50):
                if msg.author == ctx.bot.user and msg.embeds:
                    title = msg.embeds[0].title or ""
                    if "유메 음악 패널" in title or "Now Playing..." in title:
                        player.panel_msg_id = msg.id
                        panels[guild.id] = msg.id
                        await msg.edit(view=MusicControlView(self, ctx, guild.id))

                        text = self._speak(
                            "music_panel_reuse",
                            user=ctx.author,
                            fallback="이미 만들어 둔 음악 패널이 있어서, 그걸 다시 쓸게.",
                        )
                        await ctx.send(text, delete_after=3)
                        return

            if guild.id in panels:
                msg_id = panels[guild.id]
                try:
                    msg = await ctx.channel.fetch_message(msg_id)
                    player.panel_msg_id = msg.id
                    await msg.edit(view=MusicControlView(self, ctx, guild.id))

                    text = self._speak(
                        "music_panel_reuse",
                        user=ctx.author,
                        fallback="이미 만들어 둔 음악 패널이 있어서, 그걸 다시 쓸게.",
                    )
                    await ctx.send(text, delete_after=3)
                    return
                except discord.NotFound:
                    del panels[guild.id]

            await player.ensure_voice()

            embed = discord.Embed(
                title="🎶 유메 음악 패널",
                description="듣고 싶은 노래를 검색해서 넣어줘.\n유메가 차근차근 재생해 줄게.",
                color=discord.Color.blurple(),
            )

            msg = await ctx.send(
                embed=embed,
                view=MusicControlView(self, ctx, guild.id),
            )
            player.panel_msg_id = msg.id
            panels[guild.id] = msg.id

            text = self._speak(
                "music_panel_open",
                user=ctx.author,
                fallback="음악 패널 열어 뒀어. 유메랑 같이 들을래?",
            )
            await ctx.send(text, delete_after=5)

    # ==== 음성 상태 ====

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        # 봇이 채널에서 완전히 나간 경우
        if member.id == self.bot.user.id:
            guild = member.guild
            if guild and before.channel is not None and after.channel is None:
                player = self.players.get(guild.id)
                if player:
                    await player._delete_panel_message()

                mem = self._memory()
                if mem is not None:
                    try:
                        mem.log_today(
                            f"음성 채널 종료: {guild.name} 에서 유메 음악 세션 종료"
                        )
                    except Exception:
                        pass
            return

        guild = member.guild
        voice_client = discord.utils.get(self.bot.voice_clients, guild=guild)
        if not voice_client or not voice_client.channel:
            return

        non_bot_members = [m for m in voice_client.channel.members if not m.bot]
        if not non_bot_members:
            player = self.players.get(guild.id)
            if player:
                player.voice = voice_client
                await player._delete_panel_message()
            await voice_client.disconnect()

            mem = self._memory()
            if mem is not None:
                try:
                    mem.log_today(
                        f"음성 채널 비워짐: {guild.name} 에서 아무도 안 남아서 유메도 나갈 거야."
                    )
                except Exception:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
