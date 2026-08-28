"""
Ads Alert Engine (Etapa 7).

Roda pós-coleta. Compara uma janela RECENTE com um BASELINE imediatamente anterior
(dias configuráveis no risk_limits do Strategy Profile) + cruza com o break-even
do Finance Engine. Cada alerta carrega: o que aconteceu + evidência (números) +
severidade + ação sugerida. Persiste em ads_alerts com dedup por
tipo+alvo+semana (o job diário não repete o mesmo alerta na mesma semana).

Órfão/EMPTY: herda serving do Metrics Engine. Grupo que não veicula nem no
recente nem no baseline é pulado (sem alerta). Grupo que vendia no baseline e
parou no recente gera o alerta 'parou_de_vender'.
"""
from datetime import date, datetime, timedelta, timezone

from app.database import (
    get_ads_campaign,
    get_all_accounts,
    get_campaign_daily_series,
    get_campaign_item_ids,
    get_ad_group_item_ids,
    get_orders_agg_for_items,
    resolve_ads_alert,
    save_ads_alert,
)
from app.services import ads_finance, ads_metrics, ads_strategy
from app.utils.logger import get_logger

log = get_logger("ads_alerts")

SEV_INFO, SEV_ATENCAO, SEV_CRITICO = "info", "atencao", "critico"

_ML_TZ_OFFSET_H = -3


def _today_ml():
    return (datetime.now(timezone.utc) + timedelta(hours=_ML_TZ_OFFSET_H)).date()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _windows(ref_date, recent_days, baseline_days):
    """(recent_from, recent_to, base_from, base_to). `to` = último dia fechado."""
    last_closed = ref_date - timedelta(days=1)
    r_from = last_closed - timedelta(days=recent_days - 1)
    b_to = r_from - timedelta(days=1)
    b_from = b_to - timedelta(days=baseline_days - 1)
    return (r_from.isoformat(), last_closed.isoformat(),
            b_from.isoformat(), b_to.isoformat())


def _iso_week(d_iso):
    y, w, _ = date.fromisoformat(d_iso).isocalendar()
    return f"{y}W{w:02d}"


# ── Detectores ─────────────────────────────────────────────────────────────

def _alert(tipo, sev, evidencia, acao):
    return {"tipo": tipo, "severidade": sev, "evidencia": evidencia, "acao_sugerida": acao}


