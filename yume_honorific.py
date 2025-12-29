"""yume_honorific.py

Phase0: role-based honorific rules.

Rule (user request):
- If user has role_id=1453334977319276665 -> call them "후배"
- Otherwise -> "선생님"
- Exception: user_id=1433962010785349634 is always "후배" even without the role.

We keep this in one place so prompts, replies, and future features stay consistent.
"""

from __future__ import annotations

from typing import Optional

import discord


JUNIOR_ROLE_ID = 1453334977319276665
SPECIAL_JUNIOR_USER_ID = 1433962010785349634


def get_honorific(user: discord.abc.User, guild: Optional[discord.Guild]) -> str:
    """Return the honorific string for this user within a guild context."""
    if int(getattr(user, "id", 0)) == SPECIAL_JUNIOR_USER_ID:
        return "후배"

    # DM context: no roles
    if guild is None:
        return "선생님"

    member: Optional[discord.Member] = None
    if isinstance(user, discord.Member):
        member = user
    else:
        member = guild.get_member(user.id)

    if member is not None:
        try:
            for r in member.roles:
                if int(getattr(r, "id", 0)) == JUNIOR_ROLE_ID:
                    return "후배"
        except Exception:
            pass

    return "선생님"
