"""
Ads — camada de composição (view-models) pra UI (Etapa 8).

Só ORQUESTRA os engines (ads_metrics / ads_finance / ads_diagnostic / ads_alerts /
ads_experiments / ads_strategy) e monta o dict que o template renderiza. Zero
lógica de diagnóstico aqui e zero no template — os engines já decidiram tudo.
"""
from datetime import datetime, timedelta, timezone

from app.database import (
    get_ads_ad_group,
    get_ads_alerts,
    get_ad_group_items_full,
    get_campaign_ad_groups,
    get_conn,
)
from app.services import (
    ads_diagnostic,
    ads_experiments,
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


# ── 8c — Detalhe de campanha ──────────────────────────────────────────────

def campaign_detail(seller_id, campaign_id, *, period=30, since_last_change=False):
    d_from, d_to = _window(period)
    margem_alvo = ads_strategy.margem_alvo_pct(seller_id)

    m = ads_metrics.campaign_metrics(seller_id, campaign_id, d_from, d_to,
                                     since_last_change=since_last_change, include_series=True)
    if not m.get("found"):
        return None
    fin_full = ads_finance.campaign_finance(seller_id, campaign_id, d_from, d_to,
                                            since_last_change=since_last_change,
                                            margem_alvo_pct=margem_alvo)
    dg = ads_diagnostic.diagnose(seller_id, "campaign", campaign_id, d_from, d_to,
                                 since_last_change=since_last_change)
    rec = ads_diagnostic.recommend(seller_id, "campaign", campaign_id, d_from, d_to,
                                   since_last_change=since_last_change)

    ish = m.get("impression_share") or {}
    imp_breakdown = None
    if ish.get("impression_share") is not None:
        won = round(ish["impression_share"] * 100, 1)
        lb = round((ish.get("lost_impression_share_by_budget") or 0) * 100, 1)
        lr = round((ish.get("lost_impression_share_by_ad_rank") or 0) * 100, 1)
        imp_breakdown = {
            "ganho_pct": won, "perdido_orcamento_pct": lb, "perdido_classificacao_pct": lr,
            "top_pct": round((ish.get("top_impression_share") or 0) * 100, 1),
            "acos_benchmark": ish.get("acos_benchmark"),
        }

    series = m.get("daily_series") or []
    chart = {
        "labels": [r["date"] for r in series],
        "cost": [r.get("cost") for r in series],
        "roas": [r.get("roas") for r in series],
        "acos": [r.get("acos") for r in series],
        "acos_benchmark": [r.get("acos_benchmark") for r in series],
        "prints": [r.get("prints") for r in series],
        "clicks": [r.get("clicks") for r in series],
        "ads_units": [r.get("units_quantity") for r in series],
        "impression_share": [(r.get("impression_share") or 0) * 100
                             if r.get("impression_share") is not None else None for r in series],
    }

    # ad groups da campanha (resumo — detalhe fica na 8d)
    ags = []
    for g in get_campaign_ad_groups(seller_id, campaign_id, include_scaffold=True):
        gid = g["ad_group_id_ml"]
        gm = ads_metrics.ad_group_metrics(seller_id, gid, d_from, d_to,
                                          since_last_change=since_last_change, include_series=False)
        gf = gm.get("funnel") or {}
        ags.append({
            "ad_group_id": gid, "type": g.get("ad_group_type"),
            "external_id": g.get("ad_group_external_id"), "title": g.get("title"),
            "status_ml": g.get("status"), "is_scaffold": bool(g.get("is_scaffold")),
            "sku_level": g.get("ad_group_type") == "ITEM",
            "serving": gm.get("serving"), "not_serving_reason": gm.get("not_serving_reason"),
            "prints": gf.get("prints"), "clicks": gf.get("clicks"), "cost": gf.get("cost"),
            "ads_units": gf.get("ads_units"), "ads_revenue": gf.get("ads_revenue"),
            "acos": gf.get("acos"), "roas": gf.get("roas"),
            "tags": g.get("tags"),
        })
    ags.sort(key=lambda x: (-(x["cost"] or 0), x["title"] or ""))

    return {
        "campaign": {
            "id": campaign_id, "name": m.get("name"), "status_ml": m.get("status_ml"),
            "strategy": m.get("strategy"), "budget": m.get("budget"),
            "acos_target": m.get("acos_target"), "roas_target": m.get("roas_target"),
            "currency_id": m.get("currency_id"),
        },
        "window": m["window"], "period": period, "since_last_change": since_last_change,
        "serving": m["serving"], "status": m["status"],
        "not_serving_reason": m.get("not_serving_reason"),
        "funnel": m.get("funnel"),
        "impression_share": imp_breakdown,
        "finance": fin_full.get("finance"),
        "diagnostico": dg,
        "recomendacao": rec,
        "alertas": get_ads_alerts(seller_id, scope="campaign", target_id=campaign_id, only_open=True),
        "timeline": ads_experiments.timeline(seller_id, "campaign", campaign_id, limit=40),
        "ad_groups": ags,
        "chart": chart,
    }


# ── 8d — Análise por SKU / Ad Group ──────────────────────────────────────

def ad_group_detail(seller_id, ad_group_id, *, period=30, since_last_change=False):
    d_from, d_to = _window(period)
    margem_alvo = ads_strategy.margem_alvo_pct(seller_id)

    ag = get_ads_ad_group(seller_id, ad_group_id)
    if not ag:
        return None

    m = ads_metrics.ad_group_metrics(seller_id, ad_group_id, d_from, d_to,
                                     since_last_change=since_last_change, include_series=True)
    fin_full = ads_finance.ad_group_finance(seller_id, ad_group_id, d_from, d_to,
                                            since_last_change=since_last_change,
                                            margem_alvo_pct=margem_alvo)
    dg = ads_diagnostic.diagnose(seller_id, "ad_group", ad_group_id, d_from, d_to,
                                 since_last_change=since_last_change)
    rec = ads_diagnostic.recommend(seller_id, "ad_group", ad_group_id, d_from, d_to,
                                   since_last_change=since_last_change)

    fin = fin_full.get("finance") or {}
    by_sku = fin.get("by_sku") or {}
    itens = []
    for it in get_ad_group_items_full(seller_id, ad_group_id):
        bd = by_sku.get(it["item_id"])
        itens.append({
            **it,
            "lucro_antes_ads": bd["lucro_antes_ads"] if bd else None,
            "margem_antes_ads_pct": bd["margem_antes_ads_pct"] if bd else None,
            "receita_bruta": bd["receita_bruta"] if bd else None,
            "qty": bd["qty"] if bd else None,
            "has_cost": bd is not None,
        })
    itens.sort(key=lambda x: -(x["receita_bruta"] or 0))

    series = m.get("daily_series") or []
    chart = {
        "labels": [r["date"] for r in series],
        "cost": [r.get("cost") for r in series],
        "roas": [r.get("roas") for r in series],
        "acos": [r.get("acos") for r in series],
        "prints": [r.get("prints") for r in series],
        "ads_units": [r.get("units_quantity") for r in series],
    }

    return {
        "ad_group": {
            "id": ad_group_id, "type": ag.get("ad_group_type"),
            "external_id": ag.get("ad_group_external_id"), "title": ag.get("title"),
            "status_ml": ag.get("status"), "campaign_id": ag.get("campaign_id"),
            "is_scaffold": bool(ag.get("is_scaffold")), "tags": ag.get("tags"),
            "date_created_ml": ag.get("date_created_ml"),
            "domain_id": ag.get("domain_id"),
        },
        "sku_level": ag.get("ad_group_type") == "ITEM",
        "window": m["window"], "period": period, "since_last_change": since_last_change,
        "serving": m["serving"], "status": m["status"],
        "not_serving_reason": m.get("not_serving_reason"),
        "funnel": m.get("funnel"),
        "finance": fin,
        "diagnostico": dg, "recomendacao": rec,
        "itens": itens,
        "n_itens": len(itens),
        "alertas": get_ads_alerts(seller_id, scope="ad_group", target_id=ad_group_id, only_open=True),
        "timeline": ads_experiments.timeline(seller_id, "ad_group", ad_group_id, limit=40),
        "chart": chart,
    }


# ── 8e — Experimentos ───────────────────────────────────────────────────────

def _targets_for_select(seller_id):
    """Campanhas + ad groups (não-scaffold) p/ os selects de alvo dos formulários."""
    conn = get_conn()
    camps = [{"id": r["campaign_id_ml"], "label": f"campanha · {r['name']}"}
             for r in conn.execute(
                 "SELECT campaign_id_ml, name FROM campaigns WHERE seller_id=? ORDER BY name",
                 (seller_id,))]
    ags = [{"id": r["ad_group_id_ml"],
            "label": f"ad group · {(r['title'] or r['ad_group_external_id'])[:50]} ({r['ad_group_type']})"}
           for r in conn.execute(
               "SELECT ad_group_id_ml, title, ad_group_external_id, ad_group_type "
               "FROM ad_groups WHERE seller_id=? AND is_scaffold=0 ORDER BY title",
               (seller_id,))]
    conn.close()
    return camps, ags


def experiments_page(seller_id):
    from app.database import list_ads_experiments
    exps = list_ads_experiments(seller_id, limit=200)
    for x in exps:
        x["created_iso"] = datetime.fromtimestamp(x["created_at"], tz=timezone.utc).isoformat()[:16]
    camps, ags = _targets_for_select(seller_id)
    return {"experimentos": exps, "campanhas_sel": camps, "ad_groups_sel": ags}


def experiment_detail(seller_id, experiment_id):
    from app.database import get_ads_experiment
    exp = get_ads_experiment(experiment_id)
    if not exp or str(exp["seller_id"]) != str(seller_id):
        return None
    exp["created_iso"] = datetime.fromtimestamp(exp["created_at"], tz=timezone.utc).isoformat()[:16]
    avaliacao = ads_experiments.evaluate(experiment_id)
    return {"exp": exp, "avaliacao": avaliacao}


# ── 8f — Config do Strategy Profile ─────────────────────────────────────────

def config_page(seller_id):
    import json as _json
    prof = ads_strategy.get_strategy_profile(seller_id)
    grupos = []
    for g in ("minimum_sample_rules", "profit_targets", "diagnostic_rules",
              "risk_limits", "development_rules", "consolidation_rules"):
        efetivo = {k: v for k, v in prof[g].items() if k != "_source"}
        grupos.append({
            "nome": g,
            "source": prof[g].get("_source"),
            "efetivo_json": _json.dumps(efetivo, ensure_ascii=False, indent=2),
            "defaults_json": _json.dumps(ads_strategy.DEFAULTS[g], ensure_ascii=False, indent=2),
        })
    # override do próprio seller (o que está salvo especificamente pra ele)
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM ads_strategy_profile WHERE seller_id = ? AND name = 'default'",
        (seller_id,)).fetchone()
    conn.close()
    seller_override = {}
    if row:
        for g in ("minimum_sample_rules", "profit_targets", "diagnostic_rules",
                  "risk_limits", "development_rules", "consolidation_rules"):
            try:
                v = _json.loads(row[g] or "{}")
            except (TypeError, ValueError):
                v = {}
            if v:
                seller_override[g] = v
    return {"grupos": grupos, "tem_override_seller": bool(seller_override),
            "seller_override": seller_override}
