import os
import json
import base64
import re
import tempfile
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

import gspread
from google.oauth2.service_account import Credentials

import whisper

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Gastos").strip()

# Coloque o JSON da service account em Base64 na env GOOGLE_SA_JSON_B64
GOOGLE_SA_JSON_B64 = os.getenv("GOOGLE_SA_JSON_B64", "").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Faltou TELEGRAM_BOT_TOKEN nas variáveis de ambiente.")
if not SHEET_ID:
    raise RuntimeError("Faltou SHEET_ID nas variáveis de ambiente.")
if not GOOGLE_SA_JSON_B64:
    raise RuntimeError("Faltou GOOGLE_SA_JSON_B64 nas variáveis de ambiente.")

# --- Google Sheets client ---
sa_info = json.loads(base64.b64decode(GOOGLE_SA_JSON_B64).decode("utf-8"))
scopes = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
gc = gspread.authorize(creds)
ws = gc.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)

# --- Whisper model (leve) ---
# tiny = mais rápido. Se quiser mais preciso: "base" (mais pesado)
model = whisper.load_model("tiny")

def telegram_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

def parse_expense(text: str):
    """
    Espera algo como:
      "500 reais padaria nubank"
      "32,90 mercado inter"
      "R$ 18 uber pix"
    Retorna (valor_float, descricao, conta)
    """
    t = text.lower().strip()
    t = t.replace("r$", "").replace("reais", "").strip()

    # pega primeiro número (aceita 500, 500.50, 500,50)
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", t)
    if not m:
        return None

    raw_val = m.group(1).replace(".", "").replace(",", ".")  # 1.234,56 -> 1234.56 (simplificado)
    try:
        value = float(raw_val)
    except:
        return None

    # remove o valor do texto
    rest = (t[:m.start()] + t[m.end():]).strip()
    # remove palavras vazias comuns
    rest = re.sub(r"\s+", " ", rest).strip()

    # regra simples: última palavra = conta (nubank/inter/pix etc)
    parts = rest.split()
    if len(parts) >= 2:
        conta = parts[-1]
        descricao = " ".join(parts[:-1])
    elif len(parts) == 1:
        conta = ""
        descricao = parts[0]
    else:
        conta = ""
        descricao = ""

    return value, descricao, conta

def transcribe_ogg_to_text(ogg_path: str) -> str:
    # whisper consegue lidar com ogg se ffmpeg estiver instalado
    result = model.transcribe(ogg_path, language="pt")
    return (result.get("text") or "").strip()

@app.get("/")
def home():
    return "ok", 200

@app.post("/webhook")
def webhook():
    update = request.get_json(force=True, silent=True) or {}

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat_id = message.get("chat", {}).get("id")
    msg_id = message.get("message_id")

    # aceita voice (áudio curtinho) e audio (arquivo)
    file_id = None
    if "voice" in message:
        file_id = message["voice"].get("file_id")
    elif "audio" in message:
        file_id = message["audio"].get("file_id")

    if not file_id:
        # se for texto, também dá pra aceitar
        text = (message.get("text") or "").strip()
        if text:
            parsed = parse_expense(text)
            if not parsed:
                send_message(chat_id, "Não entendi. Exemplo: `500 padaria nubank`", reply_to=msg_id)
                return jsonify({"ok": True})
            value, desc, conta = parsed
            add_row(value, desc, conta, text)
            send_message(chat_id, f"Lançado ✅ R$ {value:.2f} | {desc} | {conta}".replace(".", ","), reply_to=msg_id)
        return jsonify({"ok": True})

    # 1) pega caminho do arquivo
    file_info = requests.get(telegram_api("getFile"), params={"file_id": file_id}, timeout=30).json()
    if not file_info.get("ok"):
        send_message(chat_id, "Não consegui pegar o áudio 😕", reply_to=msg_id)
        return jsonify({"ok": True})

    file_path = file_info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"

    # 2) baixa o .ogg temporário
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as tmp:
        r = requests.get(file_url, timeout=60)
        r.raise_for_status()
        tmp.write(r.content)
        tmp.flush()

        # 3) transcreve
        text = transcribe_ogg_to_text(tmp.name)

    if not text:
        send_message(chat_id, "Transcrevi vazio 😅 Tenta falar um pouco mais alto e sem muito ruído.", reply_to=msg_id)
        return jsonify({"ok": True})

    parsed = parse_expense(text)
    if not parsed:
        send_message(chat_id, f"Transcrição: `{text}`\nNão entendi o formato. Exemplo: `500 padaria nubank`", reply_to=msg_id)
        return jsonify({"ok": True})

    value, desc, conta = parsed
    add_row(value, desc, conta, text)
    send_message(chat_id, f"Transcrição: `{text}`\nLançado ✅ R$ {value:.2f} | {desc} | {conta}".replace(".", ","), reply_to=msg_id)
    return jsonify({"ok": True})

def add_row(value: float, desc: str, conta: str, original: str):
    # Horário do Brasil (UTC-3) “na unha” pra não depender de libs
    # Se quiser, depois a gente coloca timezone certinho
    now = datetime.now(timezone.utc)
    datahora = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    ws.append_row([datahora, f"{value:.2f}", desc, conta, original], value_input_option="USER_ENTERED")

def send_message(chat_id, text, reply_to=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    requests.post(telegram_api("sendMessage"), json=payload, timeout=30)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