def _detect(scope, target_id, recent, baseline, fin, series_recent, rl):
    """recent/baseline: funnel dicts (ou None). fin: finance dict (ou {}). Retorna lista."""
    out = []
    rf = recent
    bf = baseline

    # 1. CPC disparou
    if rf and bf and _f(bf.get("cpc")) and _f(rf.get("cpc")):
        if rf["cpc"] > rl["cpc_spike_factor"] * bf["cpc"] and (rf.get("clicks") or 0) >= 20:
            sev = SEV_CRITICO if rf["cpc"] > 1.5 * rl["cpc_spike_factor"] * bf["cpc"] else SEV_ATENCAO
            out.append(_alert("cpc_disparou", sev,
                              {"cpc_recente": rf["cpc"], "cpc_baseline": bf["cpc"],
                               "fator": round(rf["cpc"] / bf["cpc"], 2), "limiar": rl["cpc_spike_factor"]},
                              "Investigar concorrência de lance / mudança de meta. CPC subiu bem acima do histórico."))

    # 2. Investimento acelerou (custo/dia)
    if rf and bf:
        r_days = max(rf.get("days_with_prints") or 0, 1)
        b_days = max(bf.get("days_with_prints") or 0, 1)
        r_pace = _f(rf.get("cost")) / r_days
        b_pace = _f(bf.get("cost")) / b_days
        if b_pace and r_pace > rl["spend_pace_factor"] * b_pace:
            out.append(_alert("investimento_acelerou", SEV_ATENCAO,
                              {"custo_dia_recente": round(r_pace, 2), "custo_dia_baseline": round(b_pace, 2),
                               "fator": round(r_pace / b_pace, 2), "limiar": rl["spend_pace_factor"]},
                              "Confirmar se o aumento de investimento é intencional e se o retorno acompanhou."))

    # 3. Cliques sem venda (janela recente)
    if rf and (rf.get("clicks") or 0) >= rl["clicks_sem_venda"] and (rf.get("ads_units") or 0) == 0:
        out.append(_alert("cliques_sem_venda", SEV_ATENCAO,
                          {"cliques_recentes": rf["clicks"], "vendas_atribuidas": 0,
                           "limiar_cliques": rl["clicks_sem_venda"], "janela_dias": rl["alert_recent_days"]},
                          "Gargalo de conversão: investigar página, oferta, preço, logística. Ver Caso C no diagnóstico."))

    # 4. ROAS abaixo do break-even
    if rf and _f(rf.get("roas")) is not None and _f(fin.get("roas_equilibrio")) is not None:
        if rf["roas"] < fin["roas_equilibrio"]:
            out.append(_alert("roas_abaixo_break_even", SEV_CRITICO,
                              {"roas_recente": rf["roas"], "roas_equilibrio": fin["roas_equilibrio"],
                               "margem_depois_ads_pct": fin.get("margem_depois_ads_pct")},
                              "A venda atribuída está no prejuízo. Reduzir lance/verba ou pausar até reavaliar o SKU."))

    # 5. ACOS acima do teto configurado
    if rf and rl.get("max_acos_pct") is not None and _f(rf.get("acos")) is not None:
        if rf["acos"] > rl["max_acos_pct"]:
            out.append(_alert("acos_acima_do_limite", SEV_ATENCAO,
                              {"acos_recente": rf["acos"], "teto": rl["max_acos_pct"]},
                              "ACOS acima do teto da operação. Rever meta de ROAS/lance."))

    # 6. Batendo teto de orçamento (só campanha — impression share é campaign-level).
    #    O alerta 7 (perda por orçamento subiu vs. baseline) é adicionado depois,
    #    em _augment_budget_delta (precisa da série do baseline).
    if scope == "campaign" and series_recent:
        com_prints = [r for r in series_recent if (r.get("prints") or 0) > 0]
        capped = [r for r in com_prints if (_f(r.get("lost_impression_share_by_budget")) or 0) > 0]
        if com_prints:
            share = len(capped) / len(com_prints) * 100
            if share >= rl["budget_cap_share_pct"]:
                out.append(_alert("batendo_teto_orcamento", SEV_ATENCAO,
                                  {"dias_no_teto": len(capped), "dias_com_veiculacao": len(com_prints),
                                   "pct": round(share, 1), "limiar_pct": rl["budget_cap_share_pct"]},
                                  "Campanha limitada por orçamento na maior parte dos dias. Avaliar aumento de verba."))

    # 8. CTR despencou
    if rf and bf and _f(bf.get("ctr")) and _f(rf.get("ctr")) is not None:
        if bf["ctr"] > 0 and rf["ctr"] < rl["ctr_drop_factor"] * bf["ctr"] and (rf.get("prints") or 0) >= 1000:
            out.append(_alert("ctr_despencou", SEV_ATENCAO,
                              {"ctr_recente": rf["ctr"], "ctr_baseline": bf["ctr"],
                               "queda_pct": round((1 - rf["ctr"] / bf["ctr"]) * 100, 1)},
                              "Investigar capa, preço relativo, promoção, título, prova social. Ver Caso B."))

    # 9. Conversão caiu
    if rf and bf and _f(bf.get("cvr")) and _f(rf.get("cvr")) is not None:
        if (bf["cvr"] > 0 and rf["cvr"] < rl["cvr_drop_factor"] * bf["cvr"]
                and (rf.get("clicks") or 0) >= 30 and (bf.get("clicks") or 0) >= 30):
            out.append(_alert("conversao_caiu", SEV_ATENCAO,
                              {"cvr_recente": rf["cvr"], "cvr_baseline": bf["cvr"],
                               "queda_pct": round((1 - rf["cvr"] / bf["cvr"]) * 100, 1)},
                              "Investigar página, oferta, preço final, logística, avaliações. Ver Caso C."))

    return out


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


