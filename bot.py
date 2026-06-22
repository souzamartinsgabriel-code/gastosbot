import os
import json
import logging
import re
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
import gspread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

CATEGORIAS = [
    "Moradia", "Supermercado", "Alimentação", "Transporte", "Saúde",
    "Educação", "Lazer", "Viagem", "Roupas", "Assinaturas",
    "Investimentos", "Impostos e Taxas", "Outros"
]

CARTOES = ["XP", "C6", "Ifood", "Inter", "Nubank"]

EMOJI_CAT = {
    "Moradia": "🏠", "Supermercado": "🛒", "Alimentação": "🍽️",
    "Transporte": "🚗", "Saúde": "💊", "Educação": "📚",
    "Lazer": "🎉", "Viagem": "✈️", "Roupas": "👕",
    "Assinaturas": "📱", "Investimentos": "💰", "Impostos e Taxas": "📋",
    "Outros": "📦"
}

MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12
}

MENU_TEXT = """📋 *Comandos disponíveis*

*Registrar gasto:*
  • "Almoço XP 35"
  • "Uber C6 22,50 ontem"
  • "Mercado Nubank 150"

*Relatórios:*
  • `semana` — semana atual vs anterior
  • `semana passada` — semana anterior vs retrasada
  • `resumo` — mês atual vs mês anterior
  • `junho 2025` — qualquer mês vs mês anterior
  • `cartões` — gastos por cartão no mês atual
  • `histórico` — total geral por categoria

*Ajuda:*
  • `menu` ou /menu — exibe esta lista"""


def get_spreadsheet_id():
    value = SPREADSHEET_ID.strip().strip('"').strip("'")
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', value)
    return match.group(1) if match else value


def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    client = gspread.service_account_from_dict(creds_dict)
    sheet = client.open_by_key(get_spreadsheet_id())
    try:
        ws = sheet.worksheet("Gastos")
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Gastos", rows=1000, cols=7)
        ws.append_row(["Data", "Descrição", "Categoria", "Valor", "Mês", "Ano", "Cartão"])
        return ws
    headers = ws.row_values(1)
    if "Cartão" not in headers:
        ws.update_cell(1, len(headers) + 1, "Cartão")
    return ws


def salvar_gasto(data: str, descricao: str, categoria: str, valor: float, cartao: str = "Sem cartão"):
    ws = get_sheet()
    dt = datetime.strptime(data, "%Y-%m-%d")
    ws.append_row([data, descricao, categoria, valor, dt.strftime("%B %Y"), dt.year, cartao])


def buscar_todos_gastos() -> list:
    return get_sheet().get_all_records()


def filtrar_por_periodo(gastos: list, inicio: datetime, fim: datetime) -> list:
    resultado = []
    for g in gastos:
        try:
            data = datetime.strptime(str(g["Data"]), "%Y-%m-%d")
            if inicio <= data <= fim:
                resultado.append(g)
        except:
            pass
    return resultado


def resumo_por_categoria(gastos: list) -> dict:
    cats = {}
    for g in gastos:
        cat = g.get("Categoria") or "Outros"
        cats[cat] = cats.get(cat, 0) + float(g.get("Valor", 0))
    return dict(sorted(cats.items(), key=lambda x: x[1], reverse=True))


def resumo_por_cartao(gastos: list) -> dict:
    cartoes = {}
    for g in gastos:
        cartao = g.get("Cartão") or "Sem cartão"
        cartoes[cartao] = cartoes.get(cartao, 0) + float(g.get("Valor", 0))
    return dict(sorted(cartoes.items(), key=lambda x: x[1], reverse=True))


def indicador(atual: float, anterior: float) -> str:
    if anterior == 0:
        return "🆕 novo"
    variacao = ((atual - anterior) / anterior) * 100
    if variacao > 10:
        return f"📈 +{variacao:.0f}%"
    elif variacao < -10:
        return f"📉 {variacao:.0f}%"
    return "➡️ em linha"


