import asyncio
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime
from typing import Any

import google.generativeai as genai
from google.api_core.exceptions import (
    GoogleAPICallError,
    ResourceExhausted,
    RetryError,
    ServiceUnavailable,
)

from .config import GEMINI_API_KEY, GEMINI_MODEL
from .memory import get_memory_summary, get_recent_history

genai.configure(api_key=GEMINI_API_KEY)
log = logging.getLogger(__name__)

# Métricas leves em memória (resetam quando o processo reinicia)
_METRICS: dict[str, Any] = {
    "intent_counts": Counter(),
    "total_calls": 0,
    "total_errors": 0,
    "total_429": 0,
}


def get_metrics_snapshot() -> dict:
    """Retorna cópia das métricas em memória."""
    return {
        "intent_counts": dict(_METRICS["intent_counts"]),
        "total_calls": _METRICS["total_calls"],
        "total_errors": _METRICS["total_errors"],
        "total_429": _METRICS["total_429"],
    }

VALID_INTENTS = {
    "chat",
    "create_reminder",
    "create_recurring_reminder",
    "save_memory",
    "list_memories",
    "list_reminders",
    "search_memories",
    "cancel_reminder",
    "delete_memory",
    "show_status",
    # Treinos
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
    # Medicações
    "create_medication",
    "list_medications",
    "delete_medication",
    "track_medication",
    "untrack_medication",
    "medication_compliance",
    # Atividades diárias
    "create_activity",
    "list_activities",
    "delete_activity",
    "track_activity",
    "untrack_activity",
    "activity_compliance",
}

