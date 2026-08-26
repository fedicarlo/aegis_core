"""
AEGIS Analytics Engine
Motor de cálculo de performance por anúncio/seller.

Responsabilidades:
  - Giro em 3 janelas (7, 15, 30 dias)
  - Tendência: compara giro recente (7d) vs giro anterior (dias 16-30)
  - Cobertura de estoque FULL em dias
  - Classificação: Crítico / Oportunidade / Estável / Descarte
  - Alertas acionáveis por item

Separação de responsabilidades:
  - Este módulo só contém lógica de negócio.
  - Acesso a dados fica em app.database.get_raw_data_for_analytics().
"""

from __future__ import annotations
from app.database import get_raw_data_for_analytics

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds — ajuste conforme o negócio
# ─────────────────────────────────────────────────────────────────────────────

# Cobertura de estoque (dias)
COVERAGE_CRITICAL = 7    # abaixo: reposição urgente
COVERAGE_WARNING  = 30   # abaixo: planejar reposição

# Giro mínimo para considerar item "ativo" (filtra ruído de 1 venda esporádica)
GIRO_MIN = 0.05          # ~1.5 unidades/mês

# Volume mínimo (30d) para acionar alertas de tendência
# evita ruído em itens de baixíssimo giro
TREND_MIN_VOLUME = 5

# Giro mínimo em 30d para acionar classificação por tendência (Oportunidade)
# Evita alarme em itens com vendas esporádicas (1-2 unidades no mês)
TREND_ALERT_MIN_GIRO30 = 0.5   # ~15 unidades/mês

# Razões de comparação para detectar tendência
TREND_ACCEL_RATIO = 1.3  # giro_recente > giro_anterior * 1.3 → acelerando
TREND_DECEL_RATIO = 0.6  # giro_recente < giro_anterior * 0.6 → desacelerando

# ─────────────────────────────────────────────────────────────────────────────
# Maturidade — baseada em sold_quantity acumulado
# ─────────────────────────────────────────────────────────────────────────────
# Cada estágio carrega um fator de escala que amplifica ou modera a sugestão
# de indução de estoque FULL:
#   - Emergindo : fator 0.5 → ordem conservadora (demanda ainda não provada)
#   - Crescendo : fator 0.75 → confiança moderada (tração confirmada)
#   - Maduro    : fator 1.0 → cobertura-alvo padrão (demanda previsível)
#   - Dominante : fator 1.5 → buffer ampliado (líder, escalar operações)
#
# Formato: (limite_inferior_inclusive, limite_superior_inclusive, label, fator)
# None no limite superior = sem limite.
MATURITY_STAGES: list[tuple] = [
    (0,   50,  "Emergindo",  0.5),
    (51,  200, "Crescendo",  0.75),
    (201, 500, "Maduro",     1.0),
    (501, None,"Dominante",  1.5),
]

# Cobertura-alvo (dias) usada como base para calcular a sugestão de indução.
# A indução sugerida leva o estoque até este patamar, escalado pela maturidade.
COVERAGE_TARGET_DAYS = 30

# ─────────────────────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────────────────────

_SORT_PRIORITY = {"Crítico": 0, "Oportunidade": 1, "Estável": 2, "Descarte": 3}


def compute_all(seller_id: str) -> list[dict]:
    """
    Ponto de entrada principal.
    Retorna todos os anúncios do seller com métricas calculadas,
    ordenados por prioridade de ação (Crítico primeiro) e giro desc.
    """
    rows  = get_raw_data_for_analytics(seller_id)
    items = [_compute_item(r) for r in rows]
    items.sort(key=lambda x: (_SORT_PRIORITY[x["classification"]], -x["giro_7d"]))
    return items


