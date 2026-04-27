# Telegram Assistant Bot

Bot pessoal do Telegram que armazena suas informações e rotinas, e responde usando a API do Gemini.

## Setup

### 1. Pré-requisitos

- Python 3.10+
- Token do bot Telegram (criar via [@BotFather](https://t.me/BotFather))
- API key do Gemini (criar em [aistudio.google.com/apikey](https://aistudio.google.com/apikey))
- Seu chat_id no Telegram (consultar via [@userinfobot](https://t.me/userinfobot))

### 2. Instalação

```bash
cd telegram-assistant-bot
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 3. Configuração

Copie `.env.example` para `.env` e preencha:

```
TELEGRAM_TOKEN=123456:ABC...
GEMINI_API_KEY=AIza...
OWNER_CHAT_ID=123456789
GEMINI_MODEL=gemini-2.0-flash
```

### 4. Execução

```bash
python -m src.main
```

## Comandos

| Comando | Descrição |
|---|---|
| `/start` | Mensagem de boas-vindas |
| `/help` | Lista comandos |
| `/lembrar <texto>` | Salva uma memória manual |
| `/rotina <texto>` | Salva uma rotina |
| `/listar [categoria]` | Lista memórias salvas |
| `/esquecer <id>` | Remove uma memória |
| `/lembrete <quando> <texto>` | Cria lembrete (ex: `/lembrete 2026-04-28 09:00 Tomar remédio`) |
| `/agenda` | Lista lembretes ativos |
| `/cancelar <id>` | Cancela um lembrete |

Mensagens normais (sem comando) são enviadas ao Gemini. O bot extrai automaticamente fatos relevantes em background.
