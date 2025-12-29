from __future__ import annotations

import datetime
import random

from discord.ext import commands

from yume_honorific import get_honorific
from yume_llm import generate_survival_meal
from yume_send import send_ctx
from yume_store import get_daily_meal, get_world_state, upsert_daily_meal


KST = datetime.timezone(datetime.timedelta(hours=9))


WEATHER_LABEL = {
    "clear": "맑음",
    "cloudy": "흐림",
    "sandstorm": "대형 모래폭풍",
}


BASE_INGREDIENTS = [
    "유통기한 임박한 건빵",
    "정체불명의 통조림",
    "미지근한 물",
    "반쯤 부서진 컵라면",
    "딱딱해진 초코바",
    "모래맛이 살짝 나는 젤리",
]


def _today_kst_ymd() -> str:
    return datetime.datetime.now(tz=KST).strftime("%Y-%m-%d")


class SurvivalCookingCog(commands.Cog):
    """Phase4: '상상 급식표' (Survival Cooking)

    - !급식 / !점심 : 현실은 초라해도, 유메가 레스토랑 메뉴처럼 포장해줘요.
    - 1일 1회(날짜 기준) 캐시해서 비용을 줄이고, 매일 메뉴가 바뀌는 느낌을 유지해요.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="급식", aliases=["점심"])
    async def lunch(self, ctx: commands.Context) -> None:
        honorific = get_honorific(ctx.author, ctx.guild)
        date_ymd = _today_kst_ymd()

        # Weather label (for prompt flavor)
        try:
            state = get_world_state()
            weather = str(state.get("weather") or "clear")
        except Exception:
            weather = "clear"
        weather_label = WEATHER_LABEL.get(weather, weather)

        # Cache hit?
        cached = None
        try:
            cached = get_daily_meal(date_ymd)
        except Exception:
            cached = None

        if cached and cached.get("meal_text"):
            meal_text = str(cached["meal_text"])
        else:
            base = random.choice(BASE_INGREDIENTS)
            meal_text = ""
            try:
                meal_text = generate_survival_meal(date_ymd=date_ymd, base_ingredient=base, weather_label=weather_label)
            except Exception:
                meal_text = ""

            if not meal_text:
                meal_text = (
                    "**'Double-Baked Wheat Cracker with Desert Air' (두 번 구운 건빵과 사막 공기 곁들임)**\n"
                    "바삭함은 확실해! 목이 좀 막힐 수도 있지만… 그게 또 매력이지, 에헤헤~ 🌵"
                )

            try:
                upsert_daily_meal(date_ymd, meal_text)
            except Exception:
                # Cache failure shouldn't block the command.
                pass

        msg = f"{honorific}~ 오늘의 상상 급식표는… 짜잔!\n{meal_text}"
        await send_ctx(ctx, msg, allow_glitch=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SurvivalCookingCog(bot))
