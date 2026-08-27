"""
Ads Finance Engine (Etapa 4).

Cruza a camada de Ads com o custo/margem real do SKU (reaproveita product_costs e
a MESMA fórmula de margem de analytics.compute_margin) pra responder:

  - lucro ANTES de Ads (por SKU / ad group / campanha / conta)
  - lucro DEPOIS de Ads  (= lucro antes − custo de Ads do período)
  - ACOS de equilíbrio    (% da receita que o Ads pode consumir até lucro = 0)
  - ROAS de equilíbrio     (piso absoluto, não meta)
  - ROAS mínimo operacional (dado uma margem-alvo, quanto de Ads o alvo suporta)

Órfão/EMPTY: herda o `serving` do Metrics Engine. Grupo não veiculando NÃO
produz números financeiros de zeros — retorna serving=False + campos None.

`margem_alvo_pct` entra por parâmetro; a Etapa 5 (Strategy Engine) passa o valor
de ads_strategy_profile.profit_targets.
"""
from app.database import (
    get_campaign_item_ids,
    get_ad_group_item_ids,
    get_ads_campaign,
    get_orders_agg_for_items,
    get_product_costs_map,
    sum_ad_group_metrics,
    sum_campaign_metrics,
)
from app.services import ads_metrics
from app.utils.logger import get_logger

log = get_logger("ads_finance")

_DEFAULT_ML_FEE_RATE = 0.14  # mesmo default de analytics/compute_margin


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(num, den, nd=2):
    d = _f(den)
    return round(_f(num) / d * 100, nd) if d else None


def _sku_breakdown(receita_bruta, qty, cost_row):
    """
    Lucro antes de Ads pra um SKU no período — fórmula idêntica a
    analytics.compute_margin:
        comissao_ml     = receita_bruta * ml_fee_rate
        frete           = qty * shipping_cost
        receita_liquida = receita_bruta - comissao_ml - frete
        custo_merc      = qty * unit_cost
        imposto         = receita_bruta * tax_rate
        lucro           = receita_liquida - custo_merc - imposto
    """
    rb = _f(receita_bruta)
    q = int(qty or 0)
    unit_cost = _f(cost_row.get("unit_cost"))
    tax_rate = _f(cost_row.get("tax_rate"))
    ml_fee_rate = _f(cost_row["ml_fee_rate"]) if cost_row.get("ml_fee_rate") is not None else _DEFAULT_ML_FEE_RATE
    shipping_cost = _f(cost_row.get("shipping_cost"))

    comissao_ml = rb * ml_fee_rate
    frete = q * shipping_cost
    receita_liquida = rb - comissao_ml - frete
    custo_merc = q * unit_cost
    imposto = rb * tax_rate
    lucro = receita_liquida - custo_merc - imposto
    return {
        "receita_bruta": round(rb, 2),
        "qty": q,
        "comissao_ml": round(comissao_ml, 2),
        "frete": round(frete, 2),
        "custo_mercadoria": round(custo_merc, 2),
        "imposto": round(imposto, 2),
        "receita_liquida": round(receita_liquida, 2),
        "lucro_antes_ads": round(lucro, 2),
        "margem_antes_ads_pct": _pct(lucro, rb),
    }


