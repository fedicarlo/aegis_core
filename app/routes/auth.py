from flask import Blueprint, redirect, request, flash, url_for
from app.services.meli_auth import build_auth_url, exchange_code_for_tokens
from app.database import get_account_by_name

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/authorize/<account_name>")
def authorize(account_name):
    account = get_account_by_name(account_name)
    if not account:
        flash(f"Conta '{account_name}' não encontrada.", "error")
        return redirect(url_for("web.index"))

    auth_url = build_auth_url(account_name)
    return redirect(auth_url)


@auth_bp.route("/callback")
def callback():
    code         = request.args.get("code")
    account_name = request.args.get("state")
    error        = request.args.get("error")

    if error:
        flash(f"Autorização negada para '{account_name}': {error}", "error")
        return redirect(url_for("web.index"))

    if not code or not account_name:
        flash("Callback inválido. Tente novamente.", "error")
        return redirect(url_for("web.index"))

    account = get_account_by_name(account_name)
    if not account:
        flash(f"Conta '{account_name}' não encontrada no sistema.", "error")
        return redirect(url_for("web.index"))

    try:
        exchange_code_for_tokens(code, account_name)
        flash(f"✅ '{account_name}' autorizada com sucesso!", "success")
    except Exception as e:
        flash(f"Erro ao autorizar '{account_name}': {str(e)}", "error")

    return redirect(url_for("web.index"))
