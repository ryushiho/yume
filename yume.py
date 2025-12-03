from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import commands

from yume_ai import setup_yume_ai  # 유메 감정/말투/일기 엔진

# --------------------------------
# 로깅 설정
# --------------------------------
logger = logging.getLogger("yume")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


# --------------------------------
# 토큰 로딩 유틸
# --------------------------------
def _load_env_file_manual(path: str) -> None:
    """dotenv 없어도 .env, yumebot.env에서 key=value 읽어서 os.environ에 넣어줌."""
    if not os.path.exists(path):
        return

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
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("환경 파일 %s 읽는 중 오류: %s", path, e)


def _load_env_files_with_dotenv(root_dir: str) -> None:
    """python-dotenv가 있으면 사용, 없으면 수동 파싱."""
    try:
        from dotenv import load_dotenv  # type: ignore

        env_path = os.path.join(root_dir, ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=False)

        yume_env_path = os.path.join(root_dir, "yumebot.env")
        if os.path.exists(yume_env_path):
            load_dotenv(yume_env_path, override=False)

    except ImportError:
        env_path = os.path.join(root_dir, ".env")
        yume_env_path = os.path.join(root_dir, "yumebot.env")
        _load_env_file_manual(env_path)
        _load_env_file_manual(yume_env_path)


def resolve_discord_token() -> Optional[str]:
    """
    DISCORD_TOKEN 찾기 우선순위:
      1) config.py 의 DISCORD_TOKEN
      2) .env / yumebot.env
      3) 환경변수 DISCORD_TOKEN
    """
    # 1) config.py
    token_from_config: Optional[str] = None
    try:
        from config import DISCORD_TOKEN as CFG_TOKEN  # type: ignore

        if isinstance(CFG_TOKEN, str) and CFG_TOKEN.strip():
            token_from_config = CFG_TOKEN.strip()
            logger.info("config.py 에서 DISCORD_TOKEN 을 불러왔습니다.")
    except Exception:
        token_from_config = None

    if token_from_config:
        return token_from_config

    # 2) env 파일
    root_dir = os.path.dirname(os.path.abspath(__file__))
    _load_env_files_with_dotenv(root_dir)

    # 3) 환경변수
    token = os.getenv("DISCORD_TOKEN")
    if token and token.strip():
        logger.info("환경변수에서 DISCORD_TOKEN 을 불러왔습니다.")
        return token.strip()

    return None


# --------------------------------
# Bot 설정
# --------------------------------
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

# 개발자(너) ID – /sync 권한 체크용
DEV_USER_ID = 1433962010785349634

# 서버에서 로드할 Cog 목록
EXTENSIONS = [
    "cogs.admin",
    "cogs.blue_war",
    "cogs.feedback",
    "cogs.help",
    "cogs.music",
    "cogs.reactions",
]


# --------------------------------
# /sync 슬래시 명령어 (yume.py 직결)
# --------------------------------
@bot.tree.command(
    name="sync",
    description="유메의 슬래시 명령어를 동기화하거나, 길드 중복을 정리해요. (개발자 전용)",
)
@app_commands.describe(
    scope="global(전체 동기화) / cleanup(현재 서버 슬래시 중복 정리). 기본값: global",
)
async def sync_slash(
    interaction: discord.Interaction,
    scope: Literal["global", "cleanup"] = "global",
):
    """
    /sync
      - scope = global  : 전체 슬래시 명령어 전역 동기화
      - scope = cleanup : 이 서버에 쌓인 '길드 전용' 슬래시 명령어를 비워서
                          전역 명령어만 남기도록 정리
    """

    if interaction.user.id != DEV_USER_ID:
        await interaction.response.send_message(
            "이 명령어는 개발자만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    # cleanup 모드: 이 길드에 남아 있는 guild 슬래시 명령어 정리
    if scope == "cleanup":
        if interaction.guild is None:
            await interaction.response.send_message(
                "cleanup 은 서버 안에서만 사용할 수 있어요.",
                ephemeral=True,
            )
            return

        try:
            interaction.client.tree.clear_commands(guild=interaction.guild)
            await interaction.client.tree.sync(guild=interaction.guild)

            await interaction.response.send_message(
                "🧹 이 서버의 길드 전용 슬래시 명령어를 정리했어요.\n"
                "이제 전역 슬래시 명령어만 보여야 해요.",
                ephemeral=True,
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("길드 슬래시 정리(cleanup) 중 오류: %s", e)
            await interaction.response.send_message(
                "❌ 길드 슬래시 정리 중 오류가 발생했어요.",
                ephemeral=True,
            )
        return

    # global 동기화 (기본)
    if scope == "global":
        try:
            synced = await interaction.client.tree.sync()
            await interaction.response.send_message(
                (
                    f"🌐 전역 기준으로 슬래시 명령어 {len(synced)}개를 동기화했어요.\n"
                    "모든 서버에 반영되기까지는 시간이 조금 걸릴 수 있어요."
                ),
                ephemeral=True,
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("전역 슬래시 동기화 중 오류: %s", e)
            await interaction.response.send_message(
                "❌ 전역 슬래시 동기화 중 오류가 발생했어요.",
                ephemeral=True,
            )
        return


# --------------------------------
# 이벤트
# --------------------------------
@bot.event
async def on_ready():
    logger.info("유메 로그인 완료: %s (%s)", bot.user, bot.user.id)
    # 상태메시지: "/도움"
    await bot.change_presence(activity=discord.Game(name="/도움"))

    # 슬래시 명령어 자동 동기화 (전역)
    try:
        synced = await bot.tree.sync()
        logger.info("슬래시 명령어 동기화 완료: %d개", len(synced))
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("슬래시 명령어 동기화 중 오류: %s", e)


# --------------------------------
# 메인 루프
# --------------------------------
async def main():
    token = resolve_discord_token()
    if not token:
        logger.error(
            "DISCORD_TOKEN 이 설정되어 있지 않습니다.\n"
            "다음 중 하나를 설정해 주세요:\n"
            "  1) config.py 에 DISCORD_TOKEN = '...' 추가\n"
            "  2) .env 또는 yumebot.env 파일에 DISCORD_TOKEN=... 추가\n"
            "  3) 환경변수 DISCORD_TOKEN 설정"
        )
        return

    logger.info("로드할 Cog 확장 목록: %s", EXTENSIONS)

    # 유메 감정/말투/일기 엔진 초기화 (얇게 켜두기)
    setup_yume_ai(bot)

    async with bot:
        for ext in EXTENSIONS:
            try:
                await bot.load_extension(ext)
                logger.info("확장 로드 성공: %s", ext)
            except Exception as e:  # pylint: disable=broad-except
                logger.exception("확장 로드 실패: %s (%s)", ext, e)

        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
