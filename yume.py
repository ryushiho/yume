from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Literal

import discord
from discord.ext import commands

logger = logging.getLogger("yume")

if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

logger.setLevel(logging.INFO)

_ENV_LOADED = False


def _load_env_from_dotenv() -> None:
    """
    프로젝트 루트의 .env / yumebot.env 파일을 읽어서 os.environ 에 넣는다.
    - python-dotenv 가 있으면 그걸 사용
    - 없으면 간단한 수동 파싱
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    root_dir = os.path.dirname(os.path.abspath(__file__))
    env_paths = [
        os.path.join(root_dir, ".env"),
        os.path.join(root_dir, "yumebot.env"),
    ]

    loaded_any = False

    try:
        from dotenv import load_dotenv  # type: ignore

        for path in env_paths:
            if os.path.exists(path):
                load_dotenv(path, override=False)
                logger.info("환경 파일을 python-dotenv로 로드했습니다: %s", path)
                loaded_any = True

        if not loaded_any:
            logger.warning(
                "환경 파일(.env / yumebot.env)을 찾지 못했습니다. 루트 디렉토리: %s",
                root_dir,
            )

    except ImportError:
        for path in env_paths:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        if key and key not in os.environ:
                            os.environ[key] = value
                logger.info("환경 파일을 수동 파싱으로 로드했습니다: %s", path)
                loaded_any = True
            except Exception as e:  # pylint: disable=broad-except
                logger.warning("환경 파일(%s) 읽는 중 오류: %s", path, e)

        if not loaded_any:
            logger.warning(
                "환경 파일(.env / yumebot.env)을 찾지 못했습니다. 루트 디렉토리: %s",
                root_dir,
            )

    _ENV_LOADED = True


def resolve_discord_token() -> Optional[str]:
    """
    DISCORD_TOKEN 은 .env / yumebot.env 에서 읽는다.
    """
    _load_env_from_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if token and token.strip():
        logger.info("환경에서 DISCORD_TOKEN 을 불러왔습니다.")
        return token.strip()
    return None


_load_env_from_dotenv()

from yume_ai import setup_yume_ai  # type: ignore


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)

DEV_USER_ID = 1433962010785349634

EXTENSIONS = [
    "cogs.admin",
    "cogs.blue_war",
    "cogs.music",
    "cogs.yume_diary",
    "cogs.yume_chat",
    "cogs.social",
]


@bot.command(
    name="sync",
    help="유메의 슬래시 명령어를 동기화하거나, 길드 중복을 정리해요. (개발자 전용)",
)
async def sync_command(
    ctx: commands.Context,
    scope: Literal["global", "cleanup"] = "global",
):
    if ctx.author.id != DEV_USER_ID:
        await ctx.send(
            "이 명령어는 개발자만 사용할 수 있어요.",
            delete_after=10,
        )
        return

    tree = ctx.bot.tree

    if scope == "cleanup":
        if ctx.guild is None:
            await ctx.send(
                "cleanup 은 서버 안에서만 사용할 수 있어요.",
                delete_after=10,
            )
            return

        try:
            tree.clear_commands(guild=ctx.guild)
            await tree.sync(guild=ctx.guild)

            await ctx.send(
                "🧹 이 서버의 길드 전용 슬래시 명령어를 정리했어요.\n"
                "이제 전역 슬래시 명령어만 보여야 해요.",
                delete_after=20,
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("길드 슬래시 정리(cleanup) 중 오류: %s", e)
            await ctx.send(
                "❌ 길드 슬래시 정리 중 오류가 발생했어요.",
                delete_after=20,
            )
        return

    if scope == "global":
        try:
            synced = await tree.sync()
            await ctx.send(
                (
                    f"🌐 전역 기준으로 슬래시 명령어 {len(synced)}개를 동기화했어요.\n"
                    "모든 서버에 반영되기까지는 시간이 조금 걸릴 수 있어요."
                ),
                delete_after=20,
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("전역 슬래시 동기화 중 오류: %s", e)
            await ctx.send(
                "❌ 전역 슬래시 동기화 중 오류가 발생했어요.",
                delete_after=20,
            )


@bot.event
async def on_ready():
    logger.info("유메 로그인 완료: %s (%s)", bot.user, bot.user.id)
    await bot.change_presence(activity=discord.Game(name="!도움"))
    try:
        synced = await bot.tree.sync()
        logger.info("슬래시 명령어 동기화 완료: %d개", len(synced))
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("슬래시 명령어 동기화 중 오류: %s", e)


async def main():
    token = resolve_discord_token()
    if not token:
        logger.error(
            "DISCORD_TOKEN 이 설정되어 있지 않습니다.\n"
            ".env 또는 yumebot.env 파일에 DISCORD_TOKEN=... 을 추가해 주세요."
        )
        return

    logger.info("로드할 Cog 확장 목록: %s", EXTENSIONS)

    setup_yume_ai(bot)

    async with bot:
        for ext in EXTENSIONS:
            try:
                await bot.load_extension(ext)
                logger.info("확장 로드 성공: %s", ext)
            except Exception as e:  # pylint: disable=broad-except
                logger.exception("확장 로드 실패: %s (%s)", ext, e)

        # systemd 재시작/종료(또는 deploy 과정)에서 SIGINT/SIGTERM으로
        # 이벤트 루프가 취소될 수 있다. 이때 CancelledError/KeyboardInterrupt가
        # 그대로 전파되면 journalctl에 "Traceback"이 찍혀서
        # 마치 크래시처럼 보인다.
        try:
            await bot.start(token)
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("종료 신호를 받아 유메를 정상 종료합니다.")
            # async with bot: 블록을 빠져나가며 close가 호출된다.
            return


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 일부 환경(특히 systemd stop/restart)에서 SIGINT가 들어오면
        # asyncio.run이 KeyboardInterrupt를 던질 수 있다.
        logger.info("KeyboardInterrupt로 종료합니다.")
    except asyncio.CancelledError:
        logger.info("CancelledError로 종료합니다.")
