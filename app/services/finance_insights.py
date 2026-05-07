"""
Inteligência ativa sobre dados financeiros do Mercado Pago.
Detecta anomalias e gera alertas estruturados.
"""
import datetime as dt
from app.utils.logger import get_logger
from app.database import (
    get_mp_payments_summary,
    get_mp_daily_net,
    get_mp_pending_releases,
    get_mp_fee_audit,
)

log = get_logger("finance_insights")

# ── Nível → cor/ícone no template ────────────────────────────────────────────
LEVEL_META = {
    "critical": {"icon": "🔴", "css": "alert-critical"},
    "warning":  {"icon": "🟡", "css": "alert-warning"},
    "info":     {"icon": "🔵", "css": "alert-info"},
}


def _alert(level: str, message: str, detail: str, module: str = "financeiro") -> dict:
    return {
        "level":   level,
        "module":  module,
        "message": message,
        "detail":  detail,
        **LEVEL_META.get(level, LEVEL_META["info"]),
    }


def compute_finance_alerts(seller_id: str, days: int = 30) -> list:
    """
    Executa todas as regras de alerta financeiro para um seller.

    Regras implementadas:
      1. Taxa ML real > 16% no período → suspeita de recategorização
      2. Volume retido (pending) > 2× receita 7d → capital preso
      3. Margem média < 10% → precificação crítica
      4. Receita de hoje < 50% da média diária → queda brusca
      5. Pagamentos com data de liberação já vencida → retenção além do prazo

    Retorna lista de dicts prontos para o template.
    """
    alerts = []
    today  = dt.date.today()

    try:
        summary    = get_mp_payments_summary(seller_id, days)
        summary_7d = get_mp_payments_summary(seller_id, 7)
        pending    = get_mp_pending_releases(seller_id)
        fee_audit  = get_mp_fee_audit(seller_id, days)
        daily      = get_mp_daily_net(seller_id, days)
    except Exception as e:
        log.warning(f"compute_finance_alerts: erro ao carregar dados seller={seller_id}: {e}")
        return []

    receita_bruta = summary.get("receita_bruta", 0) or 0
    receita_7d    = summary_7d.get("receita_bruta", 0) or 0
    total_ml_fee  = summary.get("total_ml_fee", 0) or 0

    # ── Regra 1: Taxa ML > 16% no período ──────────────────────────────────
    if receita_bruta > 0:
        taxa_real = total_ml_fee / receita_bruta * 100
        if taxa_real > 16:
            alerts.append(_alert(
                "warning",
                f"Taxa ML acima do esperado: {taxa_real:.1f}%",
                f"A taxa real cobrada pelo ML no período ({taxa_real:.1f}%) está acima de 16%. "
                "Pode indicar mudança de categoria. Verifique a Seção 5 — Conferência de Tarifas.",
            ))

    # ── Regra 2: Volume retido > 2× receita 7d ─────────────────────────────
    pending_total = sum(r["net_received_amount"] for r in pending)
    if receita_7d > 0 and pending_total > receita_7d * 2:
        alerts.append(_alert(
            "warning",
            f"Alto volume retido: R$ {pending_total:,.0f} a liberar",
            f"O valor a liberar (R$ {pending_total:,.0f}) supera 2× a receita dos últimos 7 dias "
            f"(R$ {receita_7d:,.0f}). Pode indicar bloqueio de conta ou concentração em parcelas.",
        ))

    # ── Regra 3: Margem média < 10% ────────────────────────────────────────
    try:
        from app.services.analytics import compute_margin
        md = compute_margin(seller_id, days=30)
        margem_pct = md.get("totals", {}).get("margem_pct")
        if margem_pct is not None and margem_pct < 10:
            severity = "critical" if margem_pct < 5 else "warning"
            alerts.append(_alert(
                severity,
                f"Margem média crítica: {margem_pct:.1f}%",
                f"A margem média ponderada dos últimos 30 dias está em {margem_pct:.1f}%. "
                "Revise precificação e custos em Custos → Apuração.",
                module="margem",
            ))
    except Exception as e:
        log.debug(f"margem check skipped: {e}")

    # ── Regra 4: Receita hoje < 50% da média diária ─────────────────────────
    if daily and len(daily) >= 7:
        media_diaria = sum(r["gross_amount"] for r in daily) / len(daily)
        today_str    = today.isoformat()
        today_row    = next((r for r in daily if r["day"] == today_str), None)
        if today_row and media_diaria > 50:  # só alerta se média for significativa
            pct = today_row["gross_amount"] / media_diaria * 100
            if pct < 50:
                alerts.append(_alert(
                    "critical",
                    f"Queda brusca hoje: {pct:.0f}% da média diária",
                    f"Receita de hoje (R$ {today_row['gross_amount']:,.0f}) está em {pct:.0f}% "
                    f"da média diária do período (R$ {media_diaria:,.0f}). "
                    "Verifique se há anúncios pausados ou problemas de catálogo.",
                ))

    # ── Regra 5: Pagamentos retidos além do prazo ───────────────────────────
    overdue = [
        p for p in pending
        if p.get("money_release_date", "") and p["money_release_date"][:10] < today.isoformat()
    ]
    if overdue:
        total_overdue  = sum(r["net_received_amount"] for r in overdue)
        oldest_date    = min(p["money_release_date"][:10] for p in overdue)
        days_overdue   = (today - dt.date.fromisoformat(oldest_date)).days
        alerts.append(_alert(
            "critical" if days_overdue > 14 else "warning",
            f"{len(overdue)} pagamento(s) retido(s) além do prazo",
            f"R$ {total_overdue:,.2f} com data de liberação já vencida "
            f"(mais antigo: {oldest_date}, há {days_overdue} dias). "
            "Acesse Financeiro → Seção 4 ou entre em contato com o suporte ML.",
        ))

    log.info(f"compute_finance_alerts seller={seller_id}: {len(alerts)} alerta(s)")
    return alerts
