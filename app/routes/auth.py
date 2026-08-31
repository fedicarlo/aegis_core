import secrets

from flask import Blueprint, redirect, request, flash, url_for, render_template, session

from app import config
from app.services.meli_auth import build_auth_url, exchange_code_for_tokens
from app.database import get_account_by_name, authenticate_seller, touch_seller_login

auth_bp = Blueprint("auth", __name__)


# ── Login (admin único OU seller assinante, mesma tela) ──────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        modo     = request.form.get("modo", "admin")
        next_url = request.form.get("next") or ""

        if modo == "seller":
            email = request.form.get("email", "")
            senha = request.form.get("password", "")
            user  = authenticate_seller(email, senha)
            if user:
                session.clear()
                session["role"]           = "seller"
                session["seller_user_id"] = user["id"]
                session["seller_id"]      = str(user["seller_id"])
                session.permanent = True
                touch_seller_login(user["id"])
                return redirect(url_for("seller.painel"))
            flash("E-mail ou senha inválidos.", "error")

        else:  # admin
            senha = request.form.get("password", "")
            if secrets.compare_digest(senha, config.ADMIN_PASSWORD):
                session.clear()
                session["role"]  = "admin"
                session["admin"] = True  # compat com sessões/checagens antigas
                session.permanent = True
                return redirect(next_url or url_for("web.index"))
            flash("Senha incorreta.", "error")

    next_url = request.args.get("next", "")
    return render_template("login.html", next=next_url)


@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/painel/sair")
def seller_logout():
    """Logout do seller — endpoint próprio pra entrar na allowlist do seller."""
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
