import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .. import medications as medications_service
from .. import reminders as reminders_service
from .. import workouts as workouts_service
from ..intent import classify_and_respond, transcribe_voice
from ..memory import (
    add_memory,
    append_message,
    delete_memory,
    list_memories,
    search_memories,
)
from ..scheduler import (
    add_medication_job,
    remove_medication_job,
)
from .auth import owner_only

MAX_VOICE_DURATION_S = 60
MAX_PHOTO_SIZE_MB = 8

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


def _format_workout_detail(workout: dict) -> str:
    lines = [f"💪 *{workout['name']}*"]
    if workout.get("description"):
        lines.append(workout["description"])
    if not workout.get("exercises"):
        lines.append("(sem exercícios cadastrados)")
        return "\n".join(lines)
    for ex in workout["exercises"]:
        sets = ex.get("sets")
        reps = ex.get("reps")
        weight = ex.get("target_weight")
        parts = [ex["name"]]
        if sets and reps:
            parts.append(f"{sets}x{reps}")
        elif sets:
            parts.append(f"{sets} séries")
        elif reps:
            parts.append(f"{reps} reps")
        if weight:
            parts.append(f"{_format_weight(weight)}")
        notes = ex.get("notes")
        line = "• " + " — ".join(parts)
        if notes:
            line += f" ({notes})"
        lines.append(line)
    return "\n".join(lines)


def _format_weight(w: float | None) -> str:
    if w is None:
        return ""
    if float(w).is_integer():
        return f"{int(w)}kg"
    return f"{w:g}kg"


def _format_schedule(schedule: dict[int, dict | None]) -> str:
    lines = ["📅 Sua semana:"]
    for idx in range(7):
        label = workouts_service.WEEKDAY_SHORT[idx]
        if idx not in schedule:
            lines.append(f"• {label} — (não definido)")
        elif schedule[idx] is None:
            lines.append(f"• {label} — descanso")
        else:
            lines.append(f"• {label} — {schedule[idx]['name']}")
    return "\n".join(lines)


def _format_history(history: list[dict]) -> str:
    if not history:
        return "Sem histórico ainda — esse exercício ainda não foi registrado em nenhuma sessão encerrada."
    lines = []
    for sess in history:
        date = sess["started_at"][:10] if sess.get("started_at") else "?"
        sets_str = ", ".join(
            (
                f"{_format_weight(s['weight_used'])} x {s['reps_done']}"
                if s.get("weight_used") and s.get("reps_done")
                else (
                    f"{s['reps_done']} reps"
                    if s.get("reps_done")
                    else _format_weight(s.get("weight_used"))
                )
            )
            for s in sess["sets"]
        )
        lines.append(f"{date} — {sets_str}")
    return "📊 Histórico:\n" + "\n".join(lines)


def _summarize_session(summary: dict) -> str:
    logs = summary.get("logs") or []
    if not logs:
        return "Sessão encerrada (sem séries registradas)."
    by_exercise: dict[str, list[dict]] = {}
    for log_row in logs:
        by_exercise.setdefault(log_row["exercise_name"], []).append(log_row)
    total_sets = len(logs)
    total_volume = sum(
        (lg.get("weight_used") or 0) * (lg.get("reps_done") or 0) for lg in logs
    )
    parts = [f"✅ Treino encerrado — {total_sets} séries, {len(by_exercise)} exercícios"]
    if total_volume:
        parts.append(f"Volume total: {int(total_volume)}kg")
    for name, sets_list in by_exercise.items():
        line = f"• {name}: " + ", ".join(
            (
                f"{_format_weight(s.get('weight_used'))}x{s.get('reps_done')}"
                if s.get("weight_used") and s.get("reps_done")
                else f"{s.get('reps_done')} reps" if s.get("reps_done") else "1 set"
            )
            for s in sets_list
        )
        parts.append(line)
    return "\n".join(parts)


_WORKOUT_INTENTS = {
    "create_workout",
    "list_workouts",
    "show_workout",
    "delete_workout",
    "set_workout_schedule",
    "show_workout_schedule",
    "start_workout_session",
    "log_set",
    "finish_workout_session",
    "cancel_workout_session",
    "show_exercise_history",
    "start_rest_timer",
    "workout_stats",
}

_MEDICATION_INTENTS = {
    "create_medication",
    "list_medications",
    "delete_medication",
    "track_medication",
    "untrack_medication",
    "medication_compliance",
}


