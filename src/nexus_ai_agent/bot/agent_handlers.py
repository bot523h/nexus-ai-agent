from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from nexus_ai_agent.agents.store.agent_manager import AgentManager


async def agents_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the Agent Store menu."""
    agents = AgentManager.list_agents()
    keyboard = []
    # Create 2x5 grid
    for i in range(0, len(agents), 2):
        row = [
            InlineKeyboardButton(
                f"{agents[i]['emoji']} {agents[i]['id'].capitalize()}",
                callback_data=f"agent_select_{agents[i]['id']}",
            ),
        ]
        if i + 1 < len(agents):
            row.append(
                InlineKeyboardButton(
                    f"{agents[i+1]['emoji']} {agents[i+1]['id'].capitalize()}",
                    callback_data=f"agent_select_{agents[i+1]['id']}",
                )
            )
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("❌ غیرفعال کردن Agent", callback_data="agent_stop")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🤖 *Agent Store*\n\nیک دستیار متخصص انتخاب کنید تا تمام پیام‌های شما توسط او پاسخ داده شود:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def agent_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle agent selection and stopping."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "agent_stop":
        await AgentManager.deactivate(user_id)
        await query.edit_message_text("✅ Agent غیرفعال شد. حالا به حالت هوش مصنوعی معمولی برگشتیم.")
        return
    
    if query.data and query.data.startswith("agent_select_"):
        agent_id = query.data.replace("agent_select_", "")
        success = await AgentManager.activate(user_id, agent_id)
        if success:
            agent_list = AgentManager.list_agents()
            agent_info = next(a for a in agent_list if a["id"] == agent_id)
            await query.edit_message_text(
                f"✅ *{agent_info['name']}* فعال شد!\n\n"
                f"{agent_info['description']}\n\n"
                "از این به بعد من با این شخصیت پاسخ شما را می‌دهم. برای توقف: /agent_stop",
                parse_mode="Markdown"
            )


async def myagent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show currently active agent."""
    user_id = update.effective_user.id
    active_agent = await AgentManager.get_active(user_id)
    if active_agent:
        await update.message.reply_text(
            f"🤖 در حال حاضر **{active_agent.name}** فعال است.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ هیچ Agent فعالی ندارید. از /agents یکی انتخاب کنید.")


async def agent_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop active agent via command."""
    user_id = update.effective_user.id
    await AgentManager.deactivate(user_id)
    await update.message.reply_text("✅ Agent غیرفعال شد.")
