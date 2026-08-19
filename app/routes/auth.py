import secrets

from flask import Blueprint, redirect, request, flash, url_for, render_template, session

from app import config
from app.services.meli_auth import build_auth_url, exchange_code_for_tokens
from app.database import get_account_by_name

auth_bp = Blueprint("auth", __name__)


# ── Login administrativo ──────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password, config.ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            session.permanent = True
            next_url = request.form.get("next") or url_for("web.index")
            return redirect(next_url)
        flash("Senha incorreta.", "error")

    next_url = request.args.get("next", "")
    return render_template("login.html", next=next_url)


@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("auth.login"))


# ── Fluxo OAuth (público — porta de entrada para sellers externos) ───────────

@auth_bp.route("/authorize/<account_name>")
def authorize(account_name):
    account = get_account_by_name(account_name)
    if not account:
        return render_template(
            "callback_result.html",
            ok=False,
            message="Link de autorização inválido ou expirado.",
        )

    auth_url = build_auth_url(account_name)
    return redirect(auth_url)


@auth_bp.route("/callback")
def callback():
    code         = request.args.get("code")
    account_name = request.args.get("state")
    error        = request.args.get("error")

    if error:
        return render_template(
            "callback_result.html",
            ok=False,
            message="Autorização negada ou cancelada.",
        )

    if not code or not account_name:
        return render_template(
            "callback_result.html",
            ok=False,
            message="Callback inválido. Solicite um novo link.",
        )

    account = get_account_by_name(account_name)
    if not account:
        return render_template(
            "callback_result.html",
            ok=False,
            message="Conta não encontrada. Solicite um novo link.",
        )

    try:
        exchange_code_for_tokens(code, account_name)
        return render_template(
            "callback_result.html",
            ok=True,
            message="Autorização concluída com sucesso. Você já pode fechar esta janela.",
        )
    except Exception:
        return render_template(
            "callback_result.html",
            ok=False,
            message="Ocorreu um erro ao concluir a autorização. Solicite um novo link.",
        )