_RETRY_DELAY_RE = re.compile(r"retry in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _parse_retry_delay(err: Exception) -> int | None:
    match = _RETRY_DELAY_RE.search(str(err))
    if match:
        try:
            return int(float(match.group(1)))
        except ValueError:
            return None
    return None

VALID_CATEGORIES = {"fact", "preference", "routine", "goal", "habit"}


# O bloco abaixo é INTENCIONALMENTE estático para maximizar cache implícito do
# Gemini 2.5 (prefixo idêntico = cacheável). Conteúdo dinâmico (data/hora,
# memórias, histórico, mensagem atual) vai DEPOIS, em `_build_prompt`.
_STATIC_SYSTEM_PROMPT = """Você é um assistente pessoal. Conversa natural em português do Brasil, direta e útil.

Sua tarefa é decidir o que fazer com a mensagem do usuário e responder em JSON com um dos intents abaixo:

1. "chat" — quando ele só quer conversar, perguntar algo, opinar.
   Campos: reply (sua resposta natural pra ele).

2. "create_reminder" — quando ele pede pra ser lembrado de algo num momento específico (uma vez só).
   Exemplos: "me lembra daqui 10 min de beber água", "amanhã 8h reunião", "às 18h ligar pra mãe".
   Campos: text (o que lembrar, conciso), datetime_iso (quando, formato YYYY-MM-DDTHH:MM:SS no horário local — calcule a partir do "agora" acima), reply (confirmação amigável e curta mencionando data/hora).

3. "create_recurring_reminder" — quando ele pede um lembrete recorrente para uma ATIVIDADE (não medicamento/suplemento).
   Exemplos: "toda segunda 9h reunião", "todo dia 7h alongar", "toda sexta 18h sair pra correr".
   ATENÇÃO: NÃO use este intent quando for ingerir uma substância (remédio/vitamina/suplemento/creatina/whey/etc.) — nesse caso use "create_medication" (ver seção MEDICAÇÕES).
   Campos: text, cron (no formato crontab de 5 campos: "min hora dia mês dia_semana"), reply.

4. "save_memory" — quando ele te conta um fato sobre ele que vale guardar a longo prazo, ou pede pra você lembrar/anotar algo sobre ele.
   Exemplos: "lembra que eu prefiro café preto", "anota aí: minha esposa se chama Ana", "trabalho na Seaway".
   Campos: text (o fato, claro e conciso), category (uma de: fact, preference, routine, goal, habit), reply (confirmação curta).

5. "list_memories" — quando ele pede pra ver o que você sabe/lembra.
   Exemplos: "o que você sabe sobre mim?", "lista minhas preferências", "minhas rotinas".
   Campos: category (opcional, se ele especificou uma), reply (curta, tipo "Aqui:" — a lista será adicionada automaticamente).

6. "list_reminders" — quando ele pergunta sobre lembretes ativos.
   Exemplos: "quais lembretes tenho?", "minha agenda", "o que tô agendado?".
   Campos: reply (curta — a agenda será adicionada automaticamente).

7. "search_memories" — quando ele pergunta sobre algo específico que pode estar nas memórias salvas.
   Exemplos: "o que sabe sobre o João?", "achou algo sobre café?", "busca aí qualquer coisa de trabalho".
   Campos: query (palavra-chave curta para buscar — extraia o termo principal da pergunta), reply (curta, tipo "Procurando..." — os resultados serão adicionados automaticamente).

8. "cancel_reminder" — quando ele pede pra cancelar um lembrete específico.
   Campos: id (número), reply.

9. "delete_memory" — quando ele pede pra esquecer/apagar uma memória específica.
   Campos: id (número), reply.

9b. "show_status" — quando ele pede um panorama geral / resumo / status agregando lembretes ativos, treino do dia, medicamentos e adesão.
    Exemplos: "status", "como tô", "panorama", "resumo", "me dá um resumo", "qual a situação?".
    Campos: reply (curta, tipo "Aqui:" — o panorama será adicionado automaticamente).

=== TREINOS DE ACADEMIA ===

10. "create_workout" — quando ele descreve/cria um treino de academia (ou manda foto de plano de treino para você cadastrar).
    Exemplos: "cria treino A com supino 4x10, agachamento 4x12 com 60kg", "salva isso como Treino Push: supino reto 4x8, supino inclinado 3x10, tríceps na polia 3x15".
    Se já existir treino com mesmo nome, este intent SUBSTITUI os exercícios (upsert).
    Campos: workout_name (nome curto), workout_description (opcional), exercises (lista de objetos {name, sets, reps, weight, notes}), reply.
    No campo "exercises":
    - "name": nome do exercício (ex: "supino reto", "agachamento livre")
    - "sets": número de séries (int)
    - "reps": string ("10", "8-12", "até falha")
    - "weight": peso em kg (float, opcional — null se não citado)
    - "notes": observações curtas (opcional)

11. "list_workouts" — quando ele pergunta quais treinos tem cadastrados.
    Exemplos: "quais treinos tenho?", "meus treinos", "lista os treinos".
    Campos: reply (curta — a lista será adicionada automaticamente).

12. "show_workout" — quando ele pede detalhes de um treino específico.
    Exemplos: "mostra o treino A", "como é o treino Push?".
    Campos: workout_name, reply (curta).

13. "delete_workout" — quando ele pede pra apagar um treino.
    Campos: workout_name, reply.

14. "set_workout_schedule" — quando ele monta a rotina semanal.
    Exemplos: "segunda treino A, quarta B, sexta C", "domingo é descanso", "tira o treino de quinta".
    Campos: schedule_assignments (lista de {weekday, workout_name}). Use weekday em português ("segunda", "terça", ..., "domingo"). Se for descanso, workout_name = null. Reply.

15. "show_workout_schedule" — quando ele pergunta sobre a rotina/treino do dia ou da semana.
    Exemplos: "qual o treino de hoje?", "minha semana", "treino de quarta".
    Campos: scope ("today" | "week" | "weekday"), weekday (se scope="weekday"), reply (curta).

16. "start_workout_session" — quando ele anuncia que vai começar a treinar.
    Exemplos: "vou treinar A agora", "começando treino", "tô na academia". Se ele não citar qual treino, deixe workout_name vazio (será inferido pelo schedule do dia).
    Campos: workout_name (opcional), reply.

17. "log_set" — quando ele registra uma série feita (use SOMENTE se houver sessão ativa, mas extraia mesmo assim — o handler valida).
    Exemplos: "supino 60 por 10", "agachamento 80 8 reps", "fiz 12 com 40", "supino 60x10".
    Campos: exercise_name (nome do exercício; se omitido, deixe vazio), reps_done (int), weight_used (float kg, null se não citado), notes (opcional), reply.

18. "finish_workout_session" — quando ele encerra o treino.
    Exemplos: "acabei", "terminei o treino", "fim".
    Campos: reply (curta — o resumo será adicionado automaticamente).

19. "cancel_workout_session" — quando ele desiste/cancela uma sessão sem encerrar normalmente.
    Exemplos: "cancela o treino", "esquece, não vou treinar".
    Campos: reply.

20. "show_exercise_history" — quando ele pergunta sobre o histórico/progressão de um exercício.
    Exemplos: "como tá meu supino?", "histórico de agachamento", "evolução do levantamento terra".
    Campos: exercise_name, reply (curta).

21. "start_rest_timer" — quando ele pede um timer de descanso entre séries.
    Exemplos: "descanso de 90s", "timer 2 min", "me avisa daqui 60 segundos".
    Campos: seconds (int, mínimo 10, máximo 600), reply (curta).

21b. "workout_stats" — quando ele pergunta sobre frequência/quantidade de treinos numa janela de tempo.
    Exemplos: "treinei quantas vezes esse mês?", "minha frequência", "quando treinei pela última vez?", "treinei quantos dias na semana?", "quantos treinos fiz nos últimos 60 dias?".
    Campos: days (int, default 30), reply (curta — o relatório será adicionado).
    NÃO use "show_exercise_history" pra isso (esse é por exercício específico, não agregado).

=== MEDICAÇÕES ===

IMPORTANTE: medicamentos têm um sistema próprio (com botões inline e tracking de adesão).

REGRA DE DECISÃO clara para evitar confusão com create_recurring_reminder:
- Se o que ele vai fazer é INGERIR/TOMAR uma SUBSTÂNCIA (medicamento, vitamina, suplemento,
  proteína, creatina, whey, ômega 3, BCAA, glutamina, magnésio, colágeno, anticoncepcional,
  antibiótico, qualquer cápsula/comprimido/pó/injeção/etc.) → SEMPRE use "create_medication".
- Se é uma ATIVIDADE não-ingerida (beber água, fazer alongamento, meditar, sair pra correr)
  → use "create_recurring_reminder".

22. "create_medication" — quando ele quer ser lembrado de tomar um medicamento ou suplemento em horário(s) recorrente(s).
    Exemplos:
    - "todo dia 9h tomar sertralina"
    - "vitamina D às 8h da manhã todo dia"
    - "remédio da tireoide 7h da manhã"
    - "todo dia 23h tomar creatina"
    - "lembra de tomar whey toda manhã 8h"
    - "ômega 3 todo dia no almoço"
    - "BCAA antes do treino, todo dia 17h"
    - "anticoncepcional todo dia 22h"
    Campos: medication_name (nome do medicamento/suplemento, ex: "creatina", "sertralina"), cron (5 campos crontab), reply (confirmação curta).

23. "list_medications" — listar medicamentos cadastrados.
    Exemplos: "meus remédios", "quais medicamentos tomo?", "lista os medicamentos".
    Campos: reply (curta — a lista será adicionada).

24. "delete_medication" — parar de lembrar de um medicamento.
    Exemplos: "para de me lembrar da sertralina", "tira a vitamina D dos remédios", "parei de tomar X".
    Campos: medication_name, reply.

25. "track_medication" — registrar manualmente que tomou (caso ele não tenha clicado o botão na hora).
    Exemplos: "tomei a sertralina", "já tomei a vitamina hoje", "tomei o remédio".
    Campos: medication_name (opcional — se omitido e só houver 1 medicamento ativo, infere; senão peça o nome), reply.

26. "untrack_medication" — desfazer a tomada de hoje.
    Exemplos: "não tomei a sertralina hoje", "tira o registro de hoje", "esquece, não tomei".
    Campos: medication_name (opcional), reply.

27. "medication_compliance" — quantos dias ele tomou em uma janela.
    Exemplos: "quantos dias tomei sertralina nos últimos 15?", "como tá minha adesão?", "tomei quantos dias dos últimos 7?", "tomei quantos dias esse mês?".
    Campos: medication_name (opcional), days (int, default 30), reply (curta — o resumo será adicionado).

=== ATIVIDADES DIÁRIAS ===

Atividades são tarefas que o usuário quer cumprir todo dia (ou em dias específicos),
agrupadas por uma CATEGORIA livre (escolhida pelo usuário, ex: "física", "obrigatória",
"estudo", "trabalho"). Todo dia às 23:30 o bot pergunta o que ele cumpriu via botões.

Diferença vs medications: medications são substâncias com horário específico no dia;
atividades são compromissos com o dia (sem hora). Diferença vs workouts: workouts é
treino estruturado com séries/reps; atividade física genérica é "correr 30min".

28. "create_activity" — quando ele quer cadastrar uma atividade diária.
    Exemplos: "cadastra atividade física: correr todo dia", "adiciona estudo: ler 30min seg/qua/sex", "obrigatória: tomar banho", "atividade física andar 10k passos".
    Campos: activity_name (descrição curta), category (livre, ex: "física", "estudo", "obrigatória"), days_of_week (lista opcional de int 0-6, omita se for todo dia. 0=segunda, 6=domingo), reply.

29. "list_activities" — quando ele pergunta quais atividades tem cadastradas.
    Exemplos: "minhas atividades", "o que eu tenho que fazer?", "lista as atividades", "quais minhas atividades físicas?".
    Campos: category (opcional, se filtrar por uma), reply (curta — a lista será adicionada).

30. "delete_activity" — apagar/parar de rastrear uma atividade.
    Exemplos: "tira a atividade correr", "para de monitorar leitura", "apaga a atividade X".
    Campos: activity_name, reply.

31. "track_activity" — registrar manualmente que fez (ou não fez) uma atividade.
    Exemplos: "fiz minha corrida hoje", "fiz a leitura", "não fiz a corrida hoje", "pulei a meditação".
    Campos: activity_name (opcional — se omitido e só houver 1 atividade ativa, infere; senão peça o nome), status ("done" se fez, "skipped" se não fez), reply.

32. "untrack_activity" — desfazer o registro de hoje.
    Exemplos: "desfaz, errei", "tira o registro de hoje", "esquece, não foi".
    Campos: activity_name (opcional), reply.

33. "activity_compliance" — quantos dias cumpriu uma atividade ou categoria.
    Exemplos: "como tô na adesão das físicas?", "fiz quantos dias de leitura nos últimos 7?", "minhas atividades obrigatórias", "como tá minha rotina física esse mês?".
    Campos: activity_name (opcional, se for 1 atividade), category (opcional, se for por categoria), days (int, default 30), reply (curta — o resumo será adicionado).

=== CONTEXTO DE IMAGEM ===

Se a mensagem incluir uma FOTO:
- Se parecer um plano/ficha de treino (lista de exercícios com séries/reps): use "create_workout" e extraia os exercícios. Se ele citou nome ("salva como A"), use; senão deixe workout_name como "Novo treino" e peça confirmação na reply.
- Se for outro tipo de imagem: use "chat" e descreva brevemente o que viu.

CAMPO EXTRA (em qualquer intent, exceto save_memory): "extracted_facts"
- Lista de fatos novos sobre o usuário que apareceram NESTA mensagem e merecem ser memorizados a longo prazo.
- Cada fato é um objeto com chaves "category" (uma de: fact, preference, routine, goal, habit) e "content" (texto curto e claro).
- Use apenas quando há fato realmente novo e relevante. Não duplique algo já presente nas memórias listadas abaixo.
- Não inclua perguntas, opiniões momentâneas, saudações ou nada efêmero.
- Se a intent for "save_memory", deixe extracted_facts como lista vazia (a memória já será salva pelo campo principal).
- Se nada relevante: extracted_facts deve ser uma lista vazia.

REGRAS:
- Responda APENAS com JSON válido, sem markdown, sem texto fora do JSON.
- Se houver ambiguidade ou faltar info crítica (ex: pediu lembrete mas não disse quando), use intent "chat" e pergunte na reply.
- Para intents de ação, a "reply" deve ser curta, natural, confirmando o que foi feito.
- Use as memórias e histórico abaixo para personalizar. Não invente fatos novos sobre o usuário.
"""


# Limite p/ truncar respostas longas do assistente no histórico (compressão simples).
_HISTORY_REPLY_MAX_CHARS = 120


def _format_history(history: list[dict]) -> str:
    """Formata histórico para o prompt, truncando respostas longas do bot."""
    if not history:
        return "(sem histórico anterior)"
    lines = []
    for h in history:
        if h["role"] == "user":
            lines.append(f"Usuário: {h['content']}")
        else:
            content = h["content"]
            if len(content) > _HISTORY_REPLY_MAX_CHARS:
                content = content[:_HISTORY_REPLY_MAX_CHARS].rstrip() + "…"
            lines.append(f"Assistente: {content}")
    return "\n".join(lines)


def _build_prompt(user_message: str, now: datetime) -> str:
    """Monta o prompt: parte estática (cacheável) + bloco dinâmico no final."""
    summary = get_memory_summary(limit_per_category=15)
    history = get_recent_history(limit=10)
    now_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({now.strftime('%A')})"

    return f"""{_STATIC_SYSTEM_PROMPT}

=== CONTEXTO DINÂMICO ===

Data e hora atuais (timezone America/Sao_Paulo): {now_str}

=== Memórias sobre o usuário ===
{summary}

=== Histórico recente ===
{_format_history(history)}

=== Mensagem atual ===
Usuário: {user_message}

Responda APENAS com o JSON:"""


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw


def _coerce_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_exercises(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for ex in raw:
        if not isinstance(ex, dict):
            continue
        name = (ex.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "sets": _coerce_int(ex.get("sets")),
            "reps": str(ex["reps"]).strip() if ex.get("reps") is not None and str(ex.get("reps")).strip() else None,
            "weight": _coerce_float(ex.get("weight") or ex.get("target_weight")),
            "notes": (ex.get("notes") or "").strip() or None,
        })
    return out


def _coerce(parsed: dict, user_message: str) -> dict:
    intent = (parsed.get("intent") or "chat").strip().lower()
    if intent not in VALID_INTENTS:
        intent = "chat"

    reply = (parsed.get("reply") or "").strip()
    result: dict[str, Any] = {"intent": intent, "reply": reply}

    if intent in {"create_reminder", "create_recurring_reminder", "save_memory"}:
        result["text"] = (parsed.get("text") or "").strip()

    if intent == "create_reminder":
        result["datetime_iso"] = (parsed.get("datetime_iso") or "").strip()

    if intent == "create_recurring_reminder":
        result["cron"] = (parsed.get("cron") or "").strip()

    if intent == "save_memory":
        cat = (parsed.get("category") or "fact").strip().lower()
        result["category"] = cat if cat in VALID_CATEGORIES else "fact"

    if intent == "list_memories":
        cat = (parsed.get("category") or "").strip().lower()
        result["category"] = cat if cat in VALID_CATEGORIES else None

    if intent == "search_memories":
        result["query"] = (parsed.get("query") or "").strip()
        if not result["query"]:
            result["intent"] = "chat"
            result["reply"] = reply or "O que você quer que eu busque?"

    if intent in {"cancel_reminder", "delete_memory"}:
        try:
            result["id"] = int(parsed.get("id"))
        except (TypeError, ValueError):
            result["intent"] = "chat"
            result["reply"] = reply or "Qual o número/ID? Manda /listar ou /agenda pra ver."

    # ---------- Treinos ----------
    if intent == "create_workout":
        result["workout_name"] = (parsed.get("workout_name") or "").strip()
        result["workout_description"] = (parsed.get("workout_description") or "").strip() or None
        result["exercises"] = _coerce_exercises(parsed.get("exercises"))
        if not result["workout_name"] or not result["exercises"]:
            result["intent"] = "chat"
            result["reply"] = (
                reply or "Faltou o nome do treino ou os exercícios. Pode descrever de novo?"
            )

    if intent in {"show_workout", "delete_workout"}:
        result["workout_name"] = (parsed.get("workout_name") or "").strip()
        if not result["workout_name"]:
            result["intent"] = "chat"
            result["reply"] = reply or "Qual treino? Me passa o nome."

    if intent == "set_workout_schedule":
        raw_assign = parsed.get("schedule_assignments") or []
        assignments = []
        if isinstance(raw_assign, list):
            for a in raw_assign:
                if not isinstance(a, dict):
                    continue
                weekday_raw = a.get("weekday")
                workout_name = a.get("workout_name")
                if isinstance(workout_name, str):
                    workout_name = workout_name.strip() or None
                assignments.append({
                    "weekday": weekday_raw,
                    "workout_name": workout_name,
                })
        result["schedule_assignments"] = assignments
        if not assignments:
            result["intent"] = "chat"
            result["reply"] = reply or "Não entendi quais dias e treinos. Pode repetir?"

    if intent == "show_workout_schedule":
        scope = (parsed.get("scope") or "today").strip().lower()
        if scope not in {"today", "week", "weekday"}:
            scope = "today"
        result["scope"] = scope
        result["weekday"] = parsed.get("weekday")

    if intent == "start_workout_session":
        result["workout_name"] = (parsed.get("workout_name") or "").strip() or None

    if intent == "log_set":
        result["exercise_name"] = (parsed.get("exercise_name") or "").strip()
        result["reps_done"] = _coerce_int(parsed.get("reps_done"))
        result["weight_used"] = _coerce_float(parsed.get("weight_used"))
        result["notes"] = (parsed.get("notes") or "").strip() or None

    if intent == "show_exercise_history":
        result["exercise_name"] = (parsed.get("exercise_name") or "").strip()
        if not result["exercise_name"]:
            result["intent"] = "chat"
            result["reply"] = reply or "Histórico de qual exercício?"

    if intent == "start_rest_timer":
        seconds = _coerce_int(parsed.get("seconds"))
        if seconds is None or seconds < 10 or seconds > 600:
            result["intent"] = "chat"
            result["reply"] = reply or "Quantos segundos de descanso? (entre 10 e 600)"
        else:
            result["seconds"] = seconds

    if intent == "workout_stats":
        days = _coerce_int(parsed.get("days"))
        if days is None or days < 1:
            days = 30
        result["days"] = min(days, 365)

    # ---------- Medicações ----------
    if intent == "create_medication":
        result["medication_name"] = (parsed.get("medication_name") or "").strip()
        result["cron"] = (parsed.get("cron") or "").strip()
        if not result["medication_name"] or not result["cron"]:
            result["intent"] = "chat"
            result["reply"] = (
                reply or "Faltou o nome do medicamento ou o horário. Pode repetir?"
            )

    if intent in {"delete_medication", "track_medication", "untrack_medication"}:
        result["medication_name"] = (parsed.get("medication_name") or "").strip() or None

    if intent == "medication_compliance":
        result["medication_name"] = (parsed.get("medication_name") or "").strip() or None
        days = _coerce_int(parsed.get("days"))
        if days is None or days < 1:
            days = 30
        result["days"] = min(days, 365)

    # ---------- Atividades ----------
    if intent == "create_activity":
        result["activity_name"] = (parsed.get("activity_name") or "").strip()
        result["category"] = (parsed.get("category") or "").strip()
        raw_days = parsed.get("days_of_week")
        days_list = []
        if isinstance(raw_days, list):
            for d in raw_days:
                v = _coerce_int(d)
                if v is not None and 0 <= v <= 6:
                    days_list.append(v)
        result["days_of_week"] = sorted(set(days_list)) or None
        if not result["activity_name"] or not result["category"]:
            result["intent"] = "chat"
            result["reply"] = (
                reply or "Faltou o nome da atividade ou a categoria. Pode repetir?"
            )

    if intent in {"delete_activity", "track_activity", "untrack_activity"}:
        result["activity_name"] = (parsed.get("activity_name") or "").strip() or None

    if intent == "track_activity":
        status = (parsed.get("status") or "").strip().lower()
        if status not in {"done", "skipped"}:
            status = "done"  # default
        result["status"] = status

    if intent == "list_activities":
        result["category"] = (parsed.get("category") or "").strip() or None

    if intent == "activity_compliance":
        result["activity_name"] = (parsed.get("activity_name") or "").strip() or None
        result["category"] = (parsed.get("category") or "").strip() or None
        days = _coerce_int(parsed.get("days"))
        if days is None or days < 1:
            days = 30
        result["days"] = min(days, 365)

    # Fatos extraídos vêm na mesma chamada — economiza 1 request por mensagem.
    raw_facts = parsed.get("extracted_facts") or []
    facts: list[dict] = []
    if isinstance(raw_facts, list) and intent != "save_memory":
        for f in raw_facts:
            if not isinstance(f, dict):
                continue
            content = (f.get("content") or "").strip()
            if not content:
                continue
            cat = (f.get("category") or "fact").strip().lower()
            if cat not in VALID_CATEGORIES:
                cat = "fact"
            facts.append({"category": cat, "content": content})
    result["extracted_facts"] = facts

    return result


def _fallback(user_message: str, error: str) -> dict:
    log.warning("Fallback de intent (%s) para mensagem: %r", error, user_message[:80])
    return {
        "intent": "chat",
        "reply": "Tive um problema interpretando, pode reformular?",
        "extracted_facts": [],
    }


_MAX_RETRIES_TRANSIENT = 2  # tentativas extras em erros transientes (timeout, 5xx)
_RETRY_BACKOFF_BASE_S = 1.0

# Cache de classificação: mensagem repetida em ≤30s reutiliza o JSON parseado.
# Útil em retries pós-429 e digitações duplicadas. Valores são cópias do dict
# original sem campos auxiliares (`_reminder_id`, `_reminder_ids`) que vêm do
# dispatcher e nunca devem ser cachados.
_CLASSIFY_CACHE: dict[str, tuple[float, dict]] = {}
_CLASSIFY_CACHE_TTL_S = 30.0
_CLASSIFY_CACHE_MAX_ENTRIES = 50


def _cache_get(key: str) -> dict | None:
    entry = _CLASSIFY_CACHE.get(key)
    if entry is None:
        return None
    cached_at, data = entry
    if time.monotonic() - cached_at > _CLASSIFY_CACHE_TTL_S:
        del _CLASSIFY_CACHE[key]
        return None
    # Cópia rasa pra evitar que o caller mute o valor cacheado
    cloned = dict(data)
    # `extracted_facts` é lista — copiar pra evitar mutação
    cloned["extracted_facts"] = list(data.get("extracted_facts") or [])
    return cloned


def _cache_set(key: str, value: dict) -> None:
    if len(_CLASSIFY_CACHE) >= _CLASSIFY_CACHE_MAX_ENTRIES:
        # FIFO simples: descarta a entrada mais antiga
        oldest_key = min(_CLASSIFY_CACHE, key=lambda k: _CLASSIFY_CACHE[k][0])
        _CLASSIFY_CACHE.pop(oldest_key, None)
    # Não armazena campos auxiliares de dispatch
    cleaned = {k: v for k, v in value.items() if not k.startswith("_")}
    _CLASSIFY_CACHE[key] = (time.monotonic(), cleaned)


async def _call_gemini_with_retry(model, content) -> str:
    """Chama Gemini com retry para erros transientes. Levanta exceção se exausto."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES_TRANSIENT + 1):
        try:
            response = await model.generate_content_async(content)
            return response.text or ""
        except ResourceExhausted:
            # 429 não é retried automaticamente — caller decide com base no retry_delay
            raise
        except (ServiceUnavailable, RetryError, GoogleAPICallError) as e:
            last_exc = e
            if attempt < _MAX_RETRIES_TRANSIENT:
                wait = _RETRY_BACKOFF_BASE_S * (2 ** attempt)
                log.warning(
                    "Gemini transient error (tentativa %d/%d), retry em %.1fs: %s",
                    attempt + 1, _MAX_RETRIES_TRANSIENT + 1, wait, type(e).__name__,
                )
                await asyncio.sleep(wait)
            else:
                raise
    if last_exc:
        raise last_exc
    return ""


async def classify_and_respond(
    user_message: str,
    image_path: str | None = None,
    image_mime: str = "image/jpeg",
) -> dict:
    """Chama o Gemini uma única vez: classifica intent e gera reply.

    Se `image_path` for passado, inclui a imagem no contexto da chamada
    (o prompt já instrui como interpretar fotos de plano de treino).
    """
    started = time.monotonic()
    _METRICS["total_calls"] += 1

    # Cache só pra mensagens textuais (imagens não são cacheadas — o file_path é tmp)
    cache_key = None
    if not image_path and user_message:
        cache_key = user_message.strip().lower()
        cached = _cache_get(cache_key)
        if cached is not None:
            log.info("intent=%s via=cache", cached.get("intent"))
            return cached

    now = datetime.now()
    prompt = _build_prompt(user_message or "(usuário enviou apenas uma imagem)", now)

    model = genai.GenerativeModel(
        GEMINI_MODEL,
        generation_config={"response_mime_type": "application/json"},
    )

    uploaded = None
    if image_path:
        uploaded = await asyncio.to_thread(
            genai.upload_file, image_path, mime_type=image_mime
        )

    try:
        api_content = [prompt, uploaded] if uploaded else prompt
        try:
            raw = await _call_gemini_with_retry(model, api_content)
        except ResourceExhausted as e:
            _METRICS["total_429"] += 1
            _METRICS["total_errors"] += 1
            retry = _parse_retry_delay(e)
            retry_msg = (
                f" Tenta de novo em ~{retry}s." if retry else " Tenta de novo daqui a pouco."
            )
            log.warning("Gemini quota exceeded (retry=%ss)", retry)
            return {
                "intent": "chat",
                "reply": f"Atingi o limite de uso da API do Gemini agora.{retry_msg}",
                "extracted_facts": [],
            }
        except Exception:
            _METRICS["total_errors"] += 1
            log.exception("Erro ao chamar Gemini para classificação")
            return _fallback(user_message, "api_error")

        raw = _strip_fences(raw)
        if not raw:
            _METRICS["total_errors"] += 1
            return _fallback(user_message, "empty_response")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            _METRICS["total_errors"] += 1
            log.warning("JSON inválido do modelo: %r", raw[:300])
            return _fallback(user_message, f"json_error: {e}")

        if not isinstance(parsed, dict):
            _METRICS["total_errors"] += 1
            return _fallback(user_message, "not_a_dict")

        result = _coerce(parsed, user_message)
        _METRICS["intent_counts"][result["intent"]] += 1
        duration_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "intent=%s duration_ms=%d image=%s msg_len=%d",
            result["intent"], duration_ms, bool(image_path), len(user_message or ""),
        )
        if cache_key is not None:
            _cache_set(cache_key, result)
        return result
    finally:
        if uploaded is not None:
            try:
                await asyncio.to_thread(genai.delete_file, uploaded.name)
            except Exception:
                log.warning("Falha ao remover upload %s", uploaded.name)


_TRANSCRIPTION_PROMPT = (
    "Transcreva o áudio em português brasileiro. "
    "Responda APENAS com o texto transcrito, sem comentários, sem prefixos."
)


async def transcribe_voice(file_path: str, mime_type: str = "audio/ogg") -> str:
    """Transcreve um arquivo de áudio usando Gemini.

    Levanta ResourceExhausted, RuntimeError ou outras exceções em falha.
    """
    uploaded = await asyncio.to_thread(
        genai.upload_file, file_path, mime_type=mime_type
    )
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = await model.generate_content_async([_TRANSCRIPTION_PROMPT, uploaded])
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("transcrição vazia")
        return text
    finally:
        try:
            await asyncio.to_thread(genai.delete_file, uploaded.name)
        except Exception:
            log.warning("Falha ao remover arquivo de upload %s", uploaded.name)
