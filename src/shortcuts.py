"""Pre-classifier por keyword: intercepta mensagens comuns ANTES de chamar o Gemini.

Cada match retorna um `intent_data` pronto pra entrar no `_dispatch`. Se nenhum
shortcut bater, devolve `None` e o pipeline cai pro Gemini normalmente.

Filosofia: cobrir queries de leitura/listagem (zero risco) e ações triviais
(encerrar treino, status). Tudo que envolve nome ou parâmetros novos vai pro
Gemini — extração natural é mais robusta lá.
"""

import unicodedata
from typing import Optional

from . import activities as activities_service
from . import medications as medications_service
from . import workouts as workouts_service


def _normalize(text: str) -> str:
    """Lowercase + remove acentos + tira pontuação trailing."""
    text = text.strip().lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    return text.rstrip("!?.,;:")


# Mapeamento estático: chave normalizada (sem acentos) → intent_data parcial.
# A normalização é feita em runtime, então registre aqui sempre na forma SEM acento.
_STATIC_SHORTCUTS: dict[str, dict] = {
    # ===== Listas =====
    "meus treinos": {"intent": "list_workouts"},
    "lista treinos": {"intent": "list_workouts"},
    "treinos": {"intent": "list_workouts"},

    "meus remedios": {"intent": "list_medications"},
    "lista remedios": {"intent": "list_medications"},
    "remedios": {"intent": "list_medications"},
    "medicamentos": {"intent": "list_medications"},

    "minhas atividades": {"intent": "list_activities"},
    "lista atividades": {"intent": "list_activities"},
    "atividades": {"intent": "list_activities"},

    "minhas memorias": {"intent": "list_memories"},
    "lista memorias": {"intent": "list_memories"},
    "memorias": {"intent": "list_memories"},

    "agenda": {"intent": "list_reminders"},
    "meus lembretes": {"intent": "list_reminders"},
    "lista lembretes": {"intent": "list_reminders"},
    "lembretes": {"intent": "list_reminders"},
    "quais lembretes tenho": {"intent": "list_reminders"},

    # ===== Status =====
    "status": {"intent": "show_status"},
    "como to": {"intent": "show_status"},
    "panorama": {"intent": "show_status"},
    "resumo": {"intent": "show_status"},
    "me da um resumo": {"intent": "show_status"},

    # ===== Treino do dia =====
    "treino de hoje": {"intent": "show_workout_schedule", "scope": "today"},
    "treino hoje": {"intent": "show_workout_schedule", "scope": "today"},
    "qual o treino de hoje": {"intent": "show_workout_schedule", "scope": "today"},
    "qual treino de hoje": {"intent": "show_workout_schedule", "scope": "today"},

    # ===== Encerrar treino (intent valida sessão ativa) =====
    "acabei": {"intent": "finish_workout_session"},
    "acabei o treino": {"intent": "finish_workout_session"},
    "terminei": {"intent": "finish_workout_session"},
    "terminei o treino": {"intent": "finish_workout_session"},
    "fim do treino": {"intent": "finish_workout_session"},

    # ===== Cancelar sessão =====
    "cancela o treino": {"intent": "cancel_workout_session"},
    "cancela treino": {"intent": "cancel_workout_session"},
}

# Frases que tentam track sem nome — só viram shortcut se houver exatamente 1 ativo.
_SOLO_MED_PHRASES = {
    "tomei",
    "tomei o remedio",
    "tomei remedio",
    "ja tomei",
}

_SOLO_ACTIVITY_PHRASES = {
    "fiz",
    "ja fiz",
    "fiz a atividade",
}


def _wrap(intent_partial: dict) -> dict:
    """Adiciona campos esperados pelo dispatcher."""
    return {
        **intent_partial,
        "reply": "",
        "extracted_facts": [],
    }


def match(text: str) -> Optional[dict]:
    """Tenta resolver `text` para um intent_data sem chamar Gemini.

    Retorna `None` quando nenhum shortcut bate — caller deve cair no fluxo normal.
    """
    if not text:
        return None
    normalized = _normalize(text)

    # Match estático
    if normalized in _STATIC_SHORTCUTS:
        return _wrap(_STATIC_SHORTCUTS[normalized])

    # Solo medication tracking
    if normalized in _SOLO_MED_PHRASES:
        med = medications_service.find_solo_active()
        if med is not None:
            return _wrap({
                "intent": "track_medication",
                "medication_name": med["name"],
            })

    # Solo activity tracking
    if normalized in _SOLO_ACTIVITY_PHRASES:
        activity = activities_service.find_solo_active()
        if activity is not None:
            return _wrap({
                "intent": "track_activity",
                "activity_name": activity["name"],
                "status": "done",
            })

    return None
