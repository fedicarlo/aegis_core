from flask import Blueprint, redirect, render_template, flash, url_for, request
from app.database import get_all_accounts, get_account_by_name, revoke_account, get_last_order_sync_hours
from app.services.meli_auth import get_valid_token
from app.services.meli_api import get_user_info
from app.services.analytics import compute_all, summary, get_promo_alerts
from app.utils.logger import get_logger

log = get_logger("routes.web")
web_bp = Blueprint("web", __name__)

SYNC_STALE_HOURS = 24  # acima disso, badge de alerta no dashboard de contas


# ── Painel principal ──────────────────────────────────────────────────────────

@web_bp.route("/")
def index():
    accounts   = get_all_accounts()
    total      = len(accounts)
    authorized = sum(1 for a in accounts if a.get("access_token"))
    pending    = total - authorized

    for account in accounts:
        if not account.get("access_token"):
            account["sync_ago_str"] = None
            account["sync_stale"] = False
            continue
        # baseado em pedidos, não em items — items.updated_at fica "fresco"
        # mesmo quando só a etapa de pedidos da coleta falha (ver VIVALEVE:
        # catálogo sincronizava normalmente enquanto pedidos ficou 12 dias parado)
        hours_ago = get_last_order_sync_hours(account["seller_id"])
        if hours_ago is None:
            account["sync_ago_str"] = "nunca sincronizada"
            account["sync_stale"] = True
        else:
            if hours_ago < 1:
                account["sync_ago_str"] = f"{int(hours_ago * 60)}min atrás"
            elif hours_ago < 24:
                account["sync_ago_str"] = f"{hours_ago:.0f}h atrás"
            else:
                account["sync_ago_str"] = f"{hours_ago / 24:.1f}d atrás"
            account["sync_stale"] = hours_ago > SYNC_STALE_HOURS

    return render_template(
        "index.html",
        accounts   = accounts,
        total      = total,
        authorized = authorized,
        pending    = pending,
    )


# ── Gerar link para enviar ao seller ─────────────────────────────────────────

@web_bp.route("/link/<account_name>")
def get_link(account_name):
    from app.services.meli_auth import build_auth_url

    account = get_account_by_name(account_name)
    if not account:
        flash(f"Conta '{account_name}' não encontrada.", "error")
        return redirect(url_for("web.index"))

    auth_url = build_auth_url(account_name)
    return render_template(
        "link.html",
        account_name = account_name,
        auth_url     = auth_url
    )


# ── Testar conexão de uma conta ───────────────────────────────────────────────

@web_bp.route("/test/<account_name>")
def test_account(account_name):
    account = get_account_by_name(account_name)

    if not account or not account["access_token"]:
        flash(f"'{account_name}' não está autorizada.", "error")
        return redirect(url_for("web.index"))

    try:
        token = get_valid_token(account)
        info  = get_user_info(token)
        flash(
            f"✅ '{account_name}' OK — MELI ID: {info['id']} | Nickname: {info.get('nickname')}",
            "success"
        )
    except Exception as e:
        flash(f"❌ Erro ao testar '{account_name}': {str(e)}", "error")

    return redirect(url_for("web.index"))


# ── Revogar autorização ───────────────────────────────────────────────────────

@web_bp.route("/revoke/<account_name>", methods=["POST"])
def revoke(account_name):
    revoke_account(account_name)
    flash(f"🔴 Autorização de '{account_name}' removida.", "info")
    return redirect(url_for("web.index"))

@web_bp.route("/exchange", methods=["GET", "POST"])
def exchange():
    from app.services.meli_auth import exchange_code_for_tokens

    accounts = get_all_accounts()

    if request.method == "POST":
        code         = request.form.get("code", "").strip()
        account_name = request.form.get("account_name", "").strip()

        if not code or not account_name:
            flash("Preencha o code e selecione a conta.", "error")
            return redirect(url_for("web.exchange"))

        try:
            exchange_code_for_tokens(code, account_name)
            flash(f"✅ '{account_name}' autorizada com sucesso!", "success")
            return redirect(url_for("web.index"))
        except Exception as e:
            flash(f"Erro: {str(e)}", "error")

    return render_template("exchange.html", accounts=accounts)
# ── Coleta de dados ───────────────────────────────────────────────────────────