# ── Orquestração ───────────────────────────────────────────────────────────

def run_alerts_for_target(seller_id, scope, target_id, *, ref_date=None, persist=True):
    ref_date = ref_date or _today_ml()
    rl = ads_strategy.risk_limits(seller_id)
    r_from, r_to, b_from, b_to = _windows(ref_date, rl["alert_recent_days"], rl["alert_baseline_days"])

    mfn = ads_metrics.campaign_metrics if scope == "campaign" else ads_metrics.ad_group_metrics
    ffn = ads_finance.campaign_finance if scope == "campaign" else ads_finance.ad_group_finance

    m_recent = mfn(seller_id, target_id, r_from, r_to, include_series=False)
    m_base = mfn(seller_id, target_id, b_from, b_to, include_series=False)
    if not m_recent.get("found"):
        return {"found": False, "scope": scope, "target_id": target_id}

    recent_f = m_recent.get("funnel")
    base_f = m_base.get("funnel")

    result = {"found": True, "scope": scope, "target_id": target_id,
              "recent": {"from": r_from, "to": r_to}, "baseline": {"from": b_from, "to": b_to},
              "serving_recent": m_recent["serving"], "alerts": []}

    alerts = []

    # parou_de_vender: vendia no baseline, zero no recente
    prev_units = (base_f or {}).get("ads_units", 0) if m_base.get("serving") else 0
    now_units = (recent_f or {}).get("ads_units", 0) if m_recent["serving"] else 0
    if prev_units >= rl["parou_de_vender_min_prev_units"] and now_units == 0:
        alerts.append(_alert("parou_de_vender", SEV_CRITICO,
                             {"unidades_baseline": prev_units, "unidades_recentes": 0,
                              "janela_recente_dias": rl["alert_recent_days"]},
                             "Produto que vendia via Ads parou. Verificar estoque, status do anúncio, preço, "
                             "buy box e concorrência."))

    if not m_recent["serving"]:
        # sem veiculação recente -> só o alerta acima (se aplicável)
        result["alerts"] = alerts
        result["skipped_reason"] = None if alerts else "nao_veiculando_no_recente"
        if persist:
            _persist(seller_id, scope, target_id, alerts, r_to)
        return result

    fin_full = ffn(seller_id, target_id, r_from, r_to)
    fin = fin_full.get("finance") or {}
    series_recent = (get_campaign_daily_series(seller_id, target_id, r_from, r_to)
                     if scope == "campaign" else [])

    alerts += _detect(scope, target_id, recent_f, base_f, fin, series_recent, rl)

    # 6b (perda por orçamento subiu) precisa do baseline lost_by_budget — calcula aqui
    if scope == "campaign":
        alerts = _augment_budget_delta(seller_id, target_id, alerts, recent_f, series_recent,
                                       b_from, b_to, rl)

    # 11. Outlier distorcendo ROAS (venda excepcional)
    item_ids = (get_campaign_item_ids(seller_id, target_id) if scope == "campaign"
                else get_ad_group_item_ids(seller_id, target_id))
    ot = get_orders_agg_for_items(seller_id, item_ids, r_from, r_to)["total"]
    if ot["qty"] > 0 and (recent_f or {}).get("ads_units", 0) > 0:
        dom = ot["max_single_order_qty"] / ot["qty"] * 100
        if dom >= rl["outlier_roas_dominance_pct"]:
            alerts.append(_alert("outlier_distorcendo_roas", SEV_INFO,
                                 {"maior_pedido_un": ot["max_single_order_qty"], "unidades_reais": ot["qty"],
                                  "concentracao_pct": round(dom, 1), "roas_recente": recent_f.get("roas")},
                                 "Um único pedido concentra a maior parte das vendas do período — o ROAS/CVR "
                                 "recente pode estar distorcido. Confirmar antes de decidir escala."))

    result["alerts"] = alerts
    if persist:
        _persist(seller_id, scope, target_id, alerts, r_to)
    return result


