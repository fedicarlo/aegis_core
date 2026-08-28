"""
Rotas do módulo Ads Intelligence (Etapa 8) — UI read-only.

O blueprint só resolve a conta, chama app.services.ads_view (que orquestra os
engines) e renderiza. Nenhuma lógica de diagnóstico/cálculo aqui nem no template.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.database import get_all_accounts
from app.services import ads_experiments, ads_view

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


# ── 8c — Detalhe de campanha ──────────────────────────────────────────────

@ads_bp.route("/ads/<seller_id>/campanha/<int:campaign_id>")
def campanha(seller_id, campaign_id):
    account, authorized = _resolve(seller_id)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))
    period = _period()
    slc = _since_last_change()
    d = ads_view.campaign_detail(seller_id, campaign_id, period=period, since_last_change=slc)
    if not d:
        flash("Campanha não encontrada.", "error")
        return redirect(url_for("ads.campanhas", seller_id=seller_id))
    return render_template("ads_campanha_detalhe.html", account=account, authorized=authorized,
                           seller_id=seller_id, period=period, periods=_PERIODS,
                           desde_ultima_alteracao=slc, d=d)


# ── 8d — Análise por SKU / Ad Group ──────────────────────────────────────

@ads_bp.route("/ads/<seller_id>/ad-group/<int:ad_group_id>")
def ad_group(seller_id, ad_group_id):
    account, authorized = _resolve(seller_id)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))
    period = _period()
    slc = _since_last_change()
    d = ads_view.ad_group_detail(seller_id, ad_group_id, period=period, since_last_change=slc)
    if not d:
        flash("Ad group não encontrado.", "error")
        return redirect(url_for("ads.campanhas", seller_id=seller_id))
    return render_template("ads_ad_group_detalhe.html", account=account, authorized=authorized,
                           seller_id=seller_id, period=period, periods=_PERIODS,
                           desde_ultima_alteracao=slc, d=d)


# ── 8e — Experimentos ───────────────────────────────────────────────────────

def _scope_target(form):
    raw = form.get("alvo", "")
    scope, _, tid = raw.partition(":")
    return (scope or None), (tid or None)


@ads_bp.route("/ads/<seller_id>/experimentos", methods=["GET", "POST"])
def experimentos(seller_id):
    account, authorized = _resolve(seller_id)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))

    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "experimento":
            scope, tid = _scope_target(request.form)
            hip = (request.form.get("hipotese") or "").strip()
            if not (scope and tid and hip):
                flash("Hipótese e alvo são obrigatórios.", "error")
            else:
                xid = ads_experiments.create(
                    seller_id, scope, tid, hip,
                    intervencao=request.form.get("intervencao") or None,
                    janela_inicio=request.form.get("janela_inicio") or None,
                    janela_fim=request.form.get("janela_fim") or None)
                flash(f"Experimento #{xid} criado.", "success")
                return redirect(url_for("ads.experimento", seller_id=seller_id, experiment_id=xid))
        elif acao == "evento":
            scope, tid = _scope_target(request.form)
            field = (request.form.get("field") or "").strip()
            if not (scope and tid and field):
                flash("Alvo e campo são obrigatórios.", "error")
            else:
                ads_experiments.record_manual_event(
                    seller_id, scope, tid, field,
                    old_value=request.form.get("old_value") or None,
                    new_value=request.form.get("new_value") or None,
                    author=request.form.get("author") or "—",
                    motivo=request.form.get("motivo") or None,
                    hipotese=request.form.get("hipotese_ev") or None)
                flash("Evento registrado na linha do tempo do alvo.", "success")
        return redirect(url_for("ads.experimentos", seller_id=seller_id))

    data = ads_view.experiments_page(seller_id)
    return render_template("ads_experimentos.html", account=account, authorized=authorized,
                           seller_id=seller_id, d=data)


@ads_bp.route("/ads/<seller_id>/experimento/<int:experiment_id>", methods=["GET", "POST"])
def experimento(seller_id, experiment_id):
    account, authorized = _resolve(seller_id)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))

    if request.method == "POST":
        patch = {}
        for k in ("intervencao", "janela_fim", "conclusao", "status"):
            v = request.form.get(k)
            if v is not None and v != "":
                patch[k] = v
        if patch:
            ads_experiments.update(experiment_id, **patch)
        if request.form.get("recomputar"):
            ads_experiments.evaluate(experiment_id, persist=True)
        flash("Experimento atualizado.", "success")
        return redirect(url_for("ads.experimento", seller_id=seller_id, experiment_id=experiment_id))

    d = ads_view.experiment_detail(seller_id, experiment_id)
    if not d:
        flash("Experimento não encontrado.", "error")
        return redirect(url_for("ads.experimentos", seller_id=seller_id))
    return render_template("ads_experimento_detalhe.html", account=account, authorized=authorized,
                           seller_id=seller_id, d=d)
