"""
Ads — Linha do tempo de eventos + Experimentos (Etapa 7).

Eventos: a coleta (Etapa 3) já grava os eventos de SISTEMA (diff de snapshot de
meta/orçamento). Aqui é o registro MANUAL — o operador anota "mudei X por causa Y,
hipótese Z" — e a leitura da timeline pra UI.

Experimentos: hipótese -> intervenção -> janela -> resultado -> conclusão.
`evaluate()` calcula o Before/After usando o Metrics Engine, com janelas
SIMÉTRICAS em torno da data de intervenção (Before tem o mesmo tamanho do After),
e reporta a régua de amostra dos dois lados (pra não comparar com um lado fino).
"""
from datetime import datetime, timedelta, timezone

from app.database import (
    create_ads_experiment,
    get_ads_experiment,
    get_ads_events,
    list_ads_experiments,
    record_ads_event,
    update_ads_experiment,
)
from app.services import ads_metrics
from app.utils.logger import get_logger

log = get_logger("ads_experiments")

_ML_TZ_OFFSET_H = -3

_COMPARE_KEYS = ("prints", "clicks", "cost", "ctr", "cpc", "acos", "roas", "cvr",
                 "ads_units", "ads_revenue", "real_revenue", "real_units",
                 "real_orders", "organic_revenue", "tacos", "days_with_prints")


def _today_ml():
    return (datetime.now(timezone.utc) + timedelta(hours=_ML_TZ_OFFSET_H)).date()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Eventos (registro manual) ─────────────────────────────────────────────

def record_manual_event(seller_id, scope, target_id, field, *, old_value=None,
                        new_value=None, author, motivo=None, hipotese=None):
    """Ex: field='roas_target', old=14, new=12, motivo='margem baixa', hipotese='mais volume'."""
    eid = record_ads_event(seller_id, scope, target_id, field, old_value=old_value,
                           new_value=new_value, author=author, motivo=motivo,
                           hipotese=hipotese, source="manual")
    log.info(f"evento manual #{eid} {scope}={target_id} field={field} por {author}")
    return eid


def timeline(seller_id, scope, target_id, *, limit=100):
    """Linha do tempo unificada (sistema + manual), mais recente primeiro."""
    evs = get_ads_events(seller_id, scope, target_id, limit=limit)
    for e in evs:
        e["changed_at_iso"] = datetime.fromtimestamp(e["changed_at"], tz=timezone.utc).isoformat()
    return evs


# ── Experimentos ─────────────────────────────────────────────────────────

def create(seller_id, scope, target_id, hipotese, *, intervencao=None,
           janela_inicio=None, janela_fim=None):
    janela_inicio = janela_inicio or _today_ml().isoformat()
    xid = create_ads_experiment(seller_id, scope, target_id, hipotese,
                                intervencao=intervencao, janela_inicio=janela_inicio,
                                janela_fim=janela_fim)
    log.info(f"experimento #{xid} criado — {scope}={target_id}, início {janela_inicio}")
    return xid


def get(experiment_id):
    return get_ads_experiment(experiment_id)


def listar(seller_id, **kw):
    return list_ads_experiments(seller_id, **kw)


def update(experiment_id, **fields):
    update_ads_experiment(experiment_id, **fields)


def _window_metrics(exp, d_from, d_to):
    mfn = (ads_metrics.campaign_metrics if exp["scope"] == "campaign"
           else ads_metrics.ad_group_metrics)
    m = mfn(exp["seller_id"], _target(exp), d_from, d_to, include_series=False)
    ss = ads_metrics.sample_sufficiency(exp["seller_id"], exp["scope"], _target(exp), d_from, d_to)
    return {
        "window": {"from": d_from, "to": d_to},
        "serving": m.get("serving"),
        "funnel": m.get("funnel"),
        "amostra": {"suficiente": ss.get("suficiente"), "motivo": ss.get("motivo")},
    }


def _target(exp):
    tid = exp["target_id"]
    try:
        return int(tid)
    except (TypeError, ValueError):
        return tid


def evaluate(experiment_id, *, ref_date=None, persist=False):
    """
    Before/After em torno de janela_inicio (data da intervenção).
    After  = [janela_inicio .. janela_fim ou ontem]
    Before = janela imediatamente anterior, do MESMO tamanho do After.
    """
    exp = get_ads_experiment(experiment_id)
    if not exp:
        return {"found": False, "experiment_id": experiment_id}
    if not exp.get("janela_inicio"):
        return {"found": True, "experiment_id": experiment_id,
                "erro": "experimento sem janela_inicio (data da intervenção)"}

    ref = ref_date or _today_ml()
    start = datetime.fromisoformat(exp["janela_inicio"]).date()
    after_to = (datetime.fromisoformat(exp["janela_fim"]).date()
                if exp.get("janela_fim") else ref - timedelta(days=1))
    if after_to < start:
        return {"found": True, "experiment_id": experiment_id,
                "erro": "janela After ainda não tem dias fechados (intervenção muito recente)"}

    after_len = (after_to - start).days + 1
    before_to = start - timedelta(days=1)
    before_from = before_to - timedelta(days=after_len - 1)

    before = _window_metrics(exp, before_from.isoformat(), before_to.isoformat())
    after = _window_metrics(exp, start.isoformat(), after_to.isoformat())

    comparacao = {}
    bf = before.get("funnel") or {}
    af = after.get("funnel") or {}
    for k in _COMPARE_KEYS:
        b, a = _f(bf.get(k)), _f(af.get(k))
        row = {"antes": b, "depois": a, "delta": None, "delta_pct": None}
        if b is not None and a is not None:
            row["delta"] = round(a - b, 4)
            row["delta_pct"] = round((a - b) / b * 100, 1) if b else None
        comparacao[k] = row

    out = {
        "found": True, "experiment_id": experiment_id,
        "scope": exp["scope"], "target_id": exp["target_id"],
        "hipotese": exp["hipotese"], "intervencao": exp.get("intervencao"),
        "before": before, "after": after,
        "before_window": before["window"], "after_window": after["window"],
        "comparacao": comparacao,
        "ressalva_amostra": [
            lado for lado, w in (("before", before), ("after", after))
            if not (w["amostra"] or {}).get("suficiente")
        ],
    }
    if persist:
        update_ads_experiment(experiment_id, resultado=out,
                              status="concluido" if exp.get("janela_fim") else exp.get("status"))
    return out
