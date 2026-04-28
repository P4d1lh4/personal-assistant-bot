import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import TELEGRAM_TOKEN
from .db import init_db
from .handlers.chat import handle_message, handle_photo, handle_voice
from .handlers.commands import (
    cmd_esquecer,
    cmd_help,
    cmd_lembrar,
    cmd_listar,
    cmd_rotina,
    cmd_start,
)
from .handlers.activities import handle_activity_callback
from .handlers.inline_buttons import handle_reminder_callback
from .handlers.medications import handle_medication_callback
from .handlers.reminders import cmd_agenda, cmd_cancelar, cmd_lembrete
from .scheduler import (
    init_scheduler,
    load_medications_from_db,
    load_reminders_from_db,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# Reduzir ruído de libs externas
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

log = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    init_scheduler(app)
    load_reminders_from_db()
    load_medications_from_db()
    log.info("Bot pronto. Aguardando mensagens.")


def build_app() -> Application:
    init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lembrar", cmd_lembrar))
    app.add_handler(CommandHandler("rotina", cmd_rotina))
    app.add_handler(CommandHandler("listar", cmd_listar))
    app.add_handler(CommandHandler("esquecer", cmd_esquecer))
    app.add_handler(CommandHandler("lembrete", cmd_lembrete))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_handler(
        CallbackQueryHandler(handle_medication_callback, pattern=r"^med_(taken|skip):")
    )
    app.add_handler(
        CallbackQueryHandler(handle_reminder_callback, pattern=r"^rem_cancel:")
    )
    app.add_handler(
        CallbackQueryHandler(handle_activity_callback, pattern=r"^act_(done|skip):")
    )

    return app


def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