def _resolve_medication(name: str | None) -> tuple[dict | None, str | None]:
    """Tenta resolver um medicamento por nome; se name=None, tenta inferir.

    Retorna (med, error_msg). Se med for None, error_msg explica o porquê.
    """
    if name:
        med = medications_service.get_by_name(name)
        if med is None:
            return None, f"Não achei medicamento \"{name}\" ativo."
        return med, None
    solo = medications_service.find_solo_active()
    if solo is None:
        actives = medications_service.list_active()
        if not actives:
            return None, "Você não tem medicamentos cadastrados."
        names = ", ".join(m["name"] for m in actives)
        return None, f"Tem mais de um medicamento ativo ({names}). Diz qual."
    return medications_service.get_by_id(solo["id"]), None


async def _dispatch_medication(intent_data: dict) -> str:
    intent = intent_data["intent"]
    reply = intent_data.get("reply", "").strip()

    if intent == "create_medication":
        name = intent_data["medication_name"]
        cron = intent_data["cron"]
        existing = medications_service.get_by_name(name)
        if existing:
            return (
                f"Já tem um medicamento chamado \"{name}\". "
                "Apaga antes (\"para de me lembrar do {name}\") e cria de novo."
            )
        try:
            med_id = medications_service.create(name, cron)
            add_medication_job(med_id, name, cron)
        except Exception as e:
            log.exception("Falha ao criar medicamento")
            return f"Não consegui agendar (cron inválido?): {e}"
        return reply or (
            f"💊 *{name}* cadastrado. Vou te lembrar com botões nos horários definidos."
        )

    if intent == "list_medications":
        rows = medications_service.list_active()
        if not rows:
            return "Nenhum medicamento cadastrado."
        lines = [f"💊 Seus medicamentos ({len(rows)}):"]
        for r in rows:
            lines.append(f"• {r['name']} — cron: `{r['schedule_cron']}`")
        return "\n".join(lines)

    if intent == "delete_medication":
        med, err = _resolve_medication(intent_data.get("medication_name"))
        if err:
            return err
        ok = medications_service.deactivate(med["id"])
        if ok:
            try:
                remove_medication_job(med["id"])
            except Exception:
                log.exception("Falha ao remover job do medicamento %s", med["id"])
        return reply or (
            f"Removido. Não vou mais te lembrar do {med['name']}."
            if ok
            else "Falhou ao remover."
        )

    if intent == "track_medication":
        med, err = _resolve_medication(intent_data.get("medication_name"))
        if err:
            return err
        if medications_service.has_intake_today(med["id"]):
            return f"Já estava registrado que você tomou {med['name']} hoje ✅"
        medications_service.track(med["id"], skipped=False)
        return reply or f"💊 {med['name']}: registrado como tomado ✅"

    if intent == "untrack_medication":
        med, err = _resolve_medication(intent_data.get("medication_name"))
        if err:
            return err
        removed = medications_service.untrack_today(med["id"])
        if removed:
            return reply or f"Tomada de hoje do {med['name']} apagada."
        return f"Não tinha registro de tomada de hoje pro {med['name']}."

    if intent == "medication_compliance":
        med, err = _resolve_medication(intent_data.get("medication_name"))
        if err:
            return err
        days = intent_data.get("days", 30)
        result = medications_service.compliance(med["id"], days=days)
        bar_filled = int(result["percent"] // 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        body = (
            f"💊 *{med['name']}* — últimos {result['total']} dias\n"
            f"{result['taken']}/{result['total']} dias tomados ({result['percent']}%)\n"
            f"{bar}"
        )
        if result["taken"] and result["taken"] <= 12:
            body += "\n\nDias: " + ", ".join(result["dates_taken"])
        prefix = (reply + "\n\n") if reply else ""
        return prefix + body

    return reply or "(sem resposta)"