def frase_analise(total_atual: float, total_anterior: float, cats_atual: dict, cats_anterior: dict) -> str:
    if total_anterior == 0:
        return "📊 Sem dados anteriores para comparar"
    variacao = ((total_atual - total_anterior) / total_anterior) * 100
    if abs(variacao) <= 5:
        return f"➡️ Estável ({variacao:+.1f}% vs período anterior)"
    impactos = sorted(
        [(cat, val - cats_anterior.get(cat, 0)) for cat, val in cats_atual.items()],
        key=lambda x: abs(x[1]), reverse=True
    )[:2]
    detalhes = " e ".join([
        f"{cat} ({'+' if d > 0 else ''}R${d:.0f})" for cat, d in impactos if abs(d) > 0
    ])
    seta = "📈" if variacao > 0 else "📉"
    direcao = "Alta" if variacao > 0 else "Queda"
    base = f"{seta} {direcao} de {abs(variacao):.1f}% vs período anterior"
    return f"{base} — impactado por {detalhes}" if detalhes else base


def datas_semana(semanas_atras: int = 0):
    hoje = datetime.now()
    seg = hoje - timedelta(days=hoje.weekday())
    seg_ref = seg - timedelta(weeks=semanas_atras)
    dom_ref = seg_ref + timedelta(days=6)
    seg_ant = seg_ref - timedelta(weeks=1)
    dom_ant = seg_ant + timedelta(days=6)
    return seg_ref, dom_ref, seg_ant, dom_ant


def datas_mes(mes: int, ano: int):
    inicio = datetime(ano, mes, 1)
    fim = datetime(ano + 1, 1, 1) - timedelta(days=1) if mes == 12 else datetime(ano, mes + 1, 1) - timedelta(days=1)
    mes_ant, ano_ant = (12, ano - 1) if mes == 1 else (mes - 1, ano)
    inicio_ant = datetime(ano_ant, mes_ant, 1)
    fim_ant = datetime(ano_ant + 1, 1, 1) - timedelta(days=1) if mes_ant == 12 else datetime(ano_ant, mes_ant + 1, 1) - timedelta(days=1)
    return inicio, fim, inicio_ant, fim_ant


def relatorio_semana(gastos: list, semanas_atras: int = 0) -> str:
    seg_ref, dom_ref, seg_ant, dom_ant = datas_semana(semanas_atras)
    periodo = filtrar_por_periodo(gastos, seg_ref, dom_ref)
    anterior = filtrar_por_periodo(gastos, seg_ant, dom_ant)
    total = sum(float(g["Valor"]) for g in periodo)
    total_ant = sum(float(g["Valor"]) for g in anterior)
    cats = resumo_por_categoria(periodo)
    cats_ant = resumo_por_categoria(anterior)

    label = "Semana atual" if semanas_atras == 0 else "Semana passada"
    label_ant = "Semana anterior" if semanas_atras == 0 else "Semana retrasada"

    linhas = [
        f"📅 *{label}* ({seg_ref.strftime('%d/%m')} — {dom_ref.strftime('%d/%m')})",
        f"💰 Total: R$ {total:.2f}",
        frase_analise(total, total_ant, cats, cats_ant),
        "", "*🗂️ Por categoria:*",
    ]
    for cat, val in cats.items():
        linhas.append(f"  • {cat}: R$ {val:.2f} {indicador(val, cats_ant.get(cat, 0))}")

    if total_ant > 0:
        linhas += ["", f"📅 *{label_ant}* ({seg_ant.strftime('%d/%m')} — {dom_ant.strftime('%d/%m')}): R$ {total_ant:.2f}", "*🗂️ Por categoria:*"]
        for cat, val in cats_ant.items():
            linhas.append(f"  • {cat}: R$ {val:.2f}")

    return "\n".join(linhas)


