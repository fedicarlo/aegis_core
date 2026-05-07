from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from app.database import get_all_accounts, get_account_by_seller_id

simulador_bp = Blueprint("simulador", __name__)


@simulador_bp.route("/simulador/<seller_id>")
def simulador(seller_id: str):
    accounts   = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]

    account = next((a for a in authorized if str(a["seller_id"]) == str(seller_id)), None)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))

    query = request.args.get("q", "").strip()

    return render_template(
        "simulador.html",
        account    = account,
        authorized = authorized,
        seller_id  = seller_id,
        query      = query,
    )


@simulador_bp.route("/simulador/<seller_id>/search")
def simulador_search(seller_id: str):
    """JSON endpoint — retorna preços de mercado para uma query."""
    accounts   = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]
    account    = next((a for a in authorized if str(a["seller_id"]) == str(seller_id)), None)

    if not account:
        return jsonify({"ok": False, "error": "Conta não encontrada"}), 404

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Query vazia"}), 400

    try:
        from app.services.meli_api import search_market
        from app.services.meli_auth import get_valid_token

        token   = get_valid_token(account)
        results = search_market(token, query, limit=20)

        prices = [r["price"] for r in results if r["price"] > 0]
        summary = {}
        if prices:
            summary = {
                "count":     len(prices),
                "min":       round(min(prices), 2),
                "avg":       round(sum(prices) / len(prices), 2),
                "max":       round(max(prices), 2),
                "median":    round(sorted(prices)[len(prices) // 2], 2),
            }

        return jsonify({
            "ok":      True,
            "query":   query,
            "summary": summary,
            "items":   results[:15],
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
