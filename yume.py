from __future__ import annotations

import asyncio
import logging
import os
import signal
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
from yume_db import init_db  # type: ignore
from yume_runtime import start_background_tasks  # type: ignore


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
    "cogs.aby_environment",
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

    # Phase0: start background loops (safe to call multiple times).
    try:
        start_background_tasks(bot)
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("백그라운드 작업 시작 중 오류: %s", e)
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

    # Phase0: ensure DB schema exists before loading cogs.
    try:
        init_db()
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("DB 초기화 실패: %s", e)
        return

    logger.info("로드할 Cog 확장 목록: %s", EXTENSIONS)

    setup_yume_ai(bot)

    # systemd stop/restart 과정에서 SIGINT/SIGTERM이 들어올 때,
    # asyncio.run 기본 SIGINT 처리(KeyboardInterrupt)로 인해
    # CancelledError/KeyboardInterrupt Traceback이 journalctl에 찍히는 경우가 있다.
    # 여기서 이벤트 루프의 시그널 핸들러를 우리가 다시 등록해서
    # "Traceback 폭발" 없이 조용히 종료하도록 만든다.
    loop = asyncio.get_running_loop()
    _shutdown_called = {"v": False}

    def _request_shutdown(signame: str) -> None:
        if _shutdown_called["v"]:
            return
        _shutdown_called["v"] = True
        logger.info("종료 신호(%s) 수신: 유메를 종료합니다.", signame)
        try:
            loop.create_task(bot.close())
        except Exception:  # pylint: disable=broad-except
            # 루프가 이미 닫히는 중이거나, close 예약이 실패해도 종료는 진행된다.
            pass

    def _install_signal(sig: int, name: str) -> None:
        try:
            loop.add_signal_handler(sig, _request_shutdown, name)
            return
        except (NotImplementedError, RuntimeError):
            # Windows 등에서는 add_signal_handler가 지원되지 않을 수 있다.
            pass
        try:
            signal.signal(sig, lambda _s, _f, _name=name: _request_shutdown(_name))
        except Exception:  # pylint: disable=broad-except
            pass

    # Linux(systemd) 기준: SIGTERM(기본) + SIGINT(KillSignal=SIGINT 같은 설정)
    if hasattr(signal, "SIGTERM"):
        _install_signal(signal.SIGTERM, "SIGTERM")
    if hasattr(signal, "SIGINT"):
        _install_signal(signal.SIGINT, "SIGINT")

    async with bot:
        for ext in EXTENSIONS:
            try:
                await bot.load_extension(ext)
                logger.info("확장 로드 성공: %s", ext)
            except Exception as e:  # pylint: disable=broad-except
                logger.exception("확장 로드 실패: %s (%s)", ext, e)

        # 시그널 핸들러에서 bot.close()를 호출하면 bot.start()가 조용히 반환된다.
        # (KeyboardInterrupt/CancelledError를 최대한 바깥으로 새지 않게)
        try:
            await bot.start(token)
        except asyncio.CancelledError:
            logger.info("CancelledError로 종료합니다.")
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
