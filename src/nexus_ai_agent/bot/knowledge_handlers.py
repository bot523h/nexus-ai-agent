from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from nexus_ai_agent.knowledge.knowledge_manager import KnowledgeManager


def _shared_llm_provider(context: ContextTypes.DEFAULT_TYPE) -> Any | None:
    """Resolve the runtime-owned shared GeminiProvider from bot_data (W1).

    Falls back to ``None`` so the KnowledgeManager keeps its legacy
    self-construction seam when no runtime is wired (tests/standalone).
    The knowledge/ zone itself is claimed by another agent — the wiring
    happens HERE instead of inside that zone.
    """

    bot_data = getattr(getattr(context, "application", None), "bot_data", None)
    get = getattr(bot_data, "get", None)
    if get is None:
        return None
    return get("llm_provider")


async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text(
            "لطفاً یک موضوع برای یادگیری وارد کنید. مثال: /learn هوش مصنوعی"
        )
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"🔍 در حال یادگیری در مورد '{query}'...")

    km = KnowledgeManager(gemini_provider=_shared_llm_provider(context))
    try:
        summary = await km.learn(query)
        await update.message.reply_text(summary)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در یادگیری: {e}")
    finally:
        await km.close()


async def wiki_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("لطفاً یک موضوع برای جستجو در ویکی‌پدیا وارد کنید.")
        return

    query = " ".join(context.args)
    km = KnowledgeManager(gemini_provider=_shared_llm_provider(context))
    try:
        content = await km.wiki.fetch_summary(query)
        if content:
            await update.message.reply_text(content)
        else:
            await update.message.reply_text("😔 متأسفانه مطلبی پیدا نشد.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")
    finally:
        await km.close()


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("لطفاً یک موضوع برای جستجو در وب وارد کنید.")
        return

    query = " ".join(context.args)
    km = KnowledgeManager(gemini_provider=_shared_llm_provider(context))
    try:
        results = await km.web.search_and_summarize(query)
        if results:
            response = "🌐 نتایج جستجو:\n\n"
            for res in results:
                response += f"🔹 {res['title']}\n🔗 {res['url']}\n\n"
            await update.message.reply_text(response)
        else:
            await update.message.reply_text("😔 نتیجه‌ای یافت نشد.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")
    finally:
        await km.close()
