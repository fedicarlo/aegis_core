"""
Ads Metrics Engine (Etapa 4).

Consolida a série diária persistida (a API do ML só entrega diário) em funil de
período, recalcula os ratios a partir dos agregados (nunca faz média de ratio),
cruza com vendas REAIS (tabela orders) pra receita orgânica e TACOS, e expõe a
régua de "amostra suficiente" que o Diagnostic Engine (Etapa 6) usa como gate.

Tratamento de ad group órfão/EMPTY: explícito em toda função. Um grupo
`is_scaffold` (fora de campanha ou status EMPTY) ou sem nenhuma impressão no
período NUNCA retorna funil de zeros — retorna `serving=False` + `status` +
`funnel=None`. O Diagnostic Engine só precisa checar `serving`.
"""
from app.database import (
    get_ads_ad_group,
    get_ads_campaign,
    get_campaign_daily_series,
    get_campaign_impression_share_avg,
    get_campaign_item_ids,
    get_ad_group_daily_series,
    get_ad_group_item_ids,
    get_last_campaign_change_date,
    get_orders_agg_for_items,
    sum_ad_group_metrics,
    sum_campaign_metrics,
)
from app.services import ads_strategy
from app.utils.logger import get_logger

log = get_logger("ads_metrics")

_STATUS_SERVING = "VEICULANDO"
_STATUS_NOT_SERVING = "NAO_VEICULANDO"

# Motivos de "não veiculando" (estáveis — o Diagnostic Engine referencia).
REASON_SCAFFOLD = "orfao_ou_empty"          # is_scaffold: fora de campanha ou status EMPTY
REASON_NO_PRINTS = "sem_impressao_no_periodo"


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _ratio(num, den, *, pct=False, nd=2):
    n, d = _f(num), _f(den)
    if d == 0:
        return None
    return round(n / d * (100 if pct else 1), nd)


def _recompute_ratios(agg):
    """Ratios do período a partir dos agregados somados (consistente, não média)."""
    clicks = _f(agg.get("clicks"))
    prints = _f(agg.get("prints"))
    cost = _f(agg.get("cost"))
    total_amount = _f(agg.get("total_amount"))
    units = _f(agg.get("units_quantity"))
    return {
        "ctr": _ratio(clicks, prints, pct=True),
        "cpc": _ratio(cost, clicks),
        "acos": _ratio(cost, total_amount, pct=True),
        "roas": _ratio(total_amount, cost),
        "cvr": _ratio(units, clicks, pct=True),
    }


def _funnel(agg, orders_total):
    """Funil de período: impressão -> clique -> venda -> receita, + comparação real."""
    ads_revenue = round(_f(agg.get("total_amount")), 2)
    real_revenue = _f(orders_total.get("receita_bruta"))
    organic_revenue = round(max(real_revenue - ads_revenue, 0.0), 2)
    # Quando a receita atribuída ao Ads passa da receita real dos itens do alvo,
    # o ML está creditando venda assistida de OUTROS produtos (clique num item ->
    # compra de outro). organic_revenue vira 0 e o cruzamento fica "apertado" —
    # o Diagnostic Engine precisa saber disso (é sinal, não erro).
    attribution_exceeds_real = ads_revenue > real_revenue and real_revenue > 0
    return {
        "prints": int(_f(agg.get("prints"))),
        "clicks": int(_f(agg.get("clicks"))),
        "cost": round(_f(agg.get("cost")), 2),
        **_recompute_ratios(agg),
        "ads_units": int(_f(agg.get("units_quantity"))),
        "ads_units_direct": int(_f(agg.get("direct_units_quantity"))),
        "ads_units_indirect": int(_f(agg.get("indirect_units_quantity"))),
        "ads_items": int(_f(agg.get("direct_items_quantity")) + _f(agg.get("indirect_items_quantity"))),
        "ads_revenue": ads_revenue,
        "ads_revenue_direct": round(_f(agg.get("direct_amount")), 2),
        "ads_revenue_indirect": round(_f(agg.get("indirect_amount")), 2),
        "organic_units_ml": int(_f(agg.get("organic_units_quantity"))),
        # cruzamento com vendas reais (orders)
        "real_revenue": round(real_revenue, 2),
        "real_units": int(orders_total.get("qty") or 0),
        "real_orders": int(orders_total.get("orders_count") or 0),
        "organic_revenue": organic_revenue,
        "organic_share_pct": _ratio(organic_revenue, real_revenue, pct=True),
        "tacos": _ratio(agg.get("cost"), real_revenue, pct=True),
        "attribution_exceeds_real": attribution_exceeds_real,
        "days_with_prints": int(_f(agg.get("days_with_prints"))),
    }