def relatorio_mes(gastos: list, mes: int, ano: int) -> str:
    inicio, fim, inicio_ant, fim_ant = datas_mes(mes, ano)
    periodo = filtrar_por_periodo(gastos, inicio, fim)
    anterior = filtrar_por_periodo(gastos, inicio_ant, fim_ant)
    total = sum(float(g["Valor"]) for g in periodo)
    total_ant = sum(float(g["Valor"]) for g in anterior)
    cats = resumo_por_categoria(periodo)
    cats_ant = resumo_por_categoria(anterior)

    linhas = [
        f"📆 *{inicio.strftime('%B %Y').capitalize()}*",
        f"💰 Total: R$ {total:.2f}",
        frase_analise(total, total_ant, cats, cats_ant),
        "", "*🗂️ Por categoria:*",
    ]
    for cat, val in cats.items():
        linhas.append(f"  • {cat}: R$ {val:.2f} {indicador(val, cats_ant.get(cat, 0))}")

    if total_ant > 0:
        linhas += ["", f"📅 *{inicio_ant.strftime('%B %Y').capitalize()}:* R$ {total_ant:.2f}", "*🗂️ Por categoria:*"]
        for cat, val in cats_ant.items():
            linhas.append(f"  • {cat}: R$ {val:.2f}")

    return "\n".join(linhas)


def relatorio_cartoes(gastos: list, mes: int, ano: int) -> str:
    inicio, fim, _, _ = datas_mes(mes, ano)
    periodo = filtrar_por_periodo(gastos, inicio, fim)
    total = sum(float(g["Valor"]) for g in periodo)
    cartoes = resumo_por_cartao(periodo)

    linhas = [
        f"💳 *Resumo por cartão — {inicio.strftime('%B %Y').capitalize()}*",
        f"💰 Total: R$ {total:.2f}", "",
    ]
    for cartao, val in cartoes.items():
        pct = (val / total * 100) if total > 0 else 0
        linhas.append(f"  • {cartao}: R$ {val:.2f} ({pct:.0f}%)")

    return "\n".join(linhas)


claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def interpretar_gasto(mensagem: str) -> dict | None:
    hoje = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""Você é um assistente de controle de gastos pessoais.
O usuário enviou: "{mensagem}"
Data de hoje: {hoje}

Se for um gasto, responda APENAS com JSON válido:
{{"descricao": "...", "categoria": "...", "confianca": "alta|baixa", "categorias_alternativas": [], "valor": 0.00, "data": "YYYY-MM-DD", "cartao": "..."}}

Categorias: {", ".join(CATEGORIAS)}
Cartões: {", ".join(CARTOES)}