@web_bp.route("/collect/<account_name>")
def collect(account_name):
    from app.services.collector import collect_account
    from app.database import init_data_tables

    init_data_tables()

    account = get_account_by_name(account_name)
    if not account or not account["access_token"]:
        flash(f"'{account_name}' não está autorizada.", "error")
        return redirect(url_for("web.index"))

    try:
        result = collect_account(account)

        msg = (
            f"✅ Coleta concluída — "
            f"{result['items_collected']} anúncios | "
            f"{result['stock_collected']} com FULL | "
            f"{result['orders_collected']} pedidos"
        )

        if result["catalog_detected"] > 0:
            msg += f" | ⚠️ {result['catalog_detected']} catálogo(s) detectado(s)"

        if result["paused_detected"] > 0:
            msg += f" | 🔴 {result['paused_detected']} pausado(s)"

        flash(msg, "success")

    except Exception as e:
        flash(f"Erro na coleta: {str(e)}", "error")

    return redirect(url_for("web.index"))


@web_bp.route("/dashboard")
def dashboard():
    import datetime as dt
    from app.services.analytics import get_alerts, build_kpis, build_chart_data
    from app.database import get_last_updated

    accounts   = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]

    account_name = request.args.get("account")
    if account_name:
        account = get_account_by_name(account_name)
    elif authorized:
        account = authorized[0]
    else:
        flash("Nenhuma conta autorizada. Autorize uma conta primeiro.", "error")
        return redirect(url_for("web.index"))

    if not account or not account["seller_id"]:
        flash("Conta sem seller_id — refaça o OAuth.", "error")
        return redirect(url_for("web.index"))

    seller_id    = account["seller_id"]
    items        = compute_all(seller_id)
    totals       = summary(items)
    alerts       = get_alerts(items)
    kpis         = build_kpis(seller_id)
    chart        = build_chart_data(seller_id)

    # Filter items by classification if ?filter= is set
    filter_class = request.args.get("filter", "").strip().lower()
    _filter_map  = {"critico": "Crítico", "oportunidade": "Oportunidade",
                    "estavel": "Estável", "descarte": "Descarte"}
    active_filter = _filter_map.get(filter_class)
    if active_filter:
        items = [i for i in items if i["classification"] == active_filter]

    ts = get_last_updated(seller_id)
    last_upd = dt.datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "—"

    # top 7 by giro_7d for the side panel
    top_items = sorted(
        [i for i in items if i["giro_7d"] > 0],
        key=lambda x: -x["giro_7d"]
    )[:7]
    max_giro = top_items[0]["giro_7d"] if top_items else 1

    # Promo alerts
    promo_alerts = get_promo_alerts(seller_id)

    # Margin summary for health card (graceful if no costs configured yet)
    margin_summary = {"negativas": 0, "abaixo_10": 0, "prejuizo_total": 0.0, "sem_custo": 0, "margem_pct": None}
    try:
        from app.services.analytics import compute_margin
        md = compute_margin(seller_id, days=30)
        margin_summary = {
            "negativas":      md["negativas"],
            "abaixo_10":      md["abaixo_10"],
            "prejuizo_total": md["prejuizo_total"],
            "sem_custo":      md["sem_custo"],
            "margem_pct":     md["totals"]["margem_pct"],
        }
    except Exception as e:
        log.debug(f"margin_summary unavailable seller={seller_id}: {e}")

    # Finance insights — alertas ativos do módulo MP (não-fatal)
    finance_alerts = []
    try:
        from app.services.finance_insights import compute_finance_alerts
        finance_alerts = compute_finance_alerts(seller_id, days=30)
    except Exception as e:
        log.debug(f"finance_alerts unavailable seller={seller_id}: {e}")

    return render_template(
        "dashboard.html",
        account        = account,
        authorized     = authorized,
        items          = items,
        totals         = totals,
        alerts         = alerts,
        promo_alerts   = promo_alerts,
        finance_alerts = finance_alerts,
        kpis           = kpis,
        chart          = chart,
        last_upd       = last_upd,
        top_items      = top_items,
        max_giro       = max_giro,
        margin_summary = margin_summary,
        active_filter  = active_filter,
    )


@web_bp.route("/collect-all")
def collect_all():
    from app.services.collector import collect_all_authorized

    try:
        results = collect_all_authorized()
        total_items  = sum(r["items_collected"] for r in results)
        total_orders = sum(r["orders_collected"] for r in results)
        flash(
            f"✅ Coleta geral — {len(results)} conta(s) | "
            f"{total_items} anúncios | {total_orders} pedidos",
            "success"
        )
    except Exception as e:
        flash(f"Erro na coleta geral: {str(e)}", "error")

    return redirect(url_for("web.index"))