async def _dispatch_workout(intent_data: dict) -> str:
    intent = intent_data["intent"]
    reply = intent_data.get("reply", "").strip()

    if intent == "create_workout":
        name = intent_data["workout_name"]
        existing = workouts_service.get_workout_by_name(name)
        try:
            if existing:
                workouts_service.replace_workout_exercises(
                    existing["id"], intent_data["exercises"]
                )
                workout = workouts_service.get_workout(existing["id"])
                base = reply or f"Treino *{name}* atualizado."
            else:
                wid = workouts_service.create_workout(
                    name,
                    intent_data.get("workout_description"),
                    intent_data["exercises"],
                )
                workout = workouts_service.get_workout(wid)
                base = reply or f"Treino *{name}* criado."
        except Exception as e:
            log.exception("Falha ao criar/atualizar treino")
            return f"Não consegui salvar o treino: {e}"
        return f"{base}\n\n{_format_workout_detail(workout)}"

    if intent == "list_workouts":
        rows = workouts_service.list_workouts()
        if not rows:
            return "Nenhum treino cadastrado ainda."
        lines = [f"💪 Seus treinos ({len(rows)}):"]
        for r in rows:
            desc = f" — {r['description']}" if r.get("description") else ""
            lines.append(f"• {r['name']}{desc}")
        return "\n".join(lines)

    if intent == "show_workout":
        name = intent_data["workout_name"]
        workout = workouts_service.get_workout_by_name(name)
        if not workout:
            return f"Não achei treino chamado \"{name}\". Confere o nome com 'meus treinos'."
        return _format_workout_detail(workout)

    if intent == "delete_workout":
        name = intent_data["workout_name"]
        workout = workouts_service.get_workout_by_name(name)
        if not workout:
            return f"Não achei treino \"{name}\"."
        ok = workouts_service.delete_workout(workout["id"])
        return reply or (f"Treino \"{name}\" apagado." if ok else "Falhou ao apagar.")

    if intent == "set_workout_schedule":
        results = []
        for assignment in intent_data["schedule_assignments"]:
            wd = workouts_service.parse_weekday(assignment.get("weekday"))
            if wd is None:
                results.append(f"⚠️ dia inválido: {assignment.get('weekday')}")
                continue
            wname = assignment.get("workout_name")
            if wname:
                workout = workouts_service.get_workout_by_name(wname)
                if not workout:
                    results.append(
                        f"⚠️ {workouts_service.WEEKDAY_NAMES[wd]}: treino \"{wname}\" não existe"
                    )
                    continue
                workouts_service.set_schedule_for_weekday(wd, workout["id"])
                results.append(
                    f"✅ {workouts_service.WEEKDAY_NAMES[wd]}: {workout['name']}"
                )
            else:
                workouts_service.set_schedule_for_weekday(wd, None)
                results.append(f"✅ {workouts_service.WEEKDAY_NAMES[wd]}: descanso")
        body = "\n".join(results)
        prefix = (reply + "\n\n") if reply else ""
        return prefix + body

    if intent == "show_workout_schedule":
        scope = intent_data.get("scope", "today")
        if scope == "today":
            workout = workouts_service.get_today_workout()
            if workout is None:
                schedule = workouts_service.get_schedule()
                today_idx = datetime.now().weekday()
                if today_idx in schedule and schedule[today_idx] is None:
                    return "Hoje é descanso. Aproveita 💆"
                return "Não tem treino agendado pra hoje."
            return f"Treino de hoje:\n\n{_format_workout_detail(workout)}"
        if scope == "weekday":
            wd = workouts_service.parse_weekday(intent_data.get("weekday"))
            if wd is None:
                return "Não entendi qual dia."
            schedule = workouts_service.get_schedule()
            if wd not in schedule:
                return f"{workouts_service.WEEKDAY_NAMES[wd]}: nada agendado."
            if schedule[wd] is None:
                return f"{workouts_service.WEEKDAY_NAMES[wd]}: descanso."
            workout = workouts_service.get_workout(schedule[wd]["id"])
            return f"{workouts_service.WEEKDAY_NAMES[wd]}:\n\n{_format_workout_detail(workout)}"
        return _format_schedule(workouts_service.get_schedule())

    if intent == "start_workout_session":
        name = intent_data.get("workout_name")
        workout = None
        if name:
            workout = workouts_service.get_workout_by_name(name)
            if not workout:
                return f"Não achei treino \"{name}\". Quer criar antes?"
        else:
            workout = workouts_service.get_today_workout()
        try:
            sid = workouts_service.start_session(workout["id"] if workout else None)
        except workouts_service.ActiveSessionExists as exc:
            active = exc.session
            return (
                f"Já tem uma sessão ativa (#{active['id']} — "
                f"{active.get('workout_name') or 'sem treino'}). "
                "Encerra com 'acabei' ou cancela antes."
            )
        if workout:
            return (
                f"🏋️ Bora! Sessão #{sid} começou — Treino {workout['name']}.\n\n"
                f"{_format_workout_detail(workout)}\n\n"
                "Manda as séries assim: \"supino 60 por 10\""
            )
        return f"🏋️ Sessão #{sid} começou (sem treino vinculado). Manda as séries que for fazendo."

    if intent == "log_set":
        ex_name = intent_data.get("exercise_name", "").strip()
        if not ex_name:
            return "Qual exercício? Ex: \"supino 60 por 10\"."
        try:
            result = workouts_service.log_set(
                ex_name,
                reps_done=intent_data.get("reps_done"),
                weight_used=intent_data.get("weight_used"),
                notes=intent_data.get("notes"),
            )
        except workouts_service.NoActiveSession:
            return "Você não tá em nenhum treino ativo. Manda \"vou treinar X\" antes."
        except Exception as e:
            log.exception("Falha ao logar série")
            return f"Falhou ao registrar: {e}"

        bits = [f"✅ {ex_name}"]
        weight = intent_data.get("weight_used")
        reps = intent_data.get("reps_done")
        if weight is not None and reps is not None:
            bits.append(f"{_format_weight(weight)} x {reps}")
        elif weight is not None:
            bits.append(_format_weight(weight))
        elif reps is not None:
            bits.append(f"{reps} reps")
        bits.append(f"(série {result['set_number']})")
        line = " — ".join(bits)
        if result["is_pr"]:
            prev = result["prev_max_weight"]
            prev_str = (
                f"{_format_weight(prev)}"
                if prev is not None
                else "(nenhum registro anterior)"
            )
            line += f"\n🏆 PR! anterior: {prev_str}"
        return line

    if intent == "finish_workout_session":
        active = workouts_service.get_active_session()
        if not active:
            return "Não tem sessão ativa pra encerrar."
        summary = workouts_service.finish_session(active["id"])
        if summary is None:
            return "Falhou ao encerrar (talvez já estivesse encerrada)."
        return _summarize_session(summary)

    if intent == "cancel_workout_session":
        active = workouts_service.get_active_session()
        if not active:
            return "Não tem sessão ativa pra cancelar."
        ok = workouts_service.cancel_session(active["id"])
        return reply or (
            f"Sessão #{active['id']} cancelada (séries logadas foram apagadas)."
            if ok
            else "Não consegui cancelar."
        )

    if intent == "show_exercise_history":
        ex_name = intent_data["exercise_name"]
        history = workouts_service.get_exercise_history(ex_name)
        body = _format_history(history)
        prefix = (reply + "\n\n") if reply else f"Histórico de {ex_name}:\n\n"
        return prefix + body

    if intent == "start_rest_timer":
        seconds = intent_data["seconds"]
        run_at = datetime.now() + timedelta(seconds=seconds)
        try:
            rid, _ = reminders_service.create_one_shot(
                f"⏱️ Descanso de {seconds}s acabou — próxima série!", run_at
            )
        except Exception as e:
            log.exception("Falha ao criar timer de descanso")
            return f"Falhou ao agendar timer: {e}"
        return reply or f"⏱️ Timer de {seconds}s iniciado. Te aviso quando acabar."

    if intent == "workout_stats":
        days = intent_data.get("days", 30)
        stats = workouts_service.get_workout_stats(days=days)
        if stats["total"] == 0:
            return f"Você não tem treinos completos nos últimos {stats['days']} dias."
        lines = [
            f"📊 Treinos — últimos {stats['days']} dias",
            f"• Total: {stats['total']} sessão(ões)",
        ]
        if stats["per_week_avg"] is not None:
            lines.append(f"• Frequência: ~{stats['per_week_avg']}/semana")
        if stats["last_session_at"]:
            last = stats["last_session_at"][:10]
            lines.append(f"• Última: {last}")
        if stats["streak"] > 0:
            label = "dia" if stats["streak"] == 1 else "dias"
            lines.append(f"🔥 Streak: {stats['streak']} {label} consecutivos")
        else:
            lines.append("🔥 Streak: 0 (treine hoje pra começar)")
        if stats["by_workout"]:
            lines.append("")
            lines.append("Por treino:")
            for name, count in sorted(
                stats["by_workout"].items(), key=lambda x: -x[1]
            ):
                lines.append(f"  • {name}: {count}x")
        prefix = (reply + "\n\n") if reply else ""
        return prefix + "\n".join(lines)

    return reply or "(sem resposta)"


