"""yume_send.py

Phase2: Yume message sending helpers.

We want the "Glitch Effect" to be a presentation layer:
- Check current virtual weather (world_state.weather)
- If sandstorm + user opted-in, apply mild text glitch occasionally
- Optionally split a message into 2 parts with a short delay (radio hiccup)

Important:
- Never glitch code blocks / backtick text (handled in yume_effects).
- Keep critical/system notices readable by passing allow_glitch=False.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Optional

import discord
from discord.ext import commands

from yume_effects import apply_glitch, chunk_for_discord, split_for_radio
from yume_store import get_user_settings, get_world_state


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)).strip())
    except Exception:
        return float(default)


def _should_glitch(*, weather: str, user_id: Optional[int], allow_glitch: bool) -> bool:
    if not allow_glitch:
        return False

    if str(weather) != "sandstorm":
        return False

    # Global override for debugging.
    if os.getenv("YUME_GLITCH_FORCE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True

    if user_id is None:
        # Broadcast/unknown user: allow by default.
        return True

    try:
        st = get_user_settings(int(user_id))
        return int(st.get("noise_opt_in") or 0) == 1
    except Exception:
        return True


async def _send_chunks(
    messageable: discord.abc.Messageable,
    chunks: list[str],
    *,
    embed: Optional[discord.Embed] = None,
    delete_after: Optional[float] = None,
) -> None:
    # First chunk can carry embed; others shouldn't.
    first = True
    for ch in chunks:
        if first:
            await messageable.send(ch, embed=embed, delete_after=delete_after)  # type: ignore[arg-type]
            first = False
        else:
            await messageable.send(ch)  # type: ignore[arg-type]
        # tiny jitter so split/chunk feels natural, but doesn't slow down too much
        if len(chunks) >= 2:
            await asyncio.sleep(0.05)


async def send_channel(
    channel: discord.abc.Messageable,
    content: str = "",
    *,
    target_user_id: Optional[int] = None,
    embed: Optional[discord.Embed] = None,
    delete_after: Optional[float] = None,
    allow_glitch: bool = True,
) -> None:
    """Send a message to a channel/thread/DM with optional glitch effect."""

    state = {}
    try:
        state = get_world_state()
    except Exception:
        state = {"weather": "clear"}

    weather = str(state.get("weather") or "clear")

    do_glitch = _should_glitch(weather=weather, user_id=target_user_id, allow_glitch=allow_glitch)

    glitch_chance = _env_float("YUME_GLITCH_CHANCE", 0.35)
    split_chance = _env_float("YUME_GLITCH_SPLIT_CHANCE", 0.12)
    max_ratio = _env_float("YUME_GLITCH_MAX_RATIO", 0.20)

    text = content or ""

    if do_glitch and text:
        if random.random() < glitch_chance:
            text = apply_glitch(text, max_ratio=max_ratio)

        # radio hiccup: split in two with a short delay
        if random.random() < split_chance:
            parts = split_for_radio(text)
            if len(parts) == 2 and parts[0] and parts[1]:
                p1_chunks = chunk_for_discord(parts[0])
                await _send_chunks(channel, p1_chunks, embed=embed, delete_after=delete_after)
                await asyncio.sleep(random.uniform(0.3, 0.8))
                p2_chunks = chunk_for_discord(parts[1])
                await _send_chunks(channel, p2_chunks)
                return

    chunks = chunk_for_discord(text)
    await _send_chunks(channel, chunks, embed=embed, delete_after=delete_after)


async def send_ctx(
    ctx: commands.Context,
    content: str = "",
    *,
    embed: Optional[discord.Embed] = None,
    delete_after: Optional[float] = None,
    allow_glitch: bool = True,
) -> None:
    """Send via Context, using ctx.author as target for opt-in."""
    await send_channel(
        ctx.channel,
        content,
        target_user_id=int(getattr(ctx.author, "id", 0)) if getattr(ctx, "author", None) else None,
        embed=embed,
        delete_after=delete_after,
        allow_glitch=allow_glitch,
    )


async def reply_message(
    message: discord.Message,
    content: str,
    *,
    mention_author: bool = False,
    allow_glitch: bool = True,
) -> None:
    """Reply to a message with optional glitch effect."""

    state = {}
    try:
        state = get_world_state()
    except Exception:
        state = {"weather": "clear"}

    weather = str(state.get("weather") or "clear")

    target_user_id = int(getattr(message.author, "id", 0)) if getattr(message, "author", None) else None
    do_glitch = _should_glitch(weather=weather, user_id=target_user_id, allow_glitch=allow_glitch)

    glitch_chance = _env_float("YUME_GLITCH_CHANCE", 0.35)
    split_chance = _env_float("YUME_GLITCH_SPLIT_CHANCE", 0.12)
    max_ratio = _env_float("YUME_GLITCH_MAX_RATIO", 0.20)

    text = content or ""

    if do_glitch and text:
        if random.random() < glitch_chance:
            text = apply_glitch(text, max_ratio=max_ratio)

        if random.random() < split_chance:
            parts = split_for_radio(text)
            if len(parts) == 2 and parts[0] and parts[1]:
                for ch in chunk_for_discord(parts[0]):
                    await message.reply(ch, mention_author=mention_author)
                    await asyncio.sleep(0.05)
                await asyncio.sleep(random.uniform(0.3, 0.8))
                for ch in chunk_for_discord(parts[1]):
                    await message.reply(ch, mention_author=mention_author)
                    await asyncio.sleep(0.05)
                return

    for ch in chunk_for_discord(text):
        await message.reply(ch, mention_author=mention_author)
        await asyncio.sleep(0.05)


async def send_user(
    user: discord.abc.User,
    content: str = "",
    *,
    embed: Optional[discord.Embed] = None,
    allow_glitch: bool = True,
) -> None:
    """Send DM to a user. Uses the same opt-in + weather rule."""
    await send_channel(
        user,
        content,
        target_user_id=int(getattr(user, "id", 0)),
        embed=embed,
        delete_after=None,
        allow_glitch=allow_glitch,
    )
