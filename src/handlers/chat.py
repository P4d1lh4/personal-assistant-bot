import logging
import tempfile
from datetime import datetime
from pathlib import Path

from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .. import reminders as reminders_service
from ..intent import classify_and_respond, transcribe_voice
from ..memory import (
    add_memory,
    append_message,
    delete_memory,
    list_memories,
    search_memories,
)
from .auth import owner_only

MAX_VOICE_DURATION_S = 60

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

    if intent == "search_memories":
        query = intent_data.get("query", "").strip()
        if not query:
            return reply or "O que você quer que eu busque?"
        rows = search_memories(query)
        if not rows:
            return f"Não achei nada sobre \"{query}\" nas memórias."
        listing = _format_memories(rows)
        prefix = (reply + "\n\n") if reply else f"Achei {len(rows)} sobre \"{query}\":\n\n"
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


async def _run_intent_pipeline(message: Message, user_text: str) -> None:
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
    await _run_intent_pipeline(message, user_text)


@owner_only
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.voice is None:
        return

    voice = message.voice
    if voice.duration and voice.duration > MAX_VOICE_DURATION_S:
        await message.reply_text(
            f"Áudio muito longo ({voice.duration}s). Limite de {MAX_VOICE_DURATION_S}s — "
            "tenta uma mensagem mais curta."
        )
        return

    await context.bot.send_chat_action(
        chat_id=message.chat_id, action=ChatAction.TYPING
    )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(custom_path=str(tmp_path))

        try:
            transcription = await transcribe_voice(
                str(tmp_path), mime_type=voice.mime_type or "audio/ogg"
            )
        except Exception as e:
            log.exception("Falha ao transcrever áudio")
            await message.reply_text(f"Não consegui transcrever o áudio: {e}")
            return

        # Mostra a transcrição pro usuário antes de processar — útil pra debug
        # e pra ele confirmar que entendeu o que falou.
        await message.reply_text(f"🎙️ \"{transcription}\"")
        await _run_intent_pipeline(message, transcription)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                log.warning("Falha ao remover %s", tmp_path)