async def _dispatch(intent_data: dict) -> str:
    intent = intent_data["intent"]
    reply = intent_data.get("reply", "").strip()

    if intent in _WORKOUT_INTENTS:
        return await _dispatch_workout(intent_data)

    if intent in _MEDICATION_INTENTS:
        return await _dispatch_medication(intent_data)

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


async def _run_intent_pipeline(
    message: Message,
    user_text: str,
    image_path: str | None = None,
    image_mime: str = "image/jpeg",
) -> None:
    append_message("user", user_text or "(imagem)")

    try:
        intent_data = await classify_and_respond(
            user_text, image_path=image_path, image_mime=image_mime
        )
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


@owner_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.photo:
        return

    # PhotoSize maior (Telegram envia múltiplas resoluções)
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_PHOTO_SIZE_MB * 1024 * 1024:
        await message.reply_text(
            f"Imagem muito grande ({photo.file_size // (1024 * 1024)}MB). "
            f"Limite de {MAX_PHOTO_SIZE_MB}MB."
        )
        return

    caption = (message.caption or "").strip()

    await context.bot.send_chat_action(
        chat_id=message.chat_id, action=ChatAction.TYPING
    )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(custom_path=str(tmp_path))

        await _run_intent_pipeline(
            message,
            caption,
            image_path=str(tmp_path),
            image_mime="image/jpeg",
        )
    except Exception as e:
        log.exception("Falha ao processar foto")
        await message.reply_text(f"Não consegui processar a imagem: {e}")
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                log.warning("Falha ao remover %s", tmp_path)