def _window(date_from, date_to, campaign_id, since_last_change):
    """Resolve a janela efetiva. since_last_change corta em max(date_from, últ. alteração)."""
    eff_from = date_from
    clamped = False
    last_change = None
    if since_last_change and campaign_id is not None:
        last_change = get_last_campaign_change_date(campaign_id)
        if last_change and last_change > date_from:
            eff_from = last_change
            clamped = True
    return {
        "requested_from": date_from, "requested_to": date_to,
        "effective_from": eff_from, "effective_to": date_to,
        "clamped_by_last_change": clamped, "last_change_date": last_change,
    }


# ── Campanha ────────────────────────────────────────────────────────────────

def campaign_metrics(seller_id, campaign_id, date_from, date_to, *,
                     since_last_change=False, include_series=True):
    camp = get_ads_campaign(seller_id, campaign_id)
    if not camp:
        return {"found": False, "campaign_id": campaign_id}

    win = _window(date_from, date_to, campaign_id, since_last_change)
    d_from, d_to = win["effective_from"], win["effective_to"]

    agg = sum_campaign_metrics(seller_id, campaign_id, d_from, d_to)
    item_ids = get_campaign_item_ids(seller_id, campaign_id)
    orders = get_orders_agg_for_items(seller_id, item_ids, d_from, d_to)

    serving = _f(agg.get("days_with_prints")) > 0
    out = {
        "found": True, "scope": "campaign", "campaign_id": campaign_id,
        "name": camp.get("name"), "status_ml": camp.get("status"),
        "strategy": camp.get("strategy"), "budget": camp.get("budget"),
        "acos_target": camp.get("acos_target"), "roas_target": camp.get("roas_target"),
        "currency_id": camp.get("currency_id"),
        "window": win,
        "serving": serving,
        "status": _STATUS_SERVING if serving else _STATUS_NOT_SERVING,
        "not_serving_reason": None if serving else REASON_NO_PRINTS,
        "n_items": len(item_ids),
        "funnel": None,
        "impression_share": None,
        "daily_series": [],
    }
    if not serving:
        return out

    out["funnel"] = _funnel(agg, orders["total"])
    ish = get_campaign_impression_share_avg(seller_id, campaign_id, d_from, d_to)
    out["impression_share"] = {k: (round(v, 4) if v is not None else None)
                               for k, v in ish.items()} or None
    if include_series:
        out["daily_series"] = _normalize_campaign_series(
            get_campaign_daily_series(seller_id, campaign_id, d_from, d_to))
    return out


def _normalize_campaign_series(rows):
    out = []
    for r in rows:
        out.append({
            "date": r["date"],
            "prints": r["prints"], "clicks": r["clicks"], "cost": r["cost"],
            "ctr": r["ctr"], "cpc": r["cpc"], "acos": r["acos"], "roas": r["roas"],
            "cvr": r["cvr"], "sov": r["sov"],
            "units_quantity": r["units_quantity"], "total_amount": r["total_amount"],
            "organic_units_quantity": r["organic_units_quantity"],
            "impression_share": r["impression_share"],
            "top_impression_share": r["top_impression_share"],
            "lost_impression_share_by_budget": r["lost_impression_share_by_budget"],
            "lost_impression_share_by_ad_rank": r["lost_impression_share_by_ad_rank"],
            "acos_benchmark": r["acos_benchmark"],
        })
    return out


# ── Ad group ───────────────────────────────────────────────────────────────