def _finance_from_items(seller_id, item_ids, date_from, date_to, ads_cost,
                        ads_revenue, margem_alvo_pct):
    """Núcleo compartilhado: agrega margem dos SKUs + deriva break-even/ROAS mínimo."""
    orders = get_orders_agg_for_items(seller_id, item_ids, date_from, date_to)
    costs = get_product_costs_map(seller_id, item_ids)

    by_sku = {}
    lucro_antes = 0.0
    receita_costed = 0.0
    itens_sem_custo = []
    for item_id in item_ids:
        oi = orders["by_item"].get(item_id)
        if not oi or oi["qty"] == 0:
            continue
        if item_id not in costs:
            itens_sem_custo.append(item_id)
            continue
        bd = _sku_breakdown(oi["receita_bruta"], oi["qty"], costs[item_id])
        by_sku[item_id] = bd
        lucro_antes += bd["lucro_antes_ads"]
        receita_costed += bd["receita_bruta"]

    lucro_antes = round(lucro_antes, 2)
    ads_cost = round(_f(ads_cost), 2)
    lucro_depois = round(lucro_antes - ads_cost, 2)
    receita_real = orders["total"]["receita_bruta"]

    margem_antes_pct = _pct(lucro_antes, receita_costed)
    # ACOS de equilíbrio = % da receita que o Ads consome até zerar o lucro.
    # Sobre a receita com custo conhecido (a única base defensável).
    acos_equilibrio_pct = margem_antes_pct
    roas_equilibrio = round(100 / margem_antes_pct, 2) if margem_antes_pct and margem_antes_pct > 0 else None

    roas_minimo_operacional = None
    margem_alvo_inatingivel = None
    if margem_alvo_pct is not None and margem_antes_pct is not None:
        folga_pct = margem_antes_pct - margem_alvo_pct
        if folga_pct <= 0:
            margem_alvo_inatingivel = True
        else:
            roas_minimo_operacional = round(100 / folga_pct, 2)
            margem_alvo_inatingivel = False

    return {
        "receita_real": round(receita_real, 2),
        "receita_com_custo_conhecido": round(receita_costed, 2),
        "ads_cost": ads_cost,
        "ads_revenue_atribuida": round(_f(ads_revenue), 2),
        "lucro_antes_ads": lucro_antes,
        "lucro_depois_ads": lucro_depois,
        "margem_antes_ads_pct": margem_antes_pct,
        "margem_depois_ads_pct": _pct(lucro_depois, receita_costed),
        "acos_equilibrio_pct": acos_equilibrio_pct,
        "roas_equilibrio": roas_equilibrio,
        "margem_alvo_pct": margem_alvo_pct,
        "roas_minimo_operacional": roas_minimo_operacional,
        "margem_alvo_inatingivel": margem_alvo_inatingivel,
        "custo_incompleto": bool(itens_sem_custo),
        "itens_sem_custo": itens_sem_custo,
        "n_itens": len(item_ids),
        "n_itens_com_venda_e_custo": len(by_sku),
        "by_sku": by_sku,
    }


# ── SKU ────────────────────────────────────────────────────────────────────

def sku_profit(seller_id, item_id, date_from, date_to):
    """Lucro antes de Ads de um único SKU no período (só vendas reais + custo)."""
    orders = get_orders_agg_for_items(seller_id, [item_id], date_from, date_to)
    oi = orders["by_item"].get(item_id, {"qty": 0, "receita_bruta": 0.0})
    costs = get_product_costs_map(seller_id, [item_id])
    if item_id not in costs:
        return {"item_id": item_id, "has_cost": False,
                "qty": oi["qty"], "receita_bruta": oi["receita_bruta"],
                "lucro_antes_ads": None, "margem_antes_ads_pct": None}
    bd = _sku_breakdown(oi["receita_bruta"], oi["qty"], costs[item_id])
    return {"item_id": item_id, "has_cost": True, **bd}


# ── Ad group ───────────────────────────────────────────────────────────────

