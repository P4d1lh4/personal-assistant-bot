import json
import logging
import re
from typing import Optional

import google.generativeai as genai

from .config import GEMINI_API_KEY, GEMINI_MODEL
from .memory import (
    append_message,
    get_memory_summary,
    get_recent_history,
)

genai.configure(api_key=GEMINI_API_KEY)
log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Você é o assistente pessoal do Guilherme. Seu papel é:
- Conversar de forma natural, direta e útil em português do Brasil.
- Usar as memórias armazenadas sobre o Guilherme para personalizar respostas.
- Lembrar e considerar a rotina, preferências, hábitos e objetivos dele.
- Ser objetivo: respostas curtas para perguntas simples; mais detalhadas só quando necessário.
- Nunca inventar fatos sobre o Guilherme — se não souber, pergunte.
- Tratar o Guilherme com familiaridade, sem formalidade excessiva.
"""

EXTRACTION_PROMPT = """Você analisa mensagens do Guilherme para extrair fatos pessoais relevantes que devem ser memorizados a longo prazo.

Categorias possíveis:
- "fact": informação factual sobre ele (nome, idade, profissão, família, local)
- "preference": gostos e preferências
- "routine": atividades recorrentes com horário/frequência
- "goal": objetivos e metas
- "habit": hábitos comportamentais

Retorne APENAS um JSON válido (sem markdown, sem texto extra) no formato:
{"facts": [{"category": "...", "content": "..."}]}

Se a mensagem não contém nenhum fato relevante para memorizar a longo prazo, retorne:
{"facts": []}

Não memorize: perguntas, opiniões momentâneas, saudações, comentários sobre o clima do dia, ou qualquer coisa efêmera.

Mensagem do Guilherme:
"""


def _build_prompt(user_message: str) -> str:
    summary = get_memory_summary()
    history = get_recent_history(limit=20)

    history_text = "\n".join(
        f"{'Guilherme' if h['role'] == 'user' else 'Assistente'}: {h['content']}"
        for h in history
    ) or "(sem histórico anterior)"

    return f"""{SYSTEM_PROMPT}

=== Memórias sobre o Guilherme ===
{summary}

=== Histórico recente da conversa ===
{history_text}

=== Mensagem atual ===
Guilherme: {user_message}

Responda como Assistente:"""


async def chat(user_message: str) -> str:
    append_message("user", user_message)

    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = _build_prompt(user_message)

    try:
        response = await model.generate_content_async(prompt)
        text = (response.text or "").strip()
    except Exception as e:
        log.exception("Erro ao chamar Gemini")
        text = f"Tive um problema ao gerar a resposta: {e}"

    if not text:
        text = "(resposta vazia do modelo)"

    append_message("assistant", text)
    return text


def _parse_extraction_response(raw: str) -> list[dict]:
    raw = raw.strip()
    # Strip code fences if the model used them
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Resposta de extração não é JSON válido: %r", raw[:200])
        return []

    facts = data.get("facts", [])
    if not isinstance(facts, list):
        return []

    cleaned = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        cat = (f.get("category") or "").strip().lower()
        content = (f.get("content") or "").strip()
        if not content:
            continue
        cleaned.append({"category": cat or "fact", "content": content})
    return cleaned


async def extract_facts(user_message: str) -> list[dict]:
    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = EXTRACTION_PROMPT + user_message

    try:
        response = await model.generate_content_async(prompt)
        return _parse_extraction_response(response.text or "")
    except Exception:
        log.exception("Erro ao extrair fatos via Gemini")
        return []