def summary(items: list[dict]) -> dict:
    """
    Totalizadores para o header do dashboard.
    Recebe a lista já computada por compute_all().
    """
    by_class: dict[str, int] = {"Crítico": 0, "Oportunidade": 0, "Estável": 0, "Descarte": 0}
    for item in items:
        by_class[item["classification"]] += 1

    return {
        "total":        len(items),
        "critico":      by_class["Crítico"],
        "oportunidade": by_class["Oportunidade"],
        "estavel":      by_class["Estável"],
        "descarte":     by_class["Descarte"],
        "qty_30d":      sum(i["qty_30d"]  for i in items),
        "qty_7d":       sum(i["qty_7d"]   for i in items),
        "giro_medio_7d": round(
            sum(i["giro_7d"] for i in items if i["giro_7d"] > GIRO_MIN)
            / max(1, sum(1 for i in items if i["giro_7d"] > GIRO_MIN)), 2
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Computação por item
# ─────────────────────────────────────────────────────────────────────────────

def _compute_item(row: dict) -> dict:
    qty_7d  = row["qty_7d"]
    qty_15d = row["qty_15d"]
    qty_30d = row["qty_30d"]
    stock         = row["stock"]
    stock_tracked = bool(row["stock_tracked"])
    status        = row["status"]
    sold_quantity = row.get("sold_quantity") or 0

    # ── Giro (unidades/dia) ───────────────────────────────────────────────────
    giro_7d  = round(qty_7d  / 7,  2)
    giro_15d = round(qty_15d / 15, 2)
    giro_30d = round(qty_30d / 30, 2)

    # ── Tendência: últimos 7d vs dias 16-30 ──────────────────────────────────
    # giro_early = velocidade nos dias 16-30 (qty entre 15d e 30d / 15 dias)
    qty_early  = qty_30d - qty_15d
    giro_early = round(qty_early / 15, 2) if qty_early > 0 else 0.0
    trend      = _compute_trend(giro_7d, giro_early, qty_30d)

    # ── Cobertura ─────────────────────────────────────────────────────────────
    # Só calculada quando temos estoque rastreado > 0 e giro recente positivo.
    # giro_7d é o mais representativo para estimar quando o estoque acaba.
    if stock_tracked and stock > 0 and giro_7d > GIRO_MIN:
        coverage_days = round(stock / giro_7d, 1)
    else:
        coverage_days = None

    # ── Maturidade e indução ──────────────────────────────────────────────────
    maturity     = _compute_maturity(sold_quantity)
    induction_qty = _compute_induction(giro_7d, stock, maturity["factor"])

    # ── Classificação ─────────────────────────────────────────────────────────
    classification, reason = _classify(
        giro_7d, giro_30d, giro_early,
        stock, stock_tracked, coverage_days,
        status, trend,
    )

    # ── Alertas ───────────────────────────────────────────────────────────────
    alerts = _build_alerts(
        status, row["is_catalog"],
        giro_7d, giro_30d,
        stock, stock_tracked,
        trend, coverage_days,
    )

    # Dias desde a última venda real no banco
    last_sale_date = row.get("last_sale_date")
    days_since_last_sale = None
    if last_sale_date:
        from datetime import date
        try:
            last_dt = date.fromisoformat(last_sale_date)
            days_since_last_sale = (date.today() - last_dt).days
        except ValueError:
            pass

    return {
        # Identificação
        "id":                   row["id"],
        "title":                row["title"],
        "status":               status,
        "is_catalog":           bool(row["is_catalog"]),
        "price":                row["price"],
        # Volumes brutos por janela
        "qty_7d":               qty_7d,
        "qty_15d":              qty_15d,
        "qty_30d":              qty_30d,
        # Giro (unid./dia)
        "giro_7d":              giro_7d,
        "giro_15d":             giro_15d,
        "giro_30d":             giro_30d,
        "giro_early":           giro_early,
        # Estoque
        "stock":                stock,
        "stock_tracked":        stock_tracked,
        # Cobertura
        "coverage_days":        coverage_days,
        # Maturidade
        "sold_quantity":        sold_quantity,
        "maturity_stage":       maturity["stage"],
        "maturity_factor":      maturity["factor"],
        # Indução sugerida
        "induction_qty":        induction_qty,
        # Análise
        "trend":                trend,
        "classification":       classification,
        "reason":               reason,
        "alerts":               alerts,
        # Data real da última venda
        "last_sale_date":       last_sale_date,
        "days_since_last_sale": days_since_last_sale,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Maturidade
# ─────────────────────────────────────────────────────────────────────────────

def _compute_maturity(sold_quantity: int) -> dict:
    """
    Retorna o estágio de maturidade do produto baseado em sold_quantity acumulado.

    Retorna dict com:
      stage   — label do estágio (Emergindo / Crescendo / Maduro / Dominante)
      factor  — multiplicador para a sugestão de indução
    """
    for low, high, label, factor in MATURITY_STAGES:
        if high is None or sold_quantity <= high:
            return {"stage": label, "factor": factor}
    # fallback de segurança (não deve ocorrer)
    return {"stage": "Dominante", "factor": 1.5}


def _compute_induction(giro_7d: float, stock: int, maturity_factor: float) -> int:
    """
    Sugere a quantidade a induzir no estoque FULL para atingir COVERAGE_TARGET_DAYS
    de cobertura, escalada pelo fator de maturidade do produto.

    Fórmula:
      base      = giro_7d × COVERAGE_TARGET_DAYS   (estoque-alvo em unidades)
      gap       = base − estoque_atual              (quanto falta)
      sugestão  = max(0, gap) × maturity_factor

    Retorna 0 se o item não tem giro ativo ou se o estoque já supera a meta.
    """
    if giro_7d <= GIRO_MIN:
        return 0
    target = giro_7d * COVERAGE_TARGET_DAYS
    gap    = target - stock
    if gap <= 0:
        return 0
    return max(1, round(gap * maturity_factor))


# ─────────────────────────────────────────────────────────────────────────────
# Tendência
# ─────────────────────────────────────────────────────────────────────────────

def _compute_trend(giro_7d: float, giro_early: float, qty_30d: int) -> str:
    """
    Compara giro recente (últimos 7d) com giro anterior (dias 16-30).
    Retorna: acelerando | desacelerando | esfriando | neutro | estável
    """
    # Sem volume relevante → neutro
    if qty_30d < TREND_MIN_VOLUME:
        return "neutro"

    recente  = giro_7d
    anterior = giro_early

    if recente < GIRO_MIN and anterior < GIRO_MIN:
        return "neutro"

    # Vendia antes, zerou agora
    if recente < GIRO_MIN and anterior >= GIRO_MIN:
        return "esfriando"

    # Não vendia antes, acelerou agora
    if anterior < GIRO_MIN and recente >= GIRO_MIN:
        return "acelerando"

    ratio = recente / anterior
    if ratio >= TREND_ACCEL_RATIO:
        return "acelerando"
    if ratio <= TREND_DECEL_RATIO:
        return "desacelerando"
    return "estável"


# ─────────────────────────────────────────────────────────────────────────────
# Classificação
# ─────────────────────────────────────────────────────────────────────────────

def _classify(
    giro_7d: float,
    giro_30d: float,
    giro_early: float,
    stock: int,
    stock_tracked: bool,
    coverage_days: float | None,
    status: str,
    trend: str,
) -> tuple[str, str]:
    """
    Retorna (classificação, motivo).

    Hierarquia de decisão:
      1. Descarte  — sem vendas nos últimos 30 dias
      2. Crítico   — cobertura < 7d OU vendendo sem estoque FULL
      3. Oportunidade — esfriando / pausado com vendas / desacelerando / cobertura curta
      4. Estável   — demais casos com giro positivo
    """

    # ── 1. DESCARTE ───────────────────────────────────────────────────────────
    if giro_30d < GIRO_MIN:
        return "Descarte", "sem vendas nos últimos 30 dias"

    # ── 2. CRÍTICO ────────────────────────────────────────────────────────────
    if stock_tracked and stock > 0 and coverage_days is not None:
        if coverage_days < COVERAGE_CRITICAL:
            return (
                "Crítico",
                f"cobertura de {coverage_days}d com giro de {giro_7d}/dia "
                f"(crítico abaixo de {COVERAGE_CRITICAL}d)",
            )

    out_of_stock = stock_tracked and stock == 0

    if giro_7d > GIRO_MIN and out_of_stock:
        return "Crítico", "vendendo com estoque FULL zerado — reposição urgente"

    # Ruptura que já zerou o giro: a causa é falta de estoque, não desinteresse.
    # Sem esta checagem, o trend "esfriando"/"desacelerando" (calculado só a
    # partir do giro) classificava esses itens como Oportunidade, escondendo
    # a urgência real de reposição.
    if out_of_stock and giro_30d >= TREND_ALERT_MIN_GIRO30:
        return (
            "Crítico",
            f"sem estoque — vendia {giro_early}/dia (dias 16-30) e zerou o "
            f"estoque FULL (giro 30d: {giro_30d}/dia) — reposição urgente",
        )

    # ── 3. OPORTUNIDADE ───────────────────────────────────────────────────────

    # Esfriando: vendia bem nos dias 16-30, zerou nos últimos 7d
    if trend == "esfriando" and giro_30d >= TREND_ALERT_MIN_GIRO30:
        return (
            "Oportunidade",
            f"esfriando — giro caiu de {giro_early}/dia (dias 16-30) "
            f"para zero nos últimos 7 dias (verificar data real da última venda)",
        )

    # Pausado com vendas ativas (catálogo ou erro de configuração)
    if status == "paused" and giro_7d > GIRO_MIN:
        return (
            "Oportunidade",
            "anúncio pausado com vendas ativas — verificar se está em catálogo",
        )

    # Desacelerando forte com volume relevante
    if trend == "desacelerando" and giro_30d >= TREND_ALERT_MIN_GIRO30:
        return (
            "Oportunidade",
            f"desacelerando — giro caiu de {giro_early}/dia para {giro_7d}/dia",
        )

    # Cobertura em zona de atenção (mas fora do crítico)
    if coverage_days is not None and coverage_days < COVERAGE_WARNING:
        return (
            "Oportunidade",
            f"cobertura de {coverage_days}d — planejar reposição "
            f"(abaixo de {COVERAGE_WARNING}d)",
        )

    # Sem vendas nos últimos 7 dias mas teve atividade no mês
    # (casos que não foram capturados pelos checks anteriores de tendência)
    if giro_7d < GIRO_MIN and giro_30d > GIRO_MIN:
        return "Oportunidade", "sem vendas nos últimos 7 dias — monitorar"

    # ── 4. ESTÁVEL ────────────────────────────────────────────────────────────
    cob_str = f"{coverage_days}d" if coverage_days is not None else "não rastreada"
    return "Estável", f"giro de {giro_7d}/dia — cobertura {cob_str}"


# ─────────────────────────────────────────────────────────────────────────────
# Alertas
# ─────────────────────────────────────────────────────────────────────────────

def _build_alerts(
    status: str,
    is_catalog: int,
    giro_7d: float,
    giro_30d: float,
    stock: int,
    stock_tracked: bool,
    trend: str,
    coverage_days: float | None,
) -> list[str]:
    alerts = []

    if is_catalog:
        alerts.append("catálogo")

    if status == "paused" and not is_catalog:
        alerts.append("pausado")

    out_of_stock = stock_tracked and stock == 0

    if out_of_stock:
        alerts.append("sem estoque")
    elif giro_30d > GIRO_MIN and not stock_tracked:
        alerts.append("estoque não rastreado")

    # Se o item está sem estoque, o giro em queda é consequência da ruptura,
    # não desinteresse do mercado — não reportar esfriando/desacelerando junto.
    if not out_of_stock:
        if trend == "esfriando":
            alerts.append("esfriando")
        elif trend == "desacelerando":
            alerts.append("desacelerando")
        elif trend == "acelerando" and giro_7d >= 1.0:
            alerts.append("acelerando")

    return alerts


# ─────────────────────────────────────────────────────────────────────────────
# Alertas, KPIs e dados de gráfico — funções públicas
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Módulo 2 — Motor de risco preditivo
# ─────────────────────────────────────────────────────────────────────────────

_RISK_LEVELS = {
    "RUPTURA": (100, "#f44336"),
    "CRÍTICO": (80,  "#f44336"),
    "URGENTE": (55,  "#ff9800"),
    "ATENÇÃO": (30,  "#ffd54f"),
    "ESTÁVEL": (0,   "#4caf50"),
}


def compute_risk_score(item: dict, stock_history: list) -> dict:
    """
    Score de risco 0-100 + nível de urgência para um anúncio.

    item          : dict do compute_all
    stock_history : lista [{date, available_qty}] ordenada por data (pode ser vazia)

    Retorna:
      score             — 0-100
      level             — RUPTURA / CRÍTICO / URGENTE / ATENÇÃO / ESTÁVEL
      days_until_rupture — float | None
      rupture_date       — ISO date str | None
      send_deadline      — ISO date str | None (rupture - 20d ciclo logístico)
      color              — hex para o badge
      components         — breakdown do score
    """
    from datetime import date, timedelta

    giro_7d       = item["giro_7d"]
    stock         = item["stock"]
    stock_tracked = item["stock_tracked"]
    trend         = item["trend"]
    today         = date.today()

    # ── Dias até ruptura ─────────────────────────────────────────────────────
    if giro_7d > GIRO_MIN and stock_tracked:
        days_until_rupture = stock / giro_7d
    else:
        days_until_rupture = None

    # ── Coverage score (0–90) ────────────────────────────────────────────────
    if not stock_tracked or giro_7d <= GIRO_MIN:
        cov_score = 0
    elif stock == 0:
        cov_score = 100
    elif days_until_rupture < 5:
        cov_score = 90
    elif days_until_rupture < 10:
        cov_score = 75
    elif days_until_rupture < 20:
        cov_score = 50
    elif days_until_rupture < 30:
        cov_score = 20
    else:
        cov_score = 0

    # ── Trend modifier ───────────────────────────────────────────────────────
    trend_mod = {"acelerando": 15, "desacelerando": -5, "esfriando": -10}.get(trend, 0)

    # ── History modifier (rupturas anteriores) ───────────────────────────────
    hist_mod = 0
    past_zero = sum(1 for h in stock_history if h["available_qty"] == 0)
    if past_zero > 5:
        hist_mod = 15
    elif past_zero > 0:
        hist_mod = 8

    score = min(100, max(0, cov_score + trend_mod + hist_mod))

    # ── Nível ────────────────────────────────────────────────────────────────
    if stock == 0 and giro_7d > GIRO_MIN:
        level = "RUPTURA"
        score = 100
    elif days_until_rupture is not None and days_until_rupture < 10:
        level = "CRÍTICO"
    elif days_until_rupture is not None and days_until_rupture < 20:
        level = "URGENTE"
    elif days_until_rupture is not None and days_until_rupture < 30:
        level = "ATENÇÃO"
    else:
        level = "ESTÁVEL"

    # ── Datas ────────────────────────────────────────────────────────────────
    rupture_date  = None
    send_deadline = None
    if days_until_rupture is not None:
        rupture_date  = today + timedelta(days=round(days_until_rupture))
        send_deadline = rupture_date - timedelta(days=20)
        if send_deadline < today:
            send_deadline = today

    color = _RISK_LEVELS.get(level, _RISK_LEVELS["ESTÁVEL"])[1]

    return {
        "score":              score,
        "level":              level,
        "color":              color,
        "days_until_rupture": round(days_until_rupture, 1) if days_until_rupture is not None else None,
        "rupture_date":       rupture_date.isoformat()  if rupture_date  else None,
        "send_deadline":      send_deadline.isoformat() if send_deadline else None,
        "past_zero_days":     past_zero,
        "components":         {"coverage": cov_score, "trend": trend_mod, "history": hist_mod},
    }


def compute_risk_all(seller_id: str) -> list:
    """
    Scores de risco para todos os itens ativos — alimenta o calendário.
    Retorna lista ordenada por score desc, somente itens com level != ESTÁVEL.
    """
    from app.database import get_stock_history_batch
    items   = compute_all(seller_id)
    history = get_stock_history_batch(seller_id, days=30)

    result = []
    for item in items:
        if item["giro_30d"] <= GIRO_MIN:
            continue
        h = history.get(item["id"], [])
        risk      = compute_risk_score(item, h)
        if risk["level"] == "ESTÁVEL":
            continue
        induction = compute_induction_enhanced(item, h)
        result.append({**item, "risk": risk, "induction": induction})

    result.sort(key=lambda x: (-x["risk"]["score"], -x["giro_7d"]))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Módulo 3 — Impacto financeiro de rupturas
# ─────────────────────────────────────────────────────────────────────────────

def compute_rupture_impact(seller_id: str, item_id: str, days: int = 90) -> dict:
    """
    Estima receita perdida em períodos de estoque zerado nos últimos N dias.

    Para cada dia com stock=0, assume que o giro médio do período ativo
    representa a demanda não atendida.
    """
    from app.database import get_stock_history, get_product_orders_daily

    history      = get_stock_history(seller_id, item_id, days)
    orders_daily = get_product_orders_daily(seller_id, item_id, days)

    if not history:
        return {"lost_units": 0, "lost_revenue": 0.0, "rupture_days": 0, "avg_giro": 0.0}

    orders_map = {o["day"]: o for o in orders_daily}

    # Giro médio nos dias com estoque disponível
    active_units = [
        orders_map.get(h["date"], {}).get("units", 0) or 0
        for h in history if h["available_qty"] > 0
    ]
    avg_giro = sum(active_units) / max(1, len(active_units)) if active_units else 0.0

    # Ticket médio
    total_units   = sum(o.get("units",   0) or 0 for o in orders_daily)
    total_revenue = sum(o.get("revenue", 0) or 0 for o in orders_daily)
    avg_ticket    = total_revenue / total_units if total_units > 0 else 0.0

    rupture_days = sum(1 for h in history if h["available_qty"] == 0)
    lost_units   = rupture_days * avg_giro
    lost_revenue = lost_units * avg_ticket

    return {
        "lost_units":   round(lost_units, 1),
        "lost_revenue": round(lost_revenue, 2),
        "rupture_days": rupture_days,
        "avg_giro":     round(avg_giro, 2),
        "avg_ticket":   round(avg_ticket, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Módulo 4 — Calculadora de envio inteligente
# ─────────────────────────────────────────────────────────────────────────────

def compute_induction_enhanced(item: dict, stock_history: list,
                               cycle_days: int = 20) -> dict:
    """
    Calcula quantidade e prazo de envio com base em estado detectado do produto.

    Estados:
      saudável       — giro_7d >= giro_30d × 0.7
      em_recuperacao — teve ruptura recente + giro caído
      queda_real     — giro caiu sem ruptura recente

    Retorna: qty, prazo (ISO date), prazo_label, justificativa, giro_base, state,
             dias_alvo, trend_factor
    """
    from datetime import date, timedelta

    giro_7d         = item["giro_7d"]
    giro_30d        = item["giro_30d"]
    stock           = item["stock"]
    maturity_factor = item["maturity_factor"]
    trend           = item["trend"]
    today           = date.today()

    # ── Detectar estado ──────────────────────────────────────────────────────
    recent_rupture = any(
        h["available_qty"] == 0 for h in stock_history[-14:]
    ) if stock_history else False

    if giro_7d >= giro_30d * 0.7 or giro_30d <= GIRO_MIN:
        state = "saudável"
    elif recent_rupture and giro_7d < giro_30d * 0.5:
        state = "em_recuperacao"
    elif giro_7d < giro_30d * 0.5:
        state = "queda_real"
    else:
        state = "saudável"

    # ── Giro base ────────────────────────────────────────────────────────────
    if state == "em_recuperacao":
        # Melhor estimativa: usar giro_30d como potencial (abrange pré-ruptura)
        giro_base  = max(giro_30d, giro_7d)
        giro_label = f"potencial ({giro_base}/d) — recuperação"
    elif state == "queda_real":
        giro_base  = giro_30d
        giro_label = f"giro 30d ({giro_base}/d) — conservador"
    else:
        giro_base  = giro_7d if giro_7d > GIRO_MIN else giro_30d
        giro_label = f"giro 7d ({giro_base}/d)"

    if giro_base <= GIRO_MIN:
        return {
            "qty": 0, "prazo": today.isoformat(), "prazo_label": "sem giro ativo",
            "justificativa": "Item sem vendas ativas — não recomendado.",
            "giro_base": 0.0, "state": state, "dias_alvo": 0, "trend_factor": 1.0,
        }

    # ── Fator de tendência ───────────────────────────────────────────────────
    trend_factor = {"acelerando": 1.2, "desacelerando": 0.9, "esfriando": 0.8}.get(trend, 1.0)

    # ── Envio sugerido ───────────────────────────────────────────────────────
    safety    = 1.2
    dias_alvo = 30 + cycle_days   # 50 dias padrão
    envio     = max(0, round(
        giro_base * dias_alvo * maturity_factor * trend_factor * safety - stock
    ))

    # ── Prazo máximo de envio ────────────────────────────────────────────────
    risk = compute_risk_score(item, stock_history)
    dur  = risk["days_until_rupture"]

    if dur is None or dur < cycle_days:
        prazo       = today
        prazo_label = "HOJE — envio urgente"
    else:
        prazo       = today + timedelta(days=max(0, round(dur) - cycle_days))
        prazo_label = prazo.strftime("%d/%m/%Y")

    justificativa = (
        f"Estado: {state} | Base: {giro_label} | "
        f"Maturidade: ×{maturity_factor} | "
        f"Tendência: {trend} (×{trend_factor}) | "
        f"Segurança: ×{safety} | Alvo: {dias_alvo}d cobertura"
    )

    return {
        "qty":           envio,
        "prazo":         prazo.isoformat(),
        "prazo_label":   prazo_label,
        "justificativa": justificativa,
        "giro_base":     giro_base,
        "state":         state,
        "dias_alvo":     dias_alvo,
        "trend_factor":  trend_factor,
    }


def _margin_at_price(price: float, unit_cost: float, tax_rate: float,
                     ml_fee_rate: float, shipping_cost: float = 0.0):
    """Margem % para um preço unitário. Retorna None se price <= 0."""
    if price <= 0:
        return None
    comissao    = price * ml_fee_rate
    receita_liq = price - comissao - shipping_cost
    lucro       = receita_liq - unit_cost - price * tax_rate
    return round(lucro / price * 100, 1)


def compute_promo_profitability(seller_id: str) -> list:
    """
    Para cada item em promoção ativa, compara margem no preço original vs promocional.

    Retorna lista ordenada (margem negativa primeiro, depois por desconto desc).
    Campos por item:
      item_id, title, promotion_id, promotion_type,
      original_price, promo_price, discount_pct,
      margem_original, margem_promo, delta_margem,
      lucro_unit_promo, has_cost, is_negative
    """
    from app.database import get_promotions_for_profitability
    rows = get_promotions_for_profitability(seller_id)

    result = []
    for r in rows:
        orig  = float(r["original_price"] or 0)
        promo = float(r["promo_price"] or 0)

        has_cost      = r["unit_cost"] is not None
        unit_cost     = float(r["unit_cost"] or 0)
        tax_rate      = float(r["tax_rate"] or 0)
        ml_fee_rate   = float(r["ml_fee_rate"]) if r["ml_fee_rate"] is not None else 0.14
        shipping_cost = float(r["shipping_cost"] or 0)

        m_orig  = _margin_at_price(orig,  unit_cost, tax_rate, ml_fee_rate, shipping_cost)
        m_promo = _margin_at_price(promo, unit_cost, tax_rate, ml_fee_rate, shipping_cost)

        disc = float(r["discount_pct"] or 0)
        if disc == 0 and orig > 0 and promo > 0:
            disc = round((orig - promo) / orig * 100, 1)

        lucro_unit_promo = None
        if has_cost and promo > 0:
            comissao    = promo * ml_fee_rate
            receita_liq = promo - comissao - shipping_cost
            lucro_unit_promo = round(receita_liq - unit_cost - promo * tax_rate, 2)

        delta = None
        if m_promo is not None and m_orig is not None:
            delta = round(m_promo - m_orig, 1)

        result.append({
            "item_id":          r["item_id"],
            "title":            r["title"] or r["item_id"],
            "promotion_id":     r["promotion_id"],
            "promotion_type":   r["promotion_type"],
            "original_price":   round(orig, 2),
            "promo_price":      round(promo, 2),
            "discount_pct":     disc,
            "margem_original":  m_orig,
            "margem_promo":     m_promo,
            "lucro_unit_promo": lucro_unit_promo,
            "delta_margem":     delta,
            "has_cost":         has_cost,
            "is_negative":      has_cost and m_promo is not None and m_promo < 0,
        })

    result.sort(key=lambda x: (0 if x["is_negative"] else 1, -x["discount_pct"]))
    return result


def get_promo_alerts(seller_id: str) -> list:
    """
    Alertas de promoções ativas para o painel do dashboard.
    Lê diretamente do banco — deve ser chamado com seller_id.
    """
    from app.database import get_promotions_summary
    summary = get_promotions_summary(seller_id)

    if not summary or summary["items_count"] == 0:
        return []

    alerts = []

    alerts.append({
        "severity":  "promo",
        "type":      "promocao_ativa",
        "title":     f"{summary['items_count']} em promoção",
        "detail":    (
            f"{summary['total_promos']} promoção(ões) ativa(s) · "
            f"desconto médio {summary['avg_discount']:.1f}%"
        ),
        "item_id":   None,
        "seller_id": seller_id,
    })

    # Alertas de margem negativa só se custos estiverem configurados
    try:
        promos = compute_promo_profitability(seller_id)
        neg = [p for p in promos if p["is_negative"]]
        if neg:
            titles = ", ".join(p["title"][:28] for p in neg[:2])
            if len(neg) > 2:
                titles += f" +{len(neg)-2}"
            alerts.append({
                "severity":  "high",
                "type":      "promocao_margem_negativa",
                "title":     "Margem negativa em promoção",
                "detail":    f"{len(neg)} produto(s) vendendo no prejuízo: {titles}",
                "item_id":   None,
                "seller_id": seller_id,
            })
    except Exception:
        pass

    return alerts


def get_alerts(items: list) -> list:
    """
    Gera lista priorizada de alertas a partir dos itens já computados.

    Tipos de alerta:
      sem_estoque       — estoque FULL zerado (com giro ativo ou vendia recentemente)
      cobertura_critica — cobertura < COVERAGE_CRITICAL dias
      pausado_vendendo — status paused com giro ativo
      esfriando        — trend esfriando com volume relevante (item COM estoque)
      desacelerando    — trend desacelerando com giro > 1/dia (item COM estoque)

    Importante: quando o item está sem estoque (out_of_stock), o giro cai a
    zero como CONSEQUÊNCIA da ruptura, não por desinteresse do mercado. Por
    isso esfriando/desacelerando só são emitidos para itens COM estoque —
    caso contrário o alerta correto e urgente é "sem_estoque".
    """
    _SEV_ORDER = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    alerts = []

    for item in items:
        t = item['title'][:50]
        out_of_stock = item['stock_tracked'] and item['stock'] == 0
        had_recent_demand = item['giro_30d'] >= TREND_ALERT_MIN_GIRO30 or item['giro_7d'] > GIRO_MIN

        if out_of_stock and had_recent_demand:
            alerts.append({
                'severity': 'critical',
                'type':     'sem_estoque',
                'title':    'Sem estoque',
                'detail':   f"{t} — giro de {item['giro_30d']}/dia (30d), estoque FULL zerado. Ação imediata.",
                'item_id':  item['id'],
            })

        elif (item.get('coverage_days') is not None
              and item['coverage_days'] < COVERAGE_CRITICAL
              and item['giro_7d'] > GIRO_MIN):
            alerts.append({
                'severity': 'critical',
                'type':     'cobertura_critica',
                'title':    'Cobertura crítica',
                'detail':   f"{t} — {item['coverage_days']}d restantes",
                'item_id':  item['id'],
            })

        if item['status'] == 'paused' and item['giro_7d'] > GIRO_MIN:
            alerts.append({
                'severity': 'high',
                'type':     'pausado_vendendo',
                'title':    'Pausado com vendas',
                'detail':   f"{t} — {item['giro_7d']}/dia, verificar catálogo",
                'item_id':  item['id'],
            })

        if not out_of_stock:
            if item['trend'] == 'esfriando' and item['giro_30d'] >= TREND_ALERT_MIN_GIRO30:
                days_off = item.get('days_since_last_sale')
                last_dt  = item.get('last_sale_date', '')
                if days_off is not None and last_dt:
                    last_fmt = f"{last_dt[8:10]}/{last_dt[5:7]}"
                    sem_venda = f"sem vendas há {days_off}d (últ: {last_fmt})"
                else:
                    sem_venda = "sem vendas nos últimos 7d"
                alerts.append({
                    'severity': 'medium',
                    'type':     'esfriando',
                    'title':    'Esfriando',
                    'detail':   f"{t} — era {item['giro_early']}/dia, {sem_venda}",
                    'item_id':  item['id'],
                })

            if (item['trend'] == 'desacelerando'
                    and item['giro_30d'] >= 1.0
                    and item.get('giro_early', 0) > 0):
                days_off = item.get('days_since_last_sale')
                last_dt  = item.get('last_sale_date', '')
                last_info = ""
                if days_off is not None and last_dt:
                    last_fmt = f"{last_dt[8:10]}/{last_dt[5:7]}"
                    last_info = f" (últ: {last_fmt})"
                alerts.append({
                    'severity': 'low',
                    'type':     'desacelerando',
                    'title':    'Desacelerando',
                    'detail':   f"{t} — de {item['giro_early']}/dia → {item['giro_7d']}/dia{last_info}",
                    'item_id':  item['id'],
                })

    # Catálogo ML — alerta consolidado
    catalog_items = [i for i in items if i["is_catalog"]]
    if catalog_items:
        sample = ", ".join(i["title"][:28] for i in catalog_items[:3])
        if len(catalog_items) > 3:
            sample += f" +{len(catalog_items)-3}"
        alerts.append({
            "severity": "info",
            "type":     "catalogo",
            "title":    f"{len(catalog_items)} em catálogo ML",
            "detail":   f"Controlados pelo Mercado Livre: {sample}",
            "item_id":  None,
        })

    _SEV_ORDER["info"] = 4
    alerts.sort(key=lambda x: (_SEV_ORDER[x["severity"]], x["title"]))
    return alerts


def build_kpis(seller_id: str) -> dict:
    """
    KPIs com comparativo: atual (30d) vs anterior (dias 31-60).
    Campos por métrica: val, prev, delta (% ou None se sem histórico).
    """
    from app.database import get_kpi_raw
    raw  = get_kpi_raw(seller_id)
    curr = raw['current']
    prev = raw['previous']

    def _delta(c, p):
        if not p:
            return None
        return round((c - p) / p * 100, 1)

    ticket_c = round(curr['revenue'] / max(1, curr['orders']), 2)
    ticket_p = round(prev['revenue'] / max(1, prev['orders']), 2) if prev['orders'] else 0.0

    return {
        'revenue':    {'val': round(curr['revenue'], 2),   'prev': round(prev['revenue'], 2),   'delta': _delta(curr['revenue'],  prev['revenue'])},
        'units':      {'val': int(curr['units']),           'prev': int(prev['units']),           'delta': _delta(curr['units'],    prev['units'])},
        'orders':     {'val': int(curr['orders']),          'prev': int(prev['orders']),          'delta': _delta(curr['orders'],   prev['orders'])},
        'avg_ticket': {'val': ticket_c,                     'prev': ticket_p,                     'delta': _delta(ticket_c,         ticket_p)},
    }


def compute_margin(seller_id: str, days: int = 30) -> dict:
    """
    Calcula lucro e margem por item no período.

    Fórmulas:
      comissao_ml     = receita_bruta × ml_fee_rate
      frete           = qty × shipping_cost
      receita_liquida = receita_bruta - comissao_ml - frete
      custo_nf        = qty × unit_cost
      imposto         = receita_bruta × tax_rate
      lucro           = receita_liquida - custo_nf - imposto
      margem_pct      = lucro / receita_bruta × 100

    Retorna:
      items          — lista por item com breakdown completo
      totals         — totais agregados (somente itens com custo cadastrado)
      sem_custo      — nº de itens sem custo cadastrado
      negativas      — nº de itens com lucro < 0
      abaixo_10      — nº de itens com 0 ≤ margem < 10%
      prejuizo_total — soma do lucro dos itens negativos
    """
    from app.database import get_margin_data
    rows = get_margin_data(seller_id, days)

    items     = []
    sem_custo = 0

    for r in rows:
        receita_bruta = float(r["receita_bruta"] or 0)
        qty           = int(r["qty"] or 0)

        has_cost      = r["unit_cost"] is not None
        if not has_cost:
            sem_custo += 1

        unit_cost     = float(r["unit_cost"] or 0)
        tax_rate      = float(r["tax_rate"] or 0)
        ml_fee_rate   = float(r["ml_fee_rate"]) if r["ml_fee_rate"] is not None else 0.14
        shipping_cost = float(r["shipping_cost"] or 0)

        comissao_ml     = receita_bruta * ml_fee_rate
        frete           = qty * shipping_cost
        receita_liquida = receita_bruta - comissao_ml - frete
        custo_nf        = qty * unit_cost
        imposto         = receita_bruta * tax_rate
        lucro           = receita_liquida - custo_nf - imposto
        margem_pct      = round(lucro / receita_bruta * 100, 1) if receita_bruta > 0 else None

        items.append({
            "id":               r["id"],
            "title":            r["title"],
            "qty":              qty,
            "receita_bruta":    round(receita_bruta, 2),
            "comissao_ml":      round(comissao_ml, 2),
            "frete":            round(frete, 2),
            "receita_liquida":  round(receita_liquida, 2),
            "custo_nf":         round(custo_nf, 2),
            "imposto":          round(imposto, 2),
            "lucro":            round(lucro, 2),
            "margem_pct":       margem_pct,
            "has_cost":         has_cost,
            "ml_fee_pct":       round(ml_fee_rate * 100, 1),
            "tax_pct":          round(tax_rate * 100, 1),
        })

    # Totais somente de itens com custo cadastrado
    costed = [i for i in items if i["has_cost"]]
    total_rb = round(sum(i["receita_bruta"] for i in costed), 2)
    totals = {
        "receita_bruta":    total_rb,
        "comissao_ml":      round(sum(i["comissao_ml"]     for i in costed), 2),
        "frete":            round(sum(i["frete"]           for i in costed), 2),
        "receita_liquida":  round(sum(i["receita_liquida"] for i in costed), 2),
        "custo_nf":         round(sum(i["custo_nf"]        for i in costed), 2),
        "imposto":          round(sum(i["imposto"]         for i in costed), 2),
        "lucro":            round(sum(i["lucro"]           for i in costed), 2),
        "margem_pct":       None,
    }
    if total_rb > 0:
        totals["margem_pct"] = round(totals["lucro"] / total_rb * 100, 1)

    negativas      = [i for i in costed if i["lucro"] < 0]
    abaixo_10      = [i for i in costed if i["margem_pct"] is not None and 0 <= i["margem_pct"] < 10]
    prejuizo_total = round(sum(i["lucro"] for i in negativas), 2)

    return {
        "breakdown":      items,
        "totals":         totals,
        "sem_custo":      sem_custo,
        "negativas":      len(negativas),
        "abaixo_10":      len(abaixo_10),
        "prejuizo_total": prejuizo_total,
    }


def build_chart_data(seller_id: str) -> dict:
    """
    Dados para Chart.js: vendas diárias últimos 30 dias.
    Dias sem vendas são preenchidos com 0.
    Retorna: {labels: ['MM/DD', ...], revenue: [...], units: [...]}
    """
    from app.database import get_daily_sales
    from datetime import date, timedelta

    rows     = get_daily_sales(seller_id, days=30)
    data_map = {r['day']: r for r in rows}

    today = date.today()
    labels, revenue, units = [], [], []

    for i in range(29, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        r = data_map.get(d, {})
        labels.append(d[5:].replace('-', '/'))
        revenue.append(round(float(r.get('revenue') or 0), 2))
        units.append(int(r.get('units') or 0))

    return {'labels': labels, 'revenue': revenue, 'units': units}


# ─────────────────────────────────────────────────────────────────────────────
# Módulo 7 — Relatório Executivo
# ─────────────────────────────────────────────────────────────────────────────

def compute_executive_report(seller_id: str, days: int = 30) -> dict:
    """
    Constrói o relatório executivo consolidado para o período informado.

    Retorna:
      period_days       — dias do período
      receita_realizada — faturamento real no período
      receita_potencial — realizada + perdas por ruptura
      receita_perdida   — soma das estimativas de ruptura
      margem_media_pct  — margem média ponderada (se custos cadastrados)
      top_receita       — top-5 itens por receita no período
      top_crescimento   — top-5 itens com maior aceleração de giro
      top_risco         — top-5 itens por score de risco
      acoes_prioritarias — lista de ações recomendadas
      ruptura_summary   — {total_items_em_ruptura, total_dias_ruptura}
      margem_summary    — mesma estrutura de compute_margin
    """
    from app.database import get_daily_sales, get_stock_history_batch
    from datetime import date, timedelta

    items = compute_all(seller_id)

    # ── Receita realizada (pedidos no período) ────────────────────────────────
    from app.database import get_conn
    conn = get_conn()
    row = conn.execute("""
        SELECT
            COALESCE(SUM(quantity), 0)         AS total_units,
            COALESCE(SUM(quantity * price), 0) AS total_revenue
        FROM orders
        WHERE seller_id = ? AND status != 'cancelled'
          AND datetime(substr(date_created,1,19)) >= datetime('now', '-' || ? || ' days')
    """, (seller_id, days)).fetchone()
    conn.close()

    receita_realizada = round(float(row["total_revenue"] or 0), 2)
    total_units       = int(row["total_units"] or 0)

    # ── Perdas por ruptura (todos os itens com histórico) ─────────────────────
    history_batch = get_stock_history_batch(seller_id, days=days)
    receita_perdida   = 0.0
    ruptura_items     = 0
    total_ruptura_days = 0

    for item in items:
        if item["giro_30d"] <= GIRO_MIN:
            continue
        h = history_batch.get(item["id"], [])
        if not h:
            continue
        rupture_days = sum(1 for r in h if r["available_qty"] == 0)
        if rupture_days == 0:
            continue
        ruptura_items     += 1
        total_ruptura_days += rupture_days
        active_giro = item["giro_30d"]
        receita_perdida += rupture_days * active_giro * (item["price"] or 0)

    receita_perdida   = round(receita_perdida, 2)
    receita_potencial = round(receita_realizada + receita_perdida, 2)

    # ── Top 5 por receita ─────────────────────────────────────────────────────
    conn = get_conn()
    top_r_rows = conn.execute("""
        SELECT item_id,
               COALESCE(SUM(quantity * price), 0) AS rev,
               COALESCE(SUM(quantity), 0)          AS units
        FROM orders
        WHERE seller_id = ? AND status != 'cancelled'
          AND datetime(substr(date_created,1,19)) >= datetime('now', '-' || ? || ' days')
        GROUP BY item_id
        ORDER BY rev DESC LIMIT 5
    """, (seller_id, days)).fetchall()
    conn.close()

    item_map = {i["id"]: i for i in items}
    top_receita = []
    for r in top_r_rows:
        it = item_map.get(r["item_id"], {})
        top_receita.append({
            "item_id": r["item_id"],
            "title":   it.get("title", r["item_id"]),
            "revenue": round(float(r["rev"]), 2),
            "units":   int(r["units"]),
        })

    # ── Top 5 crescimento (acelerando, maior giro_7d/giro_30d ratio) ─────────
    top_crescimento = sorted(
        [i for i in items if i["giro_30d"] > GIRO_MIN and i["trend"] == "acelerando"],
        key=lambda x: -(x["giro_7d"] / max(x["giro_30d"], 0.01))
    )[:5]
    top_crescimento = [
        {"item_id": i["id"], "title": i["title"],
         "giro_7d": i["giro_7d"], "giro_30d": i["giro_30d"],
         "ratio": round(i["giro_7d"] / max(i["giro_30d"], 0.01), 2)}
        for i in top_crescimento
    ]

    # ── Top 5 risco ───────────────────────────────────────────────────────────
    risk_items = compute_risk_all(seller_id)
    top_risco  = [
        {"item_id": r["id"], "title": r["title"],
         "score": r["risk"]["score"], "level": r["risk"]["level"],
         "days_until_rupture": r["risk"]["days_until_rupture"]}
        for r in risk_items[:5]
    ]

    # ── Margem média ──────────────────────────────────────────────────────────
    margem_summary = {"negativas": 0, "abaixo_10": 0, "sem_custo": 0,
                      "margem_pct": None, "prejuizo_total": 0.0}
    try:
        md = compute_margin(seller_id, days=days)
        margem_summary = {
            "negativas":      md["negativas"],
            "abaixo_10":      md["abaixo_10"],
            "sem_custo":      md["sem_custo"],
            "margem_pct":     md["totals"]["margem_pct"],
            "prejuizo_total": md["prejuizo_total"],
        }
    except Exception:
        pass

    # ── Ações prioritárias ────────────────────────────────────────────────────
    acoes = []

    # Rupturas imediatas
    ruptura_now = [r for r in risk_items if r["risk"]["level"] == "RUPTURA"]
    if ruptura_now:
        acoes.append({
            "prioridade": 1,
            "tipo": "RUPTURA",
            "descricao": f"{len(ruptura_now)} produto(s) em ruptura agora — envio imediato necessário.",
            "itens": [r["title"][:50] for r in ruptura_now[:3]],
        })

    # Críticos (< 10d)
    criticos = [r for r in risk_items if r["risk"]["level"] == "CRÍTICO"]
    if criticos:
        acoes.append({
            "prioridade": 2,
            "tipo": "CRÍTICO",
            "descricao": f"{len(criticos)} produto(s) com menos de 10 dias de estoque.",
            "itens": [r["title"][:50] for r in criticos[:3]],
        })

    # Margem negativa
    if margem_summary["negativas"] > 0:
        acoes.append({
            "prioridade": 3,
            "tipo": "MARGEM",
            "descricao": f"{margem_summary['negativas']} produto(s) com margem negativa — revisar preço ou custo.",
            "itens": [],
        })

    # Sem custo cadastrado
    if margem_summary["sem_custo"] > 0:
        acoes.append({
            "prioridade": 4,
            "tipo": "CUSTO",
            "descricao": f"{margem_summary['sem_custo']} produto(s) sem custo cadastrado — margem não calculada.",
            "itens": [],
        })

    # Receita perdida relevante
    if receita_perdida > 500:
        acoes.append({
            "prioridade": 2,
            "tipo": "RUPTURA_HISTORICA",
            "descricao": f"R$ {receita_perdida:,.2f} estimados em receita perdida por rupturas no período.",
            "itens": [],
        })

    acoes.sort(key=lambda a: a["prioridade"])

    return {
        "period_days":         days,
        "receita_realizada":   receita_realizada,
        "receita_potencial":   receita_potencial,
        "receita_perdida":     receita_perdida,
        "total_units":         total_units,
        "ruptura_summary":     {
            "total_items":  ruptura_items,
            "total_dias":   total_ruptura_days,
        },
        "margem_summary":      margem_summary,
        "top_receita":         top_receita,
        "top_crescimento":     top_crescimento,
        "top_risco":           top_risco,
        "acoes_prioritarias":  acoes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Diagnóstico de conta (margem + representatividade + marca controlada + status)
# ─────────────────────────────────────────────────────────────────────────────

DIAGNOSTICO_MARGEM_MINIMA = 10.0   # % — abaixo disso, produto é candidato a saída
DIAGNOSTICO_JANELA_PADRAO = 90     # dias — janela própria desse módulo, não mexe no
                                    # padrão de 30d usado em compute_margin pras outras telas

_DIAG_SORT_PRIORITY = {
    "descontinuar":        0,
    "saida_planejada":     1,
    "sem_dado_suficiente": 2,
    "fora_regua":          3,
    "manter":              4,
}


def _classify_diagnostico(
    margem_pct: float | None,
    share_pct: float,
    has_cost: bool,
    qty: int,
    marca_controlada: bool,
    cost_source: str,
    threshold_pct: float,
    days: int,
) -> tuple[str, str]:
    """
    Retorna (status, motivo).

    Hierarquia de decisão:
      1. fora_regua           — marca com preço controlado pela indústria (ação é
                                 qualidade de catálogo/anúncio, não pricing)
      2. sem_dado_suficiente  — sem venda na janela, sem custo cadastrado, ou custo
                                 de bonificação pendente de revisão (margem não confiável)
      3. manter               — margem >= mínima aceitável
      4. saida_planejada      — margem abaixo do mínimo mas alta representatividade
                                 no faturamento (vende, não repõe, risco sinalizado)
      5. descontinuar         — margem abaixo do mínimo e baixa representatividade
                                 (vende até zerar estoque, sem repor)
    """
    if marca_controlada:
        return (
            "fora_regua",
            "marca com preço controlado pela indústria — ação é qualidade de "
            "catálogo/anúncio, não pricing",
        )

    if cost_source == "bonificacao":
        return (
            "sem_dado_suficiente",
            "custo de bonificação — revisar antes de classificar por margem",
        )

    if qty == 0:
        return "sem_dado_suficiente", f"sem venda nos últimos {days} dias"

    if not has_cost:
        return "sem_dado_suficiente", "sem custo cadastrado"

    if margem_pct is not None and margem_pct >= DIAGNOSTICO_MARGEM_MINIMA:
        return "manter", f"margem de {margem_pct:.1f}%"

    if share_pct >= threshold_pct:
        return (
            "saida_planejada",
            f"margem de {margem_pct:.1f}% (mínimo {DIAGNOSTICO_MARGEM_MINIMA:.0f}%), mas "
            f"representa {share_pct:.1f}% do faturamento (>= {threshold_pct:.1f}%) — "
            f"vende, não repõe, risco sinalizado",
        )

    return (
        "descontinuar",
        f"margem de {margem_pct:.1f}% (mínimo {DIAGNOSTICO_MARGEM_MINIMA:.0f}%) e "
        f"representatividade de {share_pct:.1f}% (< {threshold_pct:.1f}%) — vende até "
        f"zerar estoque, sem repor",
    )


def compute_product_diagnostics(seller_id: str, days: int = DIAGNOSTICO_JANELA_PADRAO) -> dict:
    """
    Diagnóstico de conta: classifica cada produto em manter / saída planejada /
    descontinuar / fora da régua, combinando margem, representatividade em
    faturamento e marca com preço controlado pela indústria.

    Retorna items (lista com breakdown por produto, ordenada por prioridade de
    ação) e by_status (contagem por categoria).
    """
    from app.database import (
        get_product_diagnostics_data, get_marcas_controladas,
        normalize_marca, get_representatividade_threshold,
    )

    rows = get_product_diagnostics_data(seller_id, days)
    controlled = {m["marca_normalizada"] for m in get_marcas_controladas(seller_id)}
    threshold_pct = get_representatividade_threshold(seller_id)
    total_revenue = sum(float(r["receita_bruta"] or 0) for r in rows)

    items = []
    for r in rows:
        receita_bruta = float(r["receita_bruta"] or 0)
        qty           = int(r["qty"] or 0)

        has_cost      = r["unit_cost"] is not None and float(r["unit_cost"]) > 0
        unit_cost     = float(r["unit_cost"] or 0)
        tax_rate      = float(r["tax_rate"] or 0)
        ml_fee_rate   = float(r["ml_fee_rate"]) if r["ml_fee_rate"] is not None else 0.14
        shipping_cost = float(r["shipping_cost"] or 0)
        marca         = r["marca"] or ""
        cost_source   = r["cost_source"] or "compra"

        comissao_ml     = receita_bruta * ml_fee_rate
        frete           = qty * shipping_cost
        receita_liquida = receita_bruta - comissao_ml - frete
        custo_nf        = qty * unit_cost
        imposto         = receita_bruta * tax_rate
        lucro           = receita_liquida - custo_nf - imposto
        margem_pct      = round(lucro / receita_bruta * 100, 1) if receita_bruta > 0 else None
        share_pct       = round(receita_bruta / total_revenue * 100, 2) if total_revenue > 0 else 0.0
        marca_controlada = normalize_marca(marca) in controlled

        status, motivo = _classify_diagnostico(
            margem_pct, share_pct, has_cost, qty, marca_controlada,
            cost_source, threshold_pct, days,
        )

        items.append({
            "id":               r["id"],
            "title":            r["title"],
            "qty":              qty,
            "receita_bruta":    round(receita_bruta, 2),
            "margem_pct":       margem_pct,
            "share_pct":        share_pct,
            "has_cost":         has_cost,
            "marca":            marca,
            "cost_source":      cost_source,
            "status":           status,
            "motivo":           motivo,
        })

    items.sort(key=lambda it: _DIAG_SORT_PRIORITY.get(it["status"], 9))

    by_status: dict[str, int] = {}
    for it in items:
        by_status[it["status"]] = by_status.get(it["status"], 0) + 1

    return {
        "items":         items,
        "by_status":     by_status,
        "total_revenue": round(total_revenue, 2),
        "days":          days,
        "threshold_pct": threshold_pct,
    }


def summarize_diagnostico_view(rows: list) -> dict:
    """
    KPIs a partir de linhas já persistidas em product_status (via get_product_diagnostics_view).
    Usa o status efetivo (override manual quando presente, senão o computado).

    - weighted_margin_pct: margem ponderada por receita (lucro total / receita total),
      não é média simples entre produtos.
    - risk_pct: % da receita total classificada em saída planejada + descontinuar.
    """
    total_revenue = sum(float(r["receita_bruta"] or 0) for r in rows)

    margin_rows = [r for r in rows if r["margem_pct"] is not None]
    receita_com_margem = sum(float(r["receita_bruta"] or 0) for r in margin_rows)
    lucro_total = sum(
        float(r["margem_pct"]) / 100 * float(r["receita_bruta"] or 0) for r in margin_rows
    )
    weighted_margin_pct = (
        round(lucro_total / receita_com_margem * 100, 1) if receita_com_margem > 0 else None
    )

    by_status: dict[str, int] = {}
    risk_revenue = 0.0
    for r in rows:
        status = r["override_status"] or r["computed_status"]
        by_status[status] = by_status.get(status, 0) + 1
        if status in ("saida_planejada", "descontinuar"):
            risk_revenue += float(r["receita_bruta"] or 0)

    risk_pct = round(risk_revenue / total_revenue * 100, 1) if total_revenue > 0 else 0.0
    computed_at = max((r["computed_at"] for r in rows), default=None)

    return {
        "total_revenue":       round(total_revenue, 2),
        "weighted_margin_pct": weighted_margin_pct,
        "risk_revenue":        round(risk_revenue, 2),
        "risk_pct":            risk_pct,
        "by_status":           by_status,
        "computed_at":         computed_at,
        "total_items":         len(rows),
    }
