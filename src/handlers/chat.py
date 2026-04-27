import logging
from datetime import datetime

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .. import reminders as reminders_service
from ..intent import classify_and_respond
from ..memory import (
    add_memory,
    append_message,
    delete_memory,
    list_memories,
)
from .auth import owner_only

log = logging.getLogger(__name__)


def _format_memories(rows: list[dict]) -> str:
    if not rows:
        return "Não tenho nada anotado ainda."
    lines = [f"#{r['id']} [{r['category']}] {r['content']}" for r in rows]
    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500] + "\n... (truncado)"
    return text


def _format_reminders(rows: list[dict]) -> str:
    if not rows:
        return "Nenhum lembrete ativo."
    lines = []
    for r in rows:
        kind = " (recorrente)" if r["is_recurring"] else ""
        lines.append(f"#{r['id']} [{r['cron_or_at']}] {r['text']}{kind}")
    return "\n".join(lines)


async def _dispatch(intent_data: dict) -> str:
    intent = intent_data["intent"]
    reply = intent_data.get("reply", "").strip()

    if intent == "chat":
        return reply or "(sem resposta)"

    if intent == "create_reminder":
        text = intent_data.get("text", "").strip()
        dt_iso = intent_data.get("datetime_iso", "").strip()
        if not text or not dt_iso:
            return "Faltou texto ou data/hora — pode repetir com mais detalhes?"
        try:
            run_at = datetime.fromisoformat(dt_iso)
        except ValueError:
            return f"Não entendi a data/hora ({dt_iso}). Pode reformular?"
        if run_at <= datetime.now():
            return "Esse horário já passou. Pode dar um momento futuro?"
        try:
            rid, _ = reminders_service.create_one_shot(text, run_at)
        except Exception as e:
            log.exception("Falha ao criar lembrete one-shot")
            return f"Falhou ao agendar: {e}"
        when_human = run_at.strftime("%d/%m/%Y às %H:%M")
        base = reply or f"⏰ Lembrete agendado para {when_human}: {text}"
        return f"{base}\n(id #{rid})"

    if intent == "create_recurring_reminder":
        text = intent_data.get("text", "").strip()
        cron = intent_data.get("cron", "").strip()
        if not text or not cron:
            return "Faltou texto ou frequência — pode repetir?"
        try:
            rid = reminders_service.create_recurring(text, cron)
        except Exception as e:
            log.exception("Falha ao criar lembrete recorrente")
            return f"Não consegui agendar (cron inválido?): {e}"
        base = reply or f"🔁 Lembrete recorrente criado ({cron}): {text}"
        return f"{base}\n(id #{rid})"

    if intent == "save_memory":
        text = intent_data.get("text", "").strip()
        category = intent_data.get("category", "fact")
        if not text:
            return "O que você quer que eu lembre?"
        mid = add_memory(category, text, source="manual")
        return reply or f"Anotado (id #{mid}, categoria: {category})."

    if intent == "list_memories":
        category = intent_data.get("category")
        rows = list_memories(category=category, limit=50)
        listing = _format_memories(rows)
        prefix = (reply + "\n\n") if reply else ""
        return prefix + listing

    if intent == "list_reminders":
        rows = reminders_service.list_active()
        listing = _format_reminders(rows)
        prefix = (reply + "\n\n") if reply else ""
        return prefix + listing

    if intent == "cancel_reminder":
        rid = intent_data.get("id")
        if not isinstance(rid, int):
            return "Qual lembrete? Me passa o número (ex: cancelar 3)."
        ok = reminders_service.cancel(rid)
        if ok:
            return reply or f"Lembrete #{rid} cancelado."
        return f"Lembrete #{rid} não encontrado ou já inativo."

    if intent == "delete_memory":
        mid = intent_data.get("id")
        if not isinstance(mid, int):
            return "Qual memória? Me passa o número."
        ok = delete_memory(mid)
        if ok:
            return reply or f"Memória #{mid} apagada."
        return f"Memória #{mid} não encontrada."

    return reply or "(sem resposta)"


@owner_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    user_text = message.text.strip()
    if not user_text:
        return

    await context.bot.send_chat_action(
        chat_id=message.chat_id, action=ChatAction.TYPING
    )

    append_message("user", user_text)

    try:
        intent_data = await classify_and_respond(user_text)
        final_reply = await _dispatch(intent_data)
    except Exception as e:
        log.exception("Erro ao processar mensagem")
        final_reply = f"Tive um erro: {e}"
        intent_data = {"intent": "chat"}

    append_message("assistant", final_reply)
    await message.reply_text(final_reply)

    # Fatos vieram na mesma chamada do classify — sem 2ª request ao Gemini.
    for fact in intent_data.get("extracted_facts", []):
        try:
            add_memory(fact["category"], fact["content"], source="auto")
        except Exception:
            log.exception("Falha ao salvar fato extraído: %r", fact)