# ══════════════════════════════════════════════════════════════════════════════
# Admin — credenciais de seller + fila de aprovações
# (admin-only pelo before_request; nenhum destes endpoints entra em SELLER_ALLOWED)
# ══════════════════════════════════════════════════════════════════════════════

_STATUS_SUGERIVEIS = {"manter", "saida_planejada", "descontinuar", "fora_regua"}


@web_bp.route("/admin/seller-users")
def admin_seller_users():
    from app.database import list_seller_users
    accounts = [a for a in get_all_accounts()
                if a.get("access_token") and a.get("seller_id")]
    return render_template("admin_seller_users.html",
                           users=list_seller_users(), accounts=accounts)


@web_bp.route("/admin/seller-users/create", methods=["POST"])
def admin_seller_users_create():
    from app.database import create_seller_user
    seller_id = request.form.get("seller_id", "").strip()
    email     = request.form.get("email", "").strip()
    senha     = request.form.get("password", "")
    nome      = request.form.get("nome_responsavel", "").strip()
    telefone  = request.form.get("telefone", "").strip()

    valid_ids = {str(a["seller_id"]) for a in get_all_accounts()
                 if a.get("access_token") and a.get("seller_id")}
    if seller_id not in valid_ids:
        flash("Selecione uma conta autorizada válida.", "error")
        return redirect(url_for("web.admin_seller_users"))

    try:
        create_seller_user(seller_id, email, senha, nome, telefone, created_by="admin")
        flash(f"Credencial criada para {email}.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("web.admin_seller_users"))


@web_bp.route("/admin/seller-users/<int:user_id>/reset-senha", methods=["POST"])
def admin_seller_users_reset(user_id):
    from app.database import set_seller_user_password
    try:
        set_seller_user_password(user_id, request.form.get("password", ""))
        flash("Senha redefinida.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("web.admin_seller_users"))


@web_bp.route("/admin/seller-users/<int:user_id>/toggle", methods=["POST"])
def admin_seller_users_toggle(user_id):
    from app.database import toggle_seller_user
    new_state = toggle_seller_user(user_id)
    if new_state is None:
        flash("Usuário não encontrado.", "error")
    else:
        flash(f"Login {'ativado' if new_state else 'desativado'}.", "info")
    return redirect(url_for("web.admin_seller_users"))


@web_bp.route("/admin/aprovacoes")
def admin_aprovacoes():
    from app.database import list_pending_suggestions, list_induction_decisions
    names = {str(a["seller_id"]): a["name"] for a in get_all_accounts() if a.get("seller_id")}
    pend = list_pending_suggestions()
    dec  = list_induction_decisions(limit=100)
    for s in pend:
        s["account_name"] = names.get(str(s["seller_id"]), s["seller_id"])
    for d in dec:
        d["account_name"] = names.get(str(d["seller_id"]), d["seller_id"])
    return render_template("admin_aprovacoes.html", pendencias=pend, decisions=dec)


@web_bp.route("/admin/aprovacoes/<int:suggestion_id>/resolver", methods=["POST"])
def admin_aprovacoes_resolver(suggestion_id):
    from app.database import (
        get_suggestion, resolve_suggestion, set_product_status_override,
    )
    acao = request.form.get("acao", "")            # 'aprovar' | 'rejeitar'
    nota = request.form.get("nota", "").strip()

    sug = get_suggestion(suggestion_id)
    if not sug or sug["state"] != "pending":
        flash("Sugestão não encontrada ou já resolvida.", "error")
        return redirect(url_for("web.admin_aprovacoes"))

    if acao == "aprovar":
        if sug["kind"] == "product_status":
            status = (sug["payload"].get("suggested_status") or "").strip().lower()
            if status not in _STATUS_SUGERIVEIS:
                flash("Status sugerido inválido — não aplicado.", "error")
                return redirect(url_for("web.admin_aprovacoes"))
            set_product_status_override(
                sug["seller_id"], sug["item_id"], sug["variation_id"],
                status, sug["comment"] or nota or "aprovado via sugestão do seller",
                override_by=f"seller-sugestao#{suggestion_id}")
        resolve_suggestion(suggestion_id, "approved", "admin", nota)
        flash("Sugestão aprovada e aplicada.", "success")
    elif acao == "rejeitar":
        resolve_suggestion(suggestion_id, "rejected", "admin", nota)
        flash("Sugestão rejeitada.", "info")
    else:
        flash("Ação inválida.", "error")
    return redirect(url_for("web.admin_aprovacoes"))
