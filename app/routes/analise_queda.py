from flask import Blueprint, render_template, redirect, url_for, flash, request
from app.database import get_all_accounts, get_performance_comparison
from app.utils.logger import get_logger

log = get_logger("routes.analise_queda")
analise_queda_bp = Blueprint("analise_queda", __name__)


@analise_queda_bp.route("/analise-queda/<seller_id>")
def analise_queda(seller_id: str):
    accounts   = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]
    account    = next((a for a in authorized if str(a["seller_id"]) == str(seller_id)), None)

    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))

    days = int(request.args.get("days", 30))
    if days not in (7, 15, 30, 60, 90):
        days = 30

    try:
        data = get_performance_comparison(seller_id, days)
    except Exception as e:
        log.error(f"analise_queda seller={seller_id}: {e}")
        data = {
            "period_days": days,
            "items": [],
            "total_revenue_current": 0,
            "total_revenue_prev": 0,
            "total_delta": 0,
            "by_cause": {},
        }

    # Separa itens que caíram dos que cresceram
    drops   = [i for i in data["items"] if i["revenue_delta"] < 0]
    growth  = [i for i in data["items"] if i["revenue_delta"] > 0]
    stable  = [i for i in data["items"] if i["revenue_delta"] == 0]

    return render_template(
        "analise_queda.html",
        account    = account,
        authorized = authorized,
        seller_id  = seller_id,
        days       = days,
        data       = data,
        drops      = drops,
        growth     = growth,
        stable     = stable,
    )
