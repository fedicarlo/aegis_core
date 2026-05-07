from flask import Blueprint, render_template, redirect, url_for, flash
from app.database import get_account_by_seller_id
from app.services.analytics import compute_promo_profitability

promotions_bp = Blueprint("promotions", __name__)


@promotions_bp.route("/promocoes/<seller_id>")
def promocoes(seller_id):
    account = get_account_by_seller_id(seller_id)
    if not account:
        flash("Conta não encontrada.", "error")
        return redirect(url_for("web.index"))

    promos     = compute_promo_profitability(seller_id)
    with_cost  = [p for p in promos if p["has_cost"]]
    negative   = [p for p in promos if p["is_negative"]]
    avg_disc   = round(sum(p["discount_pct"] for p in promos) / max(1, len(promos)), 1)

    summary = {
        "total":        len(promos),
        "with_cost":    len(with_cost),
        "negative":     len(negative),
        "avg_discount": avg_disc,
    }

    return render_template(
        "promocoes.html",
        account   = account,
        seller_id = seller_id,
        promos    = promos,
        summary   = summary,
    )
