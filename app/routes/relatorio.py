from flask import Blueprint, render_template, redirect, url_for, flash, request
from app.database import get_all_accounts
from app.services.analytics import compute_executive_report

relatorio_bp = Blueprint("relatorio", __name__)


@relatorio_bp.route("/relatorio/<seller_id>")
def relatorio(seller_id: str):
    accounts   = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]

    account = next((a for a in authorized if str(a["seller_id"]) == str(seller_id)), None)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))

    period = int(request.args.get("period", 30))
    if period not in (7, 15, 30, 60, 90):
        period = 30

    report = compute_executive_report(seller_id, days=period)

    return render_template(
        "relatorio.html",
        account    = account,
        authorized = authorized,
        seller_id  = seller_id,
        report     = report,
        period     = period,
    )