def ad_group_metrics(seller_id, ad_group_id, date_from, date_to, *,
                     since_last_change=False, include_series=True):
    ag = get_ads_ad_group(seller_id, ad_group_id)
    if not ag:
        return {"found": False, "ad_group_id": ad_group_id}

    campaign_id = ag.get("campaign_id")
    win = _window(date_from, date_to, campaign_id, since_last_change)
    d_from, d_to = win["effective_from"], win["effective_to"]

    is_scaffold = bool(ag.get("is_scaffold"))
    agg = sum_ad_group_metrics(seller_id, ad_group_id, d_from, d_to)
    item_ids = get_ad_group_item_ids(seller_id, ad_group_id)
    orders = get_orders_agg_for_items(seller_id, item_ids, d_from, d_to)

    if is_scaffold:
        serving, reason = False, REASON_SCAFFOLD
    elif _f(agg.get("days_with_prints")) == 0:
        serving, reason = False, REASON_NO_PRINTS
    else:
        serving, reason = True, None

    out = {
        "found": True, "scope": "ad_group", "ad_group_id": ad_group_id,
        "campaign_id": campaign_id, "ad_group_type": ag.get("ad_group_type"),
        "ad_group_external_id": ag.get("ad_group_external_id"),
        "title": ag.get("title"), "status_ml": ag.get("status"),
        "is_scaffold": is_scaffold, "tags": ag.get("tags"),
        "date_created_ml": ag.get("date_created_ml"),
        "window": win,
        "serving": serving,
        "status": _STATUS_SERVING if serving else _STATUS_NOT_SERVING,
        "not_serving_reason": reason,
        "n_items": len(item_ids), "item_ids": item_ids,
        "sku_level": ag.get("ad_group_type") == "ITEM",  # métrica == SKU só p/ ITEM
        "funnel": None,
        "daily_series": [],
    }
    if not serving:
        return out

    out["funnel"] = _funnel(agg, orders["total"])
    if include_series:
        out["daily_series"] = get_ad_group_daily_series(
            seller_id, ad_group_id, d_from, d_to)
    return out


# ── Régua de amostra suficiente (gate do Diagnostic Engine) ───────────────

def sample_sufficiency(seller_id, scope, target_id, date_from, date_to, *,
                       rules=None, since_last_change=False):
    """
    scope: 'campaign' | 'ad_group'. Retorna dict com os números crus + veredito.
    NUNCA diz "suficiente" olhando só unidades atribuídas: cruza com nº de
    pedidos reais, compradores únicos (quando disponível) e concentração num
    único pedido (o caso "16 unidades de 1 pedido só").

    As regras vêm do Strategy Profile (ads_strategy_profile.minimum_sample_rules,
    override por seller sobre o global). `rules` (dict parcial) sobrepõe pontualmente.
    """
    rules = {**ads_strategy.minimum_sample_rules(seller_id), **(rules or {})}

    if scope == "campaign":
        m = campaign_metrics(seller_id, target_id, date_from, date_to,
                             since_last_change=since_last_change, include_series=False)
        item_ids_fn = get_campaign_item_ids
    elif scope == "ad_group":
        m = ad_group_metrics(seller_id, target_id, date_from, date_to,
                             since_last_change=since_last_change, include_series=False)
        item_ids_fn = get_ad_group_item_ids
    else:
        raise ValueError(f"scope inválido: {scope}")

    if not m.get("found"):
        return {"found": False, "scope": scope, "target_id": target_id}

    win = m["window"]
    base = {
        "found": True, "scope": scope, "target_id": target_id,
        "window": win, "serving": m["serving"],
    }

    if not m["serving"]:
        return {**base, "suficiente": False,
                "motivo": ["nao_veiculando"],
                "not_serving_reason": m["not_serving_reason"],
                "numeros": None}

    item_ids = item_ids_fn(seller_id, target_id)
    orders = get_orders_agg_for_items(
        seller_id, item_ids, win["effective_from"], win["effective_to"])["total"]
    f = m["funnel"]

    real_units = orders["qty"]
    dominance = _ratio(orders["max_single_order_qty"], real_units, pct=True) or 0.0
    outlier = dominance >= rules["single_order_dominance_pct"] and orders["orders_count"] > 0

    numeros = {
        "ads_units": f["ads_units"],
        "real_units": real_units,
        "real_orders": orders["orders_count"],
        "unique_buyers": orders["unique_buyers"] if orders["buyer_id_disponivel"] else None,
        "buyer_id_disponivel": orders["buyer_id_disponivel"],
        "days_with_prints": f["days_with_prints"],
        "max_single_order_qty": orders["max_single_order_qty"],
        "single_order_dominance_pct": dominance,
    }

    falhas = []
    if real_units < rules["min_units"]:
        falhas.append("poucas_unidades")
    if orders["orders_count"] < rules["min_orders"]:
        falhas.append("poucos_pedidos")
    if f["days_with_prints"] < rules["min_days_with_prints"]:
        falhas.append("serie_curta")
    if outlier:
        falhas.append("dominado_por_1_pedido")

    out = {
        **base,
        "suficiente": len(falhas) == 0,
        "motivo": falhas,
        "regras_usadas": rules,
        "numeros": numeros,
    }
    if not orders["buyer_id_disponivel"]:
        out["caveat"] = ("buyer_id ainda não coletado nos orders — compradores "
                         "únicos indisponível; régua usa nº de pedidos como proxy")
    return out
