from flask import Blueprint, render_template, redirect, url_for, flash, jsonify, request
from app.database import (
    get_all_accounts, save_mp_payments, save_meli_order_payments,
    save_mp_other_movements, get_mp_other_movements_summary,
)
from app.services.finance_pipeline import run_finance_pipeline, invalidate_cache
from app.utils.logger import get_logger

log = get_logger("routes.financeiro")

financeiro_bp = Blueprint("financeiro", __name__)


def _authorized_account(seller_id: str):
    """Resolve conta autorizada. Usa .get() para não quebrar em rows sem token."""
    accounts   = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]
    return next((a for a in authorized if str(a["seller_id"]) == str(seller_id)), None), authorized


@financeiro_bp.route("/financeiro/<seller_id>")
def financeiro(seller_id: str):
    account, authorized = _authorized_account(seller_id)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))

    days = int(request.args.get("days", 30))
    if days not in (7, 15, 30, 60, 90):
        days = 30

    ctx = run_finance_pipeline(seller_id, days)
    other_movements = get_mp_other_movements_summary(seller_id, days)

    return render_template(
        "financeiro.html",
        account          = account,
        authorized       = authorized,
        seller_id        = seller_id,
        other_movements  = other_movements,
        **ctx,
    )


@financeiro_bp.route("/financeiro/<seller_id>/balance")
def balance_json(seller_id: str):
    """Endpoint de saldo MP — indisponível com token OAuth ML (404/403)."""
    return jsonify({
        "ok":      False,
        "available": False,
        "error":   "Endpoint de saldo MP retorna 404/403 com token OAuth do Mercado Livre. "
                   "Necessário token de aplicação MP separada.",
    }), 404


@financeiro_bp.route("/financeiro/<seller_id>/collect", methods=["POST"])
def collect_payments(seller_id: str):
    """
    Coleta manual de pagamentos MP.
    Token resolvido uma vez por request — nunca chama get_valid_token() mais de uma vez.
    JSON validado com silent=True para não lançar 400 em body vazio.
    """
    account, _ = _authorized_account(seller_id)
    if not account:
        return jsonify({"ok": False, "error": "Conta não encontrada"}), 404

    # Ponto 5 — validação JSON segura
    body = request.get_json(silent=True) or {}
    try:
        days = int(body.get("days", 30))
        if days not in (7, 15, 30, 60, 90):
            days = 30
    except (TypeError, ValueError):
        days = 30

    try:
        # Ponto 9 — token resolvido uma vez
        from app.services.meli_auth import get_valid_token
        from app.services.meli_api import get_mp_payments, get_mp_other_movements

        token    = get_valid_token(account)
        payments = get_mp_payments(token, seller_id, days=days)
        save_mp_payments(seller_id, payments)

        others = get_mp_other_movements(token, seller_id, days=days)
        save_mp_other_movements(seller_id, others)

        # Invalida cache para que a próxima página use dados frescos
        invalidate_cache(seller_id)

        log.info(
            "coleta manual: seller=%s days=%s sales=%s outros=%s",
            seller_id, days, len(payments), len(others),
        )
        return jsonify({"ok": True, "collected": len(payments), "other_movements": len(others)})

    except Exception as e:
        log.error(f"collect_payments seller={seller_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@financeiro_bp.route("/financeiro/<seller_id>/collect-official", methods=["POST"])
def collect_official_payments(seller_id: str):
    """
    Nova coleta oficial via Mercado Livre orders + payments.
    Salva em staging paralelo e opcionalmente espelha para mp_payments.
    """
    account, _ = _authorized_account(seller_id)
    if not account:
        return jsonify({"ok": False, "error": "Conta não encontrada"}), 404

    body = request.get_json(silent=True) or {}
    try:
        days = int(body.get("days", 30))
        if days not in (7, 15, 30, 60, 90):
            days = 30
    except (TypeError, ValueError):
        days = 30

    sync_to_pipeline = bool(body.get("sync_to_pipeline", False))

    try:
        from app.services.meli_auth import get_valid_token
        from app.services.meli_api import collect_meli_order_payments

        token = get_valid_token(account)
        payments = collect_meli_order_payments(token, seller_id, days=days)
        save_meli_order_payments(seller_id, payments)

        if sync_to_pipeline:
            save_mp_payments(seller_id, payments)
            invalidate_cache(seller_id)

        log.info(
            "coleta oficial: seller=%s days=%s payments=%s sync_to_pipeline=%s",
            seller_id, days, len(payments), sync_to_pipeline
        )
        return jsonify({
            "ok": True,
            "collected": len(payments),
            "saved_in_staging": True,
            "synced_to_pipeline": sync_to_pipeline,
        })

    except Exception as e:
        log.error(f"collect_official_payments seller={seller_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
