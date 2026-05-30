from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import logging

logger = logging.getLogger(__name__)

class FeedbackCollector:
    @staticmethod
    def get_feedback_keyboard(message_id: str):
        keyboard = [
            [
                InlineKeyboardButton("👍", callback_data=f"fb_up_{message_id}"),
                InlineKeyboardButton("👎", callback_data=f"fb_down_{message_id}")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def handle_feedback(self, update, context):
        query = update.callback_query
        data = query.data
        if data.startswith("fb_down"):
            await query.answer("ممنون از بازخورد شما. چه چیزی را می‌توانیم بهتر کنیم؟")
            # Log for future report
        else:
            await query.answer("ممنون از انرژی مثبت شما! ✨")
