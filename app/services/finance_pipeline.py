"""
Orquestrador central do módulo financeiro do AEGIS.

Responsabilidades:
  - get_finance_snapshot: agrega todos os dados em uma passagem, com cache de 5 min
  - build_finance_context: monta o contexto completo para o template financeiro.html
  - run_finance_pipeline: pipeline completo (snapshot + insights)
  - invalidate_cache: limpa cache após coleta manual
"""
import time
import datetime as dt
from app.utils.logger import get_logger
from app.utils.charts import build_time_series
from app.database import (
    get_mp_payments_summary,
    get_mp_daily_net,
    get_mp_conciliation,
    get_mp_pending_releases,
    get_mp_fee_audit,
)

log = get_logger("finance_pipeline")

# ── Cache em memória — TTL 5 min ─────────────────────────────────────────────
_CACHE_TTL: int = 300
_cache: dict    = {}   # key → (unix_ts, payload)


def _cache_key(seller_id: str, days: int) -> str:
    return f"{seller_id}:{days}"


def _get_cached(key: str):
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _set_cached(key: str, data: dict) -> None:
    _cache[key] = (time.monotonic(), data)


def invalidate_cache(seller_id: str) -> int:
    """Remove todas as entradas de cache do seller. Retorna número de entradas removidas."""
    keys = [k for k in list(_cache) if k.startswith(f"{seller_id}:")]
    for k in keys:
        _cache.pop(k, None)
    if keys:
        log.info(f"cache invalidado seller={seller_id}: {len(keys)} entrada(s)")
    return len(keys)


# ── Agregação principal ───────────────────────────────────────────────────────

def get_finance_snapshot(seller_id: str, days: int = 30) -> dict:
    """
    Executa todas as queries financeiras em sequência e retorna dict completo.
    Resultado em cache por 5 minutos — evita múltiplas consultas para a mesma
    página em carregamentos rápidos.

    Campos retornados:
      summary, daily, conciliation, pending, pending_total, fee_audit,
      chart_labels, chart_net, chart_gross
    """
    key    = _cache_key(seller_id, days)
    cached = _get_cached(key)
    if cached:
        log.info(f"cache hit: {key}")
        return cached

    log.info(f"computando snapshot seller={seller_id} days={days}")

    summary      = get_mp_payments_summary(seller_id, days)
    daily        = get_mp_daily_net(seller_id, days)
    conciliation = get_mp_conciliation(seller_id, days)
    pending      = get_mp_pending_releases(seller_id)
    fee_audit    = get_mp_fee_audit(seller_id, days)

    pending_total = sum(r["net_received_amount"] for r in pending)

    # Série temporal para gráfico
    ts = build_time_series(daily, days, "net_amount", secondary_field="gross_amount")

    result = {
        "summary":      summary,
        "daily":        daily,
        "conciliation": conciliation,
        "pending":      pending,
        "pending_total": pending_total,
        "fee_audit":    fee_audit,
        "chart_labels": ts["labels"],
        "chart_net":    ts["values"],
        "chart_gross":  ts["secondary"],
    }

    _set_cached(key, result)
    return result


# ── Contexto para template ────────────────────────────────────────────────────

def build_finance_context(seller_id: str, days: int = 30) -> dict:
    """
    Monta o contexto completo para financeiro.html sem nenhuma lógica no template.
    Inclui snapshot + insights + placeholder de saldo (indisponível via token ML).
    """
    from app.services.finance_insights import compute_finance_alerts
    from app.services.meli_api import get_mp_balance

    snapshot = get_finance_snapshot(seller_id, days)
    insights = compute_finance_alerts(seller_id, days)

    # Saldo: endpoint MP não acessível com token OAuth ML — retorna stub imediatamente
    balance = get_mp_balance(None, seller_id)  # token=None → retorna _BALANCE_UNAVAILABLE

    return {
        **snapshot,
        "insights": insights,
        "balance":  balance,
        "days":     days,
    }


# ── Pipeline completo ─────────────────────────────────────────────────────────

def run_finance_pipeline(seller_id: str, days: int = 30) -> dict:
    """
    Ponto de entrada principal: snapshot + insights em uma chamada.
    Usado pela rota /financeiro/<seller_id>.
    """
    log.info(f"run_finance_pipeline seller={seller_id} days={days}")
    return build_finance_context(seller_id, days)
