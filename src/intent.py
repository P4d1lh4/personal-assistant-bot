import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from .config import GEMINI_API_KEY, GEMINI_MODEL
from .memory import get_memory_summary, get_recent_history

genai.configure(api_key=GEMINI_API_KEY)
log = logging.getLogger(__name__)

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


_SYSTEM_PROMPT_TEMPLATE = """Você é o assistente pessoal do Guilherme. Conversa natural em português do Brasil, direta e útil.

Data e hora atuais (timezone America/Sao_Paulo): __NOW__

Sua tarefa é decidir o que fazer com a mensagem do Guilherme e responder em JSON com um dos intents abaixo:

1. "chat" — quando ele só quer conversar, perguntar algo, opinar.
   Campos: reply (sua resposta natural pra ele).

2. "create_reminder" — quando ele pede pra ser lembrado de algo num momento específico (uma vez só).
   Exemplos: "me lembra daqui 10 min de beber água", "amanhã 8h reunião", "às 18h ligar pra mãe".
   Campos: text (o que lembrar, conciso), datetime_iso (quando, formato YYYY-MM-DDTHH:MM:SS no horário local — calcule a partir do "agora" acima), reply (confirmação amigável e curta mencionando data/hora).

3. "create_recurring_reminder" — quando ele pede um lembrete recorrente.
   Exemplos: "todo dia às 8h tomar vitamina", "toda segunda 9h reunião".
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

CAMPO EXTRA (em qualquer intent, exceto save_memory): "extracted_facts"
- Lista de fatos novos sobre o Guilherme que apareceram NESTA mensagem e merecem ser memorizados a longo prazo.
- Cada fato é um objeto com chaves "category" (uma de: fact, preference, routine, goal, habit) e "content" (texto curto e claro).
- Use apenas quando há fato realmente novo e relevante. Não duplique algo já presente nas memórias listadas abaixo.
- Não inclua perguntas, opiniões momentâneas, saudações ou nada efêmero.
- Se a intent for "save_memory", deixe extracted_facts como lista vazia (a memória já será salva pelo campo principal).
- Se nada relevante: extracted_facts deve ser uma lista vazia.

REGRAS:
- Responda APENAS com JSON válido, sem markdown, sem texto fora do JSON.
- Se houver ambiguidade ou faltar info crítica (ex: pediu lembrete mas não disse quando), use intent "chat" e pergunte na reply.
- Para intents de ação, a "reply" deve ser curta, natural, confirmando o que foi feito.
- Use as memórias e histórico abaixo para personalizar. Não invente fatos novos sobre o Guilherme.
"""


def _system_prompt(now: datetime) -> str:
    now_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({now.strftime('%A')})"
    return _SYSTEM_PROMPT_TEMPLATE.replace("__NOW__", now_str)


def _build_prompt(user_message: str, now: datetime) -> str:
    summary = get_memory_summary(limit_per_category=15)
    history = get_recent_history(limit=10)
    history_text = "\n".join(
        f"{'Guilherme' if h['role'] == 'user' else 'Assistente'}: {h['content']}"
        for h in history
    ) or "(sem histórico anterior)"

    return f"""{_system_prompt(now)}

=== Memórias sobre o Guilherme ===
{summary}

=== Histórico recente ===
{history_text}

=== Mensagem atual ===
Guilherme: {user_message}

Responda APENAS com o JSON:"""


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw


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


async def classify_and_respond(user_message: str) -> dict:
    """Chama o Gemini uma única vez: classifica intent e gera reply.

    Retorna dict com chaves: intent, reply, e campos específicos da intent
    (text, datetime_iso, cron, category, id).
    """
    now = datetime.now()
    prompt = _build_prompt(user_message, now)

    model = genai.GenerativeModel(
        GEMINI_MODEL,
        generation_config={"response_mime_type": "application/json"},
    )

    try:
        response = await model.generate_content_async(prompt)
        raw = response.text or ""
    except ResourceExhausted as e:
        retry = _parse_retry_delay(e)
        retry_msg = f" Tenta de novo em ~{retry}s." if retry else " Tenta de novo daqui a pouco."
        log.warning("Gemini quota exceeded (retry=%ss)", retry)
        return {
            "intent": "chat",
            "reply": f"Atingi o limite de uso da API do Gemini agora.{retry_msg}",
            "extracted_facts": [],
        }
    except Exception as e:
        log.exception("Erro ao chamar Gemini para classificação")
        return _fallback(user_message, f"api_error: {e}")

    raw = _strip_fences(raw)
    if not raw:
        return _fallback(user_message, "empty_response")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("JSON inválido do modelo: %r", raw[:300])
        return _fallback(user_message, f"json_error: {e}")

    if not isinstance(parsed, dict):
        return _fallback(user_message, "not_a_dict")

    return _coerce(parsed, user_message)


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
