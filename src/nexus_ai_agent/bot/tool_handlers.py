from telegram import Update
from telegram.ext import ContextTypes
from nexus_ai_agent.integrations.free_tools import WeatherTool, CurrencyTool, NewsTool, YouTubeSearchTool
from nexus_ai_agent.config.settings import settings

async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("لطفاً نام شهر را وارد کنید. مثال: /weather Tehran")
        return
    city = context.args[0]
    tool = WeatherTool()
    data = await tool.get_weather(city)
    if data:
        current = data['current_condition'][0]
        temp = current['temp_C']
        desc = current['weatherDesc'][0]['value']
        await update.message.reply_text(f"🌡 دمای فعلی {city}: {temp}°C\n☁️ وضعیت: {desc}")
    else:
        await update.message.reply_text("❌ خطا در دریافت اطلاعات آب‌وهوا.")

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    base = context.args[0] if context.args else "USD"
    tool = CurrencyTool()
    rate = await tool.get_rate(base)
    if rate:
        # Convert IRR to IRT (approx)
        toman = rate / 10
        await update.message.reply_text(f"💰 نرخ {base.upper()} به تومان: {toman:,.0f} تومان")
    else:
        await update.message.reply_text("❌ خطا در دریافت نرخ ارز.")

async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args) if context.args else "ایران"
    tool = NewsTool(api_key=settings.NEWS_API_KEY)
    articles = await tool.get_news(query)
    if articles:
        response = f"📰 آخرین اخبار '{query}':\n\n"
        for art in articles:
            response += f"🔹 {art['title']}\n🔗 {art['url']}\n\n"
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("😔 خبری یافت نشد.")

async def youtube_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("لطفاً موضوعی برای جستجو در یوتیوب وارد کنید.")
        return
    query = " ".join(context.args)
    tool = YouTubeSearchTool()
    videos = await tool.search(query)
    if videos:
        response = f"🎥 نتایج یوتیوب برای '{query}':\n\n"
        for vid in videos:
            response += f"🔹 {vid['title']}\n🔗 {vid['url']}\n\n"
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("😔 ویدیویی یافت نشد.")
