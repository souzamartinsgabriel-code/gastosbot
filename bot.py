import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet("Gastos")
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Gastos", rows=1000, cols=6)
        ws.append_row(["Data", "Descrição", "Categoria", "Valor", "Mês", "Ano"])
    return ws

def salvar_gasto(data: str, descricao: str, categoria: str, valor: float):
    ws = get_sheet()
    dt = datetime.strptime(data, "%Y-%m-%d")
    ws.append_row([data, descricao, categoria, valor, dt.strftime("%B %Y"), dt.year])

def buscar_gastos_mes(mes: int, ano: int) -> list:
    ws = get_sheet()
    registros = ws.get_all_records()
    label = datetime(ano, mes, 1).strftime("%B %Y")
    return [r for r in registros if r.get("Mês") == label]

def buscar_todos_gastos() -> list:
    ws = get_sheet()
    return ws.get_all_records()

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def interpretar_gasto(mensagem: str) -> dict | None:
    hoje = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""Você é um assistente de controle de gastos pessoais.
O usuário enviou a mensagem: "{mensagem}"
A data de hoje é {hoje}.
Se a mensagem descreve um gasto, extraia as informações e responda APENAS com JSON válido no formato:
{{"descricao": "...", "categoria": "...", "valor": 0.00, "data": "YYYY-MM-DD"}}
Categorias possíveis: Alimentação, Transporte, Lazer, Saúde, Educação, Moradia, Roupas, Supermercado, Assinaturas, Outros
Regras:
- Se não mencionar data, use hoje ({hoje})
- O valor deve ser número float (sem R$)
- Se a mensagem NÃO for um gasto, responda apenas: null"""
    response = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if text == "null":
        return None
    return json.loads(text)

def gerar_analise(gastos: list, periodo: str) -> str:
    if not gastos:
        return f"Nenhum gasto registrado em {periodo}."
    total = sum(float(g["Valor"]) for g in gastos)
    gastos_str = "\n".join([f"- {g['Data']} | {g['Descrição']} | {g['Categoria']} | R$ {g['Valor']}" for g in gastos])
    prompt = f"""Analise os gastos de {periodo} e gere um relatório em português.\n\nGASTOS:\n{gastos_str}\n\nTOTAL: R$ {total:.2f}\n\nInclua: resumo por categoria, top 3 maiores gastos, insights e 2-3 sugestões práticas. Use emojis."""
    response = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID != 0 and update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Acesso não autorizado.")
        return
    texto = update.message.text.strip()
    texto_lower = texto.lower()
    if any(p in texto_lower for p in ["resumo", "análise", "analise", "relatório", "relatorio", "quanto gastei"]):
        agora = datetime.now()
        gastos = buscar_gastos_mes(agora.month, agora.year)
        periodo = agora.strftime("%B %Y")
        await update.message.reply_text(f"📊 Gerando análise de {periodo}...")
        analise = gerar_analise(gastos, periodo)
        await update.message.reply_text(analise)
        return
    if any(p in texto_lower for p in ["total geral", "todos os gastos", "histórico"]):
        gastos = buscar_todos_gastos()
        await update.message.reply_text("📊 Gerando análise completa...")
        analise = gerar_analise(gastos, "todo o período")
        await update.message.reply_text(analise)
        return
    await update.message.reply_text("⏳ Processando...")
    try:
        gasto = interpretar_gasto(texto)
    except Exception as e:
        logger.error(f"Erro ao interpretar: {e}")
        await update.message.reply_text("❌ Erro ao processar. Tente novamente.")
        return
    if gasto is None:
        await update.message.reply_text("🤔 Não entendi como um gasto.")
        return
    try:
        salvar_gasto(gasto["data"], gasto["descricao"], gasto["categoria"], gasto["valor"])
        emoji_cat = {
            "Alimentação": "🍽️",
            "Transporte": "🚗",
            "Lazer": "🎉",
            "Saúde": "💊",
            "Educação": "📚",
            "Moradia": "🏠",
            "Roupas": "👕",
            "Supermercado": "🛒",
            "Assinaturas": "📱",
            "Outros": "📦"
        }.get(gasto["categoria"], "💸")
        await update.message.reply_text(
            f"{emoji_cat} *{gasto['categoria']}* registrado!\n"
            f"📝 {gasto['descricao']}\n"
            f"💰 R$ {float(gasto['valor']):.2f}\n"
            f"📅 {gasto['data']}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Erro ao salvar no Google Sheets: {e}", exc_info=True)
        await update.message.reply_text("❌ Erro ao salvar. Verifique as configurações do Google Sheets.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID != 0 and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "👋 Olá! Sou seu assistente de gastos.\n\n"
        "📌 *Como usar:*\n"
        "• Mande qualquer gasto: \"Almoço 35\", \"Uber 22,50\"\n"
        "• Peça \"resumo do mês\" para análise\n"
        "• Peça \"histórico\" para ver tudo\n\n"
        "Pode mandar em linguagem natural! 🤙",
        parse_mode="Markdown"
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot rodando...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
