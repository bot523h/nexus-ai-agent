from telegram import Update
from telegram.ext import ContextTypes
from nexus_ai_agent.knowledge.knowledge_manager import KnowledgeManager

async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("لطفاً یک موضوع برای یادگیری وارد کنید. مثال: /learn هوش مصنوعی")
        return
    
    query = " ".join(context.args)
    await update.message.reply_text(f"🔍 در حال یادگیری در مورد '{query}'...")
    
    km = KnowledgeManager()
    try:
        summary = await km.learn(query)
        await update.message.reply_text(summary)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در یادگیری: {e}")
    finally:
        await km.close()

async def wiki_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("لطفاً یک موضوع برای جستجو در ویکی‌پدیا وارد کنید.")
        return
    
    query = " ".join(context.args)
    km = KnowledgeManager()
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
    if not context.args:
        await update.message.reply_text("لطفاً یک موضوع برای جستجو در وب وارد کنید.")
        return
    
    query = " ".join(context.args)
    km = KnowledgeManager()
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
