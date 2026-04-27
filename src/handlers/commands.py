from telegram import Update
from telegram.ext import ContextTypes

from ..memory import (
    VALID_CATEGORIES,
    add_memory,
    delete_memory,
    list_memories,
)
from .auth import owner_only

WELCOME = """Olá, Guilherme! Sou seu assistente pessoal.

Posso conversar com você usando IA (Gemini) e lembrar das suas informações ao longo do tempo.

Comandos disponíveis:
/lembrar <texto> — salvar uma memória
/rotina <texto> — salvar uma rotina
/listar [categoria] — ver memórias
/esquecer <id> — apagar memória
/lembrete <quando> <texto> — agendar lembrete
/agenda — ver lembretes
/cancelar <id> — cancelar lembrete
/help — esta mensagem

Mande qualquer mensagem normal e eu respondo usando IA, lembrando do que você já me contou.
"""


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME)


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME)


def _parse_remember_args(text: str) -> tuple[str, str]:
    """Tenta inferir categoria pela primeira palavra; cai pra 'fact'."""
    text = text.strip()
    if not text:
        return "fact", ""
    first, _, rest = text.partition(" ")
    first_lower = first.lower().rstrip(":")
    if first_lower in VALID_CATEGORIES and rest.strip():
        return first_lower, rest.strip()
    return "fact", text


@owner_only
async def cmd_lembrar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    args_text = " ".join(context.args or []).strip()
    if not args_text:
        await msg.reply_text("Uso: /lembrar <texto>\nEx: /lembrar preference Adoro café preto")
        return

    category, content = _parse_remember_args(args_text)
    mid = add_memory(category, content, source="manual")
    await msg.reply_text(f"Memorizado (id={mid}, categoria={category}).")


@owner_only
async def cmd_rotina(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    content = " ".join(context.args or []).strip()
    if not content:
        await msg.reply_text("Uso: /rotina <texto>\nEx: /rotina Acordo às 7h e tomo café")
        return
    mid = add_memory("routine", content, source="manual")
    await msg.reply_text(f"Rotina salva (id={mid}).")


@owner_only
async def cmd_listar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    args = context.args or []
    category = args[0].lower() if args else None
    if category and category not in VALID_CATEGORIES:
        await msg.reply_text(
            f"Categoria inválida. Use uma de: {', '.join(sorted(VALID_CATEGORIES))}"
        )
        return

    rows = list_memories(category=category, limit=50)
    if not rows:
        await msg.reply_text("Nenhuma memória registrada ainda.")
        return

    lines = [
        f"#{r['id']} [{r['category']}] {r['content']}  ({r['source']})"
        for r in rows
    ]
    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500] + "\n... (truncado)"
    await msg.reply_text(text)


@owner_only
async def cmd_esquecer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text("Uso: /esquecer <id>")
        return
    try:
        mid = int(args[0])
    except ValueError:
        await msg.reply_text("ID deve ser um número.")
        return
    ok = delete_memory(mid)
    await msg.reply_text(f"Memória {mid} {'removida' if ok else 'não encontrada'}.")
