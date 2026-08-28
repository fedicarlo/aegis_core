"""
Rotas do módulo Ads Intelligence (Etapa 8) — UI read-only.

O blueprint só resolve a conta, chama app.services.ads_view (que orquestra os
engines) e renderiza. Nenhuma lógica de diagnóstico/cálculo aqui nem no template.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.database import get_all_accounts
from app.services import ads_view

ads_bp = Blueprint("ads", __name__)

_PERIODS = (7, 14, 30, 60, 90)


def _resolve(seller_id):
    accounts = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]
    account = next((a for a in authorized if str(a["seller_id"]) == str(seller_id)), None)
    return account, authorized


def _period():
    try:
        p = int(request.args.get("period", 30))
    except (TypeError, ValueError):
        p = 30
    return p if p in _PERIODS else 30


def _since_last_change():
    return request.args.get("desde_ultima_alteracao") in ("1", "true", "on")


# ── 8a — Cockpit ───────────────────────────────────────────────────────────

@ads_bp.route("/ads/<seller_id>")
def cockpit(seller_id):
    account, authorized = _resolve(seller_id)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))
    period = _period()
    data = ads_view.cockpit(seller_id, period=period)
    return render_template("ads_cockpit.html", account=account, authorized=authorized,
                           seller_id=seller_id, period=period, periods=_PERIODS, d=data)


# ── 8b — Lista de campanhas ───────────────────────────────────────────────

@ads_bp.route("/ads/<seller_id>/campanhas")
def campanhas(seller_id):
    account, authorized = _resolve(seller_id)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))
    period = _period()
    slc = _since_last_change()
    rows = ads_view.campaign_list(seller_id, period=period, since_last_change=slc)
    return render_template("ads_campanhas.html", account=account, authorized=authorized,
                           seller_id=seller_id, period=period, periods=_PERIODS,
                           desde_ultima_alteracao=slc, rows=rows)