def ad_group_finance(seller_id, ad_group_id, date_from, date_to, *,
                     margem_alvo_pct=None, since_last_change=False):
    m = ads_metrics.ad_group_metrics(seller_id, ad_group_id, date_from, date_to,
                                     since_last_change=since_last_change,
                                     include_series=False)
    if not m.get("found"):
        return {"found": False, "ad_group_id": ad_group_id}

    base = {
        "found": True, "scope": "ad_group", "ad_group_id": ad_group_id,
        "campaign_id": m["campaign_id"], "ad_group_type": m["ad_group_type"],
        "is_scaffold": m["is_scaffold"], "sku_level": m["sku_level"],
        "window": m["window"], "serving": m["serving"],
        "status": m["status"], "not_serving_reason": m["not_serving_reason"],
    }
    if not m["serving"]:
        return {**base, "finance": None}

    win = m["window"]
    agg = sum_ad_group_metrics(seller_id, ad_group_id,
                               win["effective_from"], win["effective_to"])
    item_ids = get_ad_group_item_ids(seller_id, ad_group_id)
    fin = _finance_from_items(
        seller_id, item_ids, win["effective_from"], win["effective_to"],
        ads_cost=agg.get("cost"), ads_revenue=agg.get("total_amount"),
        margem_alvo_pct=margem_alvo_pct)
    return {**base, "finance": fin}


# ── Campanha ───────────────────────────────────────────────────────────────

def campaign_finance(seller_id, campaign_id, date_from, date_to, *,
                     margem_alvo_pct=None, since_last_change=False):
    m = ads_metrics.campaign_metrics(seller_id, campaign_id, date_from, date_to,
                                     since_last_change=since_last_change,
                                     include_series=False)
    if not m.get("found"):
        return {"found": False, "campaign_id": campaign_id}

    base = {
        "found": True, "scope": "campaign", "campaign_id": campaign_id,
        "name": m["name"], "status_ml": m["status_ml"],
        "acos_target": m["acos_target"], "roas_target": m["roas_target"],
        "window": m["window"], "serving": m["serving"],
        "status": m["status"], "not_serving_reason": m["not_serving_reason"],
    }
    if not m["serving"]:
        return {**base, "finance": None}

    win = m["window"]
    agg = sum_campaign_metrics(seller_id, campaign_id,
                               win["effective_from"], win["effective_to"])
    item_ids = get_campaign_item_ids(seller_id, campaign_id)
    fin = _finance_from_items(
        seller_id, item_ids, win["effective_from"], win["effective_to"],
        ads_cost=agg.get("cost"), ads_revenue=agg.get("total_amount"),
        margem_alvo_pct=margem_alvo_pct)
    return {**base, "finance": fin}


# ── Conta (roll-up dos 3 níveis) ─────────────────────────────────────────

def account_finance(seller_id, date_from, date_to, *, margem_alvo_pct=None):
    """
    Consolida a conta: soma custo/receita de Ads de todas as campanhas + margem
    real dos itens DISTINTOS veiculados (sem dupla contagem entre campanhas).
    """
    from app.database import get_conn
    conn = get_conn()
    camp_ids = [r["campaign_id_ml"] for r in conn.execute(
        "SELECT campaign_id_ml FROM campaigns WHERE seller_id = ?", (seller_id,)).fetchall()]
    conn.close()

    ads_cost = 0.0
    ads_rev = 0.0
    all_items = set()
    campanhas = []
    for cid in camp_ids:
        agg = sum_campaign_metrics(seller_id, cid, date_from, date_to)
        ads_cost += _f(agg.get("cost"))
        ads_rev += _f(agg.get("total_amount"))
        all_items.update(get_campaign_item_ids(seller_id, cid))
        campanhas.append({"campaign_id": cid,
                          "cost": round(_f(agg.get("cost")), 2),
                          "total_amount": round(_f(agg.get("total_amount")), 2),
                          "days_with_prints": int(_f(agg.get("days_with_prints")))})

    fin = _finance_from_items(seller_id, sorted(all_items), date_from, date_to,
                              ads_cost=ads_cost, ads_revenue=ads_rev,
                              margem_alvo_pct=margem_alvo_pct)
    fin.pop("by_sku", None)  # conta: resumo, não SKU a SKU
    return {
        "scope": "account", "seller_id": seller_id,
        "window": {"effective_from": date_from, "effective_to": date_to},
        "n_campanhas": len(camp_ids),
        "campanhas": campanhas,
        "finance": fin,
    }