Regras:
- Se não mencionar data, use hoje ({hoje})
- Valor deve ser número float (sem R$)
- cartao: detecte XP, C6, Ifood, Inter, Nubank na mensagem. Se não mencionar, use "Sem cartão"
- confianca "alta" se categoria for óbvia, "baixa" se houver dúvida
- Se confianca "baixa", liste até 3 opções em categorias_alternativas
- Se NÃO for um gasto, responda: null"""

    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    logger.info(f"Resposta Claude: {text!r}")
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    if not text or text == "null":
        return None
    return json.loads(text)


def detectar_mes_ano(texto: str):
    for nome, num in MESES_PT.items():
        match = re.search(rf'{nome}\s+(\d{{4}})', texto)
        if match:
            return num, int(match.group(1))
    return None, None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID != 0 and update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Acesso não autorizado.")
        return

    texto = update.message.text.strip()
    texto_lower = texto.lower()

    # Confirmação de categoria pendente
    if context.user_data.get("pending_gasto"):
        pending = context.user_data["pending_gasto"]
        categoria_escolhida = next(
            (cat for cat in CATEGORIAS if cat.lower() == texto_lower or texto_lower == cat.lower()[:len(texto_lower)]),
            None
        )
        if categoria_escolhida:
            try:
                salvar_gasto(pending["data"], pending["descricao"], categoria_escolhida, pending["valor"], pending["cartao"])
                emoji = EMOJI_CAT.get(categoria_escolhida, "💸")
                await update.message.reply_text(
                    f"{emoji} *{categoria_escolhida}* registrado!\n📝 {pending['descricao']}\n💰 R$ {float(pending['valor']):.2f}\n📅 {pending['data']}\n💳 {pending['cartao']}",
                    parse_mode="Markdown"
                )
                context.user_data.pop("pending_gasto")
            except Exception as e:
                logger.error(f"Erro ao salvar: {e}", exc_info=True)
                await update.message.reply_text("❌ Erro ao salvar.")
                context.user_data.pop("pending_gasto")
        else:
            opts = " / ".join(pending.get("categorias_alternativas", []))
            await update.message.reply_text(f"❓ Não reconheci. Escolha: {opts}")
        return

    if any(p in texto_lower for p in ["menu", "ajuda", "help", "comandos"]):
        await update.message.reply_text(MENU_TEXT, parse_mode="Markdown")
        return

    if "semana passada" in texto_lower:
        await update.message.reply_text("📊 Gerando relatório...")
        await update.message.reply_text(relatorio_semana(buscar_todos_gastos(), semanas_atras=1), parse_mode="Markdown")
        return

    if "semana" in texto_lower:
        await update.message.reply_text("📊 Gerando relatório...")
        await update.message.reply_text(relatorio_semana(buscar_todos_gastos(), semanas_atras=0), parse_mode="Markdown")
        return

    if any(p in texto_lower for p in ["cartões", "cartoes", "cartão", "cartao"]):
        mes, ano = detectar_mes_ano(texto_lower)
        if not mes:
            mes, ano = datetime.now().month, datetime.now().year
        await update.message.reply_text(relatorio_cartoes(buscar_todos_gastos(), mes, ano), parse_mode="Markdown")
        return

    if any(p in texto_lower for p in ["resumo", "análise", "analise", "quanto gastei"]):
        await update.message.reply_text("📊 Gerando relatório...")
        hoje = datetime.now()
        await update.message.reply_text(relatorio_mes(buscar_todos_gastos(), hoje.month, hoje.year), parse_mode="Markdown")
        return

    if any(p in texto_lower for p in ["histórico", "historico", "total geral", "todos os gastos"]):
        gastos = buscar_todos_gastos()
        total = sum(float(g["Valor"]) for g in gastos)
        cats = resumo_por_categoria(gastos)
        linhas = [f"📊 *Histórico completo*", f"💰 Total geral: R$ {total:.2f}", "", "*🗂️ Por categoria:*"]
        for cat, val in cats.items():
            linhas.append(f"  • {cat}: R$ {val:.2f}")
        await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")
        return

    mes, ano = detectar_mes_ano(texto_lower)
    if mes and ano:
        await update.message.reply_text("📊 Gerando relatório...")
        await update.message.reply_text(relatorio_mes(buscar_todos_gastos(), mes, ano), parse_mode="Markdown")
        return

    await update.message.reply_text("⏳ Processando...")
    try:
        gasto = interpretar_gasto(texto)
    except Exception as e:
        logger.error(f"Erro ao interpretar: {e}")
        await update.message.reply_text("❌ Erro ao processar. Tente novamente.")
        return

    if gasto is None:
        await update.message.reply_text("🤔 Não entendi como um gasto. Digite `menu` para ver os comandos.", parse_mode="Markdown")
        return

    cartao = gasto.get("cartao") or "Sem cartão"

    if gasto.get("confianca") == "baixa":
        alternativas = gasto.get("categorias_alternativas") or [gasto["categoria"]]
        if gasto["categoria"] not in alternativas:
            alternativas = [gasto["categoria"]] + alternativas
        alternativas = alternativas[:3]
        context.user_data["pending_gasto"] = {
            "data": gasto["data"], "descricao": gasto["descricao"],
            "valor": gasto["valor"], "cartao": cartao,
            "categorias_alternativas": alternativas
        }
        opts = " / ".join(alternativas)
        await update.message.reply_text(f"🤔 *{gasto['descricao']}* — qual categoria?\n{opts}", parse_mode="Markdown")
        return

    try:
        salvar_gasto(gasto["data"], gasto["descricao"], gasto["categoria"], gasto["valor"], cartao)
        emoji = EMOJI_CAT.get(gasto["categoria"], "💸")
        await update.message.reply_text(
            f"{emoji} *{gasto['categoria']}* registrado!\n📝 {gasto['descricao']}\n💰 R$ {float(gasto['valor']):.2f}\n📅 {gasto['data']}\n💳 {cartao}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Erro ao salvar: {e}", exc_info=True)
        await update.message.reply_text("❌ Erro ao salvar. Verifique as configurações do Google Sheets.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID != 0 and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(MENU_TEXT, parse_mode="Markdown")


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID != 0 and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(MENU_TEXT, parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot rodando...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
