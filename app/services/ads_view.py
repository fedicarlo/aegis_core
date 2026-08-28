"""
Ads — camada de composição (view-models) pra UI (Etapa 8).

Só ORQUESTRA os engines (ads_metrics / ads_finance / ads_diagnostic / ads_alerts /
ads_experiments / ads_strategy) e monta o dict que o template renderiza. Zero
lógica de diagnóstico aqui e zero no template — os engines já decidiram tudo.
"""
from datetime import datetime, timedelta, timezone

from app.database import (
    get_ads_alerts,
    get_ads_events,
    get_conn,
)
from app.services import (
    ads_diagnostic,
    ads_finance,
    ads_metrics,
    ads_strategy,
)
from app.utils.logger import get_logger

log = get_logger("ads_view")

_ML_TZ_OFFSET_H = -3


def _today_ml():
    return (datetime.now(timezone.utc) + timedelta(hours=_ML_TZ_OFFSET_H)).date()


def _window(days):
    """(from, to) terminando ONTEM (último dia fechado no fuso do ML)."""
    to = _today_ml() - timedelta(days=1)
    return (to - timedelta(days=days - 1)).isoformat(), to.isoformat()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _campaign_ids(seller_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT campaign_id_ml, name, status FROM campaigns WHERE seller_id = ? ORDER BY name",
        (seller_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 8a — Cockpit ───────────────────────────────────────────────────────────

def cockpit(seller_id, *, period=30):
    d_from, d_to = _window(period)
    o_from, o_to = _window(1)
    w_from, w_to = _window(7)

    camp_rows = _campaign_ids(seller_id)
    prof = ads_strategy.get_strategy_profile(seller_id)
    margem_alvo = ads_strategy.margem_alvo_pct(seller_id)

    # investimento por janela (soma das campanhas)
    def _invest(a, b):
        tot = 0.0
        for c in camp_rows:
            m = ads_metrics.campaign_metrics(seller_id, c["campaign_id_ml"], a, b,
                                             include_series=False)
            if m.get("funnel"):
                tot += _f(m["funnel"]["cost"]) or 0
        return round(tot, 2)

    investimento = {"ontem": _invest(o_from, o_to), "d7": _invest(w_from, w_to),
                    "d30": _invest(d_from, d_to)}

    # funil + economia consolidados do período
    acc_fin = ads_finance.account_finance(seller_id, d_from, d_to, margem_alvo_pct=margem_alvo)
    fin = acc_fin["finance"]

    funil = {"prints": 0, "clicks": 0, "ads_units": 0, "ads_revenue": 0.0,
             "real_revenue": 0.0, "real_units": 0, "organic_units_ml": 0,
             "cost": 0.0}
    roas_alvo_num = roas_alvo_den = 0.0
    campanhas = []
    for c in camp_rows:
        cid = c["campaign_id_ml"]
        m = ads_metrics.campaign_metrics(seller_id, cid, d_from, d_to, include_series=False)
        dg = ads_diagnostic.diagnose(seller_id, "campaign", cid, d_from, d_to)
        f = m.get("funnel") or {}
        if m.get("serving"):
            for k in ("prints", "clicks", "ads_units", "real_units", "organic_units_ml"):
                funil[k] += int(_f(f.get(k)) or 0)
            for k in ("ads_revenue", "real_revenue", "cost"):
                funil[k] += _f(f.get(k)) or 0
            rt = _f(m.get("roas_target"))
            cost = _f(f.get("cost")) or 0
            if rt and cost:
                roas_alvo_num += rt * cost
                roas_alvo_den += cost
        campanhas.append({
            "campaign_id": cid, "name": c["name"], "status_ml": c["status"],
            "serving": m.get("serving"), "diag_status": dg["status"],
            "caso_primario": dg.get("caso_primario"),
            "roas": f.get("roas"), "acos": f.get("acos"), "cost": f.get("cost"),
            "ads_revenue": f.get("ads_revenue"), "ctr": f.get("ctr"),
            "impression_share_pct": _is_pct(m.get("impression_share")),
        })

    for k in ("ads_revenue", "real_revenue", "cost"):
        funil[k] = round(funil[k], 2)
    funil["ctr"] = round(funil["clicks"] / funil["prints"] * 100, 2) if funil["prints"] else None
    funil["cpc"] = round(funil["cost"] / funil["clicks"], 2) if funil["clicks"] else None
    funil["acos"] = round(funil["cost"] / funil["ads_revenue"] * 100, 2) if funil["ads_revenue"] else None
    funil["roas_realizado"] = round(funil["ads_revenue"] / funil["cost"], 2) if funil["cost"] else None
    funil["organic_units_reais"] = max(funil["real_units"] - funil["ads_units"], 0)

    roas_objetivo = round(roas_alvo_num / roas_alvo_den, 2) if roas_alvo_den else None

    alertas = get_ads_alerts(seller_id, only_open=True, limit=100)

    return {
        "seller_id": seller_id, "period": period,
        "window": {"from": d_from, "to": d_to},
        "investimento": investimento,
        "funil": funil,
        "economia": {k: fin.get(k) for k in (
            "receita_real", "ads_cost", "ads_revenue_atribuida", "lucro_antes_ads",
            "lucro_depois_ads", "margem_antes_ads_pct", "margem_depois_ads_pct",
            "margem_alvo_pct", "tacos_pct", "tacos_maximo_operacional_pct",
            "atinge_meta_blended", "roas_realizado", "roas_equilibrio",
            "roas_minimo_operacional", "ads_auto_suficiente_na_meta",
            "custo_incompleto", "n_itens_com_venda_e_custo")},
        "roas_objetivo": roas_objetivo,
        "status_geral": _status_geral(campanhas, alertas),
        "alertas_abertos": alertas,
        "campanhas": campanhas,
        "profile_source": {g: prof[g].get("_source") for g in
                           ("minimum_sample_rules", "profit_targets", "risk_limits",
                            "diagnostic_rules")},
    }


def _is_pct(ish):
    if not ish or ish.get("impression_share") is None:
        return None
    return round(ish["impression_share"] * 100, 1)


def _status_geral(campanhas, alertas):
    sev = {a["severidade"] for a in alertas}
    if "critico" in sev:
        return "CRITICO"
    serving = [c for c in campanhas if c["serving"]]
    if not serving:
        return "APRENDIZADO"
    if "atencao" in sev or any(c["diag_status"] == "COM_GARGALO" for c in serving):
        return "ATENCAO"
    if all(c["diag_status"] == "APRENDIZADO" for c in serving):
        return "APRENDIZADO"
    return "SAUDAVEL"


# ── 8b — Lista de campanhas ───────────────────────────────────────────────

def campaign_list(seller_id, *, period=30, since_last_change=False):
    d_from, d_to = _window(period)
    margem_alvo = ads_strategy.margem_alvo_pct(seller_id)
    out = []
    for c in _campaign_ids(seller_id):
        cid = c["campaign_id_ml"]
        m = ads_metrics.campaign_metrics(seller_id, cid, d_from, d_to,
                                         since_last_change=since_last_change,
                                         include_series=False)
        fin_full = ads_finance.campaign_finance(seller_id, cid, d_from, d_to,
                                                since_last_change=since_last_change,
                                                margem_alvo_pct=margem_alvo)
        dg = ads_diagnostic.diagnose(seller_id, "campaign", cid, d_from, d_to,
                                     since_last_change=since_last_change)
        f = m.get("funnel") or {}
        fin = fin_full.get("finance") or {}
        out.append({
            "campaign_id": cid, "name": c["name"], "status_ml": c["status"],
            "strategy": m.get("strategy"), "budget": m.get("budget"),
            "acos_target": m.get("acos_target"), "roas_target": m.get("roas_target"),
            "serving": m.get("serving"), "not_serving_reason": m.get("not_serving_reason"),
            "window": m.get("window"),
            "prints": f.get("prints"), "clicks": f.get("clicks"), "ctr": f.get("ctr"),
            "cpc": f.get("cpc"), "cost": f.get("cost"),
            "ads_units": f.get("ads_units"), "ads_revenue": f.get("ads_revenue"),
            "real_revenue": f.get("real_revenue"), "acos": f.get("acos"),
            "roas": f.get("roas"), "tacos": f.get("tacos"),
            "impression_share_pct": _is_pct(m.get("impression_share")),
            "lost_by_ad_rank_pct": _lost(m.get("impression_share"), "lost_impression_share_by_ad_rank"),
            "lost_by_budget_pct": _lost(m.get("impression_share"), "lost_impression_share_by_budget"),
            "margem_depois_ads_pct": fin.get("margem_depois_ads_pct"),
            "atinge_meta_blended": fin.get("atinge_meta_blended"),
            "ads_auto_suficiente_na_meta": fin.get("ads_auto_suficiente_na_meta"),
            "roas_minimo_operacional": fin.get("roas_minimo_operacional"),
            "diag_status": dg["status"], "caso_primario": dg.get("caso_primario"),
        })
    return out


def _lost(ish, key):
    if not ish or ish.get(key) is None:
        return None
    return round(ish[key] * 100, 1)