def _augment_budget_delta(seller_id, target_id, alerts, recent_f, series_recent, b_from, b_to, rl):
    com_prints = [r for r in series_recent if (r.get("prints") or 0) > 0]
    if not com_prints:
        return alerts
    r_lost = _avg([_f(r.get("lost_impression_share_by_budget")) for r in com_prints]) * 100
    base_series = get_campaign_daily_series(seller_id, target_id, b_from, b_to)
    base_cp = [r for r in base_series if (r.get("prints") or 0) > 0]
    if not base_cp:
        return alerts
    b_lost = _avg([_f(r.get("lost_impression_share_by_budget")) for r in base_cp]) * 100
    if (r_lost - b_lost) >= rl["lost_by_budget_delta_pp"] and r_lost >= rl["lost_by_budget_alert_pct"]:
        if not any(a["tipo"] == "perda_por_orcamento_subiu" for a in alerts):
            alerts.append(_alert("perda_por_orcamento_subiu", SEV_ATENCAO,
                                 {"perda_recente_pct": round(r_lost, 1), "perda_baseline_pct": round(b_lost, 1),
                                  "delta_pp": round(r_lost - b_lost, 1),
                                  "limiar_delta_pp": rl["lost_by_budget_delta_pp"]},
                                 "A perda de exposição por orçamento aumentou vs. o histórico. Rever verba."))
    return alerts


def _persist(seller_id, scope, target_id, alerts, recent_to):
    wk = _iso_week(recent_to)
    for a in alerts:
        save_ads_alert(seller_id, scope, target_id, a["tipo"], a["severidade"],
                       evidencia=a["evidencia"], acao_sugerida=a["acao_sugerida"],
                       dedup_key=f"{a['tipo']}|{scope}|{target_id}|{wk}")


def run_alerts(seller_id, *, include_ad_groups=False, ref_date=None):
    from app.database import get_conn
    conn = get_conn()
    camp_ids = [r["campaign_id_ml"] for r in conn.execute(
        "SELECT campaign_id_ml FROM campaigns WHERE seller_id = ?", (seller_id,)).fetchall()]
    ag_ids = []
    if include_ad_groups:
        ag_ids = [r["ad_group_id_ml"] for r in conn.execute(
            "SELECT ad_group_id_ml FROM ad_groups WHERE seller_id = ? AND is_scaffold = 0",
            (seller_id,)).fetchall()]
    conn.close()

    total = 0
    for cid in camp_ids:
        r = run_alerts_for_target(seller_id, "campaign", cid, ref_date=ref_date)
        total += len(r.get("alerts", []))
    for agid in ag_ids:
        r = run_alerts_for_target(seller_id, "ad_group", agid, ref_date=ref_date)
        total += len(r.get("alerts", []))
    log.info(f"[{seller_id}] alertas: {len(camp_ids)} campanha(s), {len(ag_ids)} ad group(s), "
             f"{total} alerta(s) detectado(s) nesta passagem")
    return {"seller_id": seller_id, "campanhas": len(camp_ids), "ad_groups": len(ag_ids),
            "alertas_detectados": total}


def run_alerts_all(*, include_ad_groups=False):
    out = []
    for a in get_all_accounts():
        if not a.get("access_token"):
            continue
        try:
            out.append(run_alerts(a["seller_id"], include_ad_groups=include_ad_groups))
        except Exception as e:  # noqa: BLE001
            log.error(f"[{a.get('name')}] alertas falharam: {e}")
            out.append({"seller_id": a.get("seller_id"), "erro": str(e)})
    return out
