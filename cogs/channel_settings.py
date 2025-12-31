from __future__ import annotations

import re
from typing import Optional, Tuple

import discord
from discord.ext import commands

from yume_send import send_ctx
from yume_store import get_config, set_config

# 표시 이름 -> (bot_config key, 설명)
FEATURES: dict[str, Tuple[str, str]] = {
    "교칙": ("rule_channel_id", "매일 교칙 공지/강제교칙 출력 채널"),
    "유메일기": ("diary_channel_id", "매일 KST 23:59 유메일기 자동 마무리 채널"),
}

# 입력 별칭 -> 표시 이름
ALIASES: dict[str, str] = {
    "규칙": "교칙",
    "rule": "교칙",
    "rules": "교칙",
    "diary": "유메일기",
    "일기": "유메일기",
}


def _normalize_feature(s: str) -> Optional[str]:
    raw = (s or "").strip()
    if not raw:
        return None
    raw_l = raw.lower()
    # exact match
    if raw in FEATURES:
        return raw
    if raw_l in ALIASES:
        return ALIASES[raw_l]
    # allow partial match (e.g., "유메일" -> "유메일기")
    for k in FEATURES.keys():
        if raw in k:
            return k
    return None


def _extract_channel_id(token: str) -> int:
    t = (token or "").strip()
    if not t:
        return 0
    # <#123>
    m = re.match(r"^<#!?(\d+)>$", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    # pure digits
    if t.isdigit():
        try:
            return int(t)
        except Exception:
            return 0
    return 0


class ChannelSettingsCog(commands.Cog):
    """채널 지정 통합 커맨드

    예)
    - !채널지정
    - !채널지정 set 교칙 #공지
    - !채널지정 set 유메일기 #일기
    - !채널지정 clear 교칙
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _can_manage(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        perms = getattr(ctx.author, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))

    def _format_current(self) -> str:
        lines = ["📌 **채널지정 현재 상태**"]
        for display, (key, desc) in FEATURES.items():
            raw = (get_config(key, "") or "").strip()
            if raw and raw.isdigit():
                lines.append(f"- **{display}**: <#{raw}>  — {desc}")
            else:
                lines.append(f"- **{display}**: (미설정)  — {desc}")
        lines.append("")
        lines.append(
            "사용법: `!채널지정 set 교칙 #채널` / `!채널지정 set 유메일기 #채널` / `!채널지정 clear 교칙`\n"
            "테스트: `!채널지정 test 교칙` / `!채널지정 test 유메일기` / `!채널지정 test all`"
        )
        return "\n".join(lines)

    @commands.command(name="채널지정", aliases=["채널설정"])
    async def cmd_channel_settings(self, ctx: commands.Context, *, args: str = ""):
        args = (args or "").strip()
        if not args:
            await send_ctx(ctx, self._format_current())
            return

        parts = args.split()
        action = parts[0].lower().strip()

        # show/list
        if action in ("show", "list", "목록", "보기", "status", "상태", "현재"):
            await send_ctx(ctx, self._format_current())
            return

        # help
        if action in ("help", "도움", "?"):
            await send_ctx(ctx, self._format_current())
            return

        # test
        if action in ("test", "테스트", "확인"):
            if not self._can_manage(ctx):
                await send_ctx(ctx, "이 명령은 **서버 관리 권한(관리 서버)**이 필요해…")
                return
            if len(parts) < 2:
                await send_ctx(ctx, "테스트할 기능명을 같이 적어줘. 예: `!채널지정 test 교칙`")
                return

            target = parts[1].strip()
            if target.lower() in ("all", "전체", "전부", "모두"):
                results: list[tuple[str, bool, str, Optional[int]]] = []
                for disp, (key, _desc) in FEATURES.items():
                    raw = (get_config(key, "") or "").strip()
                    if not raw or not raw.isdigit():
                        results.append((disp, False, "미설정", None))
                        continue

                    cid = int(raw)
                    ch = None
                    try:
                        if ctx.guild is not None:
                            ch = ctx.guild.get_channel(cid) or await ctx.guild.fetch_channel(cid)
                    except Exception:
                        ch = None

                    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                        results.append((disp, False, "채널을 찾지 못함", cid))
                        continue

                    try:
                        await ch.send(
                            f"✅ (테스트) **{disp}** 채널이 여기로 설정돼 있어.\n"
                            f"- 설정자: {ctx.author.mention}\n"
                            f"- 명령: `!채널지정 test all`",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        results.append((disp, True, "전송 성공", cid))
                    except discord.Forbidden:
                        results.append((disp, False, "권한 없음", cid))
                    except Exception:
                        results.append((disp, False, "전송 실패", cid))

                ok = sum(1 for _disp, success, _reason, _cid in results if success)
                lines_out = ["✅ (테스트) **전체 채널 지정 점검 결과**"]
                for disp, success, reason, cid in results:
                    icon = "✅" if success else "⚠️"
                    chan = f"<#{cid}>" if cid else "(미설정)"
                    lines_out.append(f"- {icon} **{disp}**: {chan} — {reason}")
                lines_out.append(f"완료! 성공 {ok}/{len(results)}")
                await send_ctx(ctx, "\n".join(lines_out))
                return

            feature = _normalize_feature(target)
            if not feature:
                await send_ctx(ctx, "그 기능은 잘 모르겠어… (가능: 교칙, 유메일기)")
                return

            key, _desc = FEATURES[feature]
            raw = (get_config(key, "") or "").strip()
            if not raw or not raw.isdigit():
                await send_ctx(ctx, f"**{feature}** 채널이 아직 설정되지 않았어. 먼저 `!채널지정 set {feature} #채널` 해줘.")
                return

            cid = int(raw)
            ch = None
            try:
                if ctx.guild is not None:
                    ch = ctx.guild.get_channel(cid) or await ctx.guild.fetch_channel(cid)
            except Exception:
                ch = None

            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                await send_ctx(ctx, f"<#{cid}> 채널을 찾지 못했어… 채널이 삭제됐거나 접근 권한이 없을 수도 있어.")
                return

            try:
                await ch.send(
                    f"✅ (테스트) **{feature}** 채널이 여기로 설정돼 있어.\n"
                    f"- 설정자: {ctx.author.mention}\n"
                    f"- 명령: `!채널지정 test {feature}`",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await send_ctx(ctx, f"완료! <#{cid}> 로 테스트 메시지를 보냈어.")
            except discord.Forbidden:
                await send_ctx(ctx, f"<#{cid}>에 메시지를 보낼 권한이 없어… 유메 권한을 확인해줘.")
            except Exception:
                await send_ctx(ctx, f"<#{cid}>로 테스트를 보내는 중에 오류가 났어…")
            return

        # clear/unset
        if action in ("clear", "unset", "remove", "해제", "삭제", "지우기"):
            if not self._can_manage(ctx):
                await send_ctx(ctx, "이 명령은 **서버 관리 권한(관리 서버)**이 필요해…")
                return
            if len(parts) < 2:
                await send_ctx(ctx, "해제할 기능명을 같이 적어줘. 예: `!채널지정 clear 교칙`")
                return
            target = parts[1].strip()
            if target.lower() in ("all", "전체", "전부", "모두"):
                results: list[tuple[str, bool, str, Optional[int]]] = []
                for disp, (key, _desc) in FEATURES.items():
                    raw = (get_config(key, "") or "").strip()
                    if not raw or not raw.isdigit():
                        results.append((disp, False, "미설정", None))
                        continue

                    cid = int(raw)
                    ch = None
                    try:
                        if ctx.guild is not None:
                            ch = ctx.guild.get_channel(cid) or await ctx.guild.fetch_channel(cid)
                    except Exception:
                        ch = None

                    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                        results.append((disp, False, "채널을 찾지 못함", cid))
                        continue

                    try:
                        await ch.send(
                            f"✅ (테스트) **{disp}** 채널이 여기로 설정돼 있어.\n"
                            f"- 설정자: {ctx.author.mention}\n"
                            f"- 명령: `!채널지정 test all`",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        results.append((disp, True, "전송 성공", cid))
                    except discord.Forbidden:
                        results.append((disp, False, "권한 없음", cid))
                    except Exception:
                        results.append((disp, False, "전송 실패", cid))

                ok = sum(1 for _disp, success, _reason, _cid in results if success)
                lines_out = ["✅ (테스트) **전체 채널 지정 점검 결과**"]
                for disp, success, reason, cid in results:
                    icon = "✅" if success else "⚠️"
                    chan = f"<#{cid}>" if cid else "(미설정)"
                    lines_out.append(f"- {icon} **{disp}**: {chan} — {reason}")
                lines_out.append(f"완료! 성공 {ok}/{len(results)}")
                await send_ctx(ctx, "\n".join(lines_out))
                return

            feature = _normalize_feature(target)
            if not feature:
                await send_ctx(ctx, "그 기능은 잘 모르겠어… (가능: 교칙, 유메일기)")
                return
            key, _desc = FEATURES[feature]
            set_config(key, "")
            await send_ctx(ctx, f"✅ **{feature}** 채널 지정을 해제했어.")
            return

        # set/설정
        if action in ("set", "설정", "지정", "change", "update"):
            if not self._can_manage(ctx):
                await send_ctx(ctx, "이 명령은 **서버 관리 권한(관리 서버)**이 필요해…")
                return
            if len(parts) < 3:
                await send_ctx(ctx, "지정할 기능명과 채널을 같이 적어줘. 예: `!채널지정 set 교칙 #공지`")
                return

            target = parts[1].strip()
            if target.lower() in ("all", "전체", "전부", "모두"):
                results: list[tuple[str, bool, str, Optional[int]]] = []
                for disp, (key, _desc) in FEATURES.items():
                    raw = (get_config(key, "") or "").strip()
                    if not raw or not raw.isdigit():
                        results.append((disp, False, "미설정", None))
                        continue

                    cid = int(raw)
                    ch = None
                    try:
                        if ctx.guild is not None:
                            ch = ctx.guild.get_channel(cid) or await ctx.guild.fetch_channel(cid)
                    except Exception:
                        ch = None

                    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                        results.append((disp, False, "채널을 찾지 못함", cid))
                        continue

                    try:
                        await ch.send(
                            f"✅ (테스트) **{disp}** 채널이 여기로 설정돼 있어.\n"
                            f"- 설정자: {ctx.author.mention}\n"
                            f"- 명령: `!채널지정 test all`",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        results.append((disp, True, "전송 성공", cid))
                    except discord.Forbidden:
                        results.append((disp, False, "권한 없음", cid))
                    except Exception:
                        results.append((disp, False, "전송 실패", cid))

                ok = sum(1 for _disp, success, _reason, _cid in results if success)
                lines_out = ["✅ (테스트) **전체 채널 지정 점검 결과**"]
                for disp, success, reason, cid in results:
                    icon = "✅" if success else "⚠️"
                    chan = f"<#{cid}>" if cid else "(미설정)"
                    lines_out.append(f"- {icon} **{disp}**: {chan} — {reason}")
                lines_out.append(f"완료! 성공 {ok}/{len(results)}")
                await send_ctx(ctx, "\n".join(lines_out))
                return

            feature = _normalize_feature(target)
            if not feature:
                await send_ctx(ctx, "그 기능은 잘 모르겠어… (가능: 교칙, 유메일기)")
                return

            # channel: prefer mention
            cid = 0
            if ctx.message.channel_mentions:
                cid = int(ctx.message.channel_mentions[0].id)
            else:
                cid = _extract_channel_id(parts[2])

            if cid <= 0:
                await send_ctx(ctx, "채널을 인식 못 했어. `#채널`을 멘션해서 지정해줘.")
                return

            key, _desc = FEATURES[feature]
            set_config(key, str(cid))
            await send_ctx(ctx, f"✅ **{feature}** 채널을 <#{cid}> 로 지정했어.")
            return

        await send_ctx(ctx, "형식이 조금 이상해… `!채널지정`을 쳐서 사용법을 볼래?")


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelSettingsCog(bot))
