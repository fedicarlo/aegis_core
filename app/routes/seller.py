"""
Blueprint do seller assinante.

Landing read-only + as 3 ações de escrita permitidas:
  - confirmar envio FULL   (registrar que mandou X unidades)
  - decidir sugestão de indução  (aprovar/rejeitar; ledger)
  - sugerir mudança de status de produto  (fica pendente de aprovação do admin)

REGRA DE ISOLAMENTO: toda escrita usa auth_policy.current_seller_id() — o
seller_id vem da SESSÃO, nunca de request.form/args/view_args. Um seller_id
mandado pelo cliente é ignorado de propósito.
"""
import hashlib

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.auth_policy import current_seller_id, current_seller_user_id
from app.database import (
    get_account_by_seller_id,
    register_full_shipment,
    create_suggestion,
    upsert_induction_decision,
    list_suggestions,
    list_induction_decisions,
)

seller_bp = Blueprint("seller", __name__)

# Estados que um seller pode PROPOR — subconjunto de costs._DIAG_STATUS_VALIDOS.
# "sem_dado_suficiente" é estado calculado, não sugerível.
_STATUS_SUGERIVEIS = frozenset({"manter", "saida_planejada", "descontinuar", "fora_regua"})


def _int_or_none(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _back():
    return redirect(request.referrer or url_for("seller.painel"))


@seller_bp.route("/painel")
def painel():
    seller_id = current_seller_id()
    account = get_account_by_seller_id(seller_id) if seller_id else None
    if not account:
        flash("Conta não encontrada. Faça login novamente.", "error")
        return redirect(url_for("auth.seller_logout"))

    return render_template(
        "seller_painel.html",
        account          = account,
        seller_id        = seller_id,
        minhas_sugestoes = list_suggestions(seller_id=seller_id, limit=50),
        minhas_decisoes  = list_induction_decisions(seller_id=seller_id, limit=50),
    )


@seller_bp.route("/painel/envio-full", methods=["POST"])
def confirm_full_shipment():
    seller_id  = current_seller_id()
    item_id    = request.form.get("item_id", "").strip()
    qty        = _int_or_none(request.form.get("quantity"))
    shipped_at = request.form.get("shipped_at", "").strip()

    if not item_id or qty is None or not shipped_at:
        flash("Informe item, quantidade e data do envio.", "error")
        return _back()

    try:
        register_full_shipment(seller_id, item_id, qty, shipped_at)
        flash(f"Envio de {qty} unidade(s) de '{item_id}' registrado.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return _back()


@seller_bp.route("/painel/inducao/decidir", methods=["POST"])
def decide_induction():
    seller_id        = current_seller_id()
    item_id          = request.form.get("item_id", "").strip()
    variation_id     = request.form.get("variation_id", "").strip()
    decisao          = request.form.get("decisao", "").strip()   # approved | rejected
    comment          = request.form.get("comment", "").strip()
    decided_qty_form = _int_or_none(request.form.get("decided_qty"))

    if decisao not in ("approved", "rejected") or not item_id:
        flash("Decisão inválida.", "error")
        return _back()

    # suggested_qty / suggestion_hash SEMPRE recalculados no servidor, com a
    # MESMA função que gera o número mostrado na tela do produto.
    from app.services.analytics import compute_all, compute_induction_enhanced
    from app.database import get_stock_history
    item = next((i for i in compute_all(seller_id) if str(i["id"]) == item_id), None)
    if not item:
        flash("Item não encontrado nesta conta.", "error")
        return _back()

    ind = compute_induction_enhanced(item, get_stock_history(seller_id, item_id, days=90))
    suggested_qty   = ind.get("qty")
    fingerprint     = (f"{suggested_qty}|{ind.get('state')}|{ind.get('giro_base')}|"
                       f"{ind.get('dias_alvo')}|{item.get('stock')}")
    suggestion_hash = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]

    if decisao == "approved":
        decided_qty = decided_qty_form if decided_qty_form is not None else suggested_qty
    else:
        decided_qty = None

    upsert_induction_decision(
        seller_id, item_id, variation_id,
        suggested_qty=suggested_qty, suggestion_hash=suggestion_hash,
        decision=decisao, decided_qty=decided_qty, comment=comment,
        decided_by=current_seller_user_id(),
    )
    flash("Decisão registrada." if decisao == "approved" else "Sugestão rejeitada.", "success")
    return _back()


@seller_bp.route("/painel/status/sugerir", methods=["POST"])
def suggest_status():
    seller_id    = current_seller_id()
    item_id      = request.form.get("item_id", "").strip()
    variation_id = request.form.get("variation_id", "").strip()
    sugestao     = request.form.get("suggested_status", "").strip().lower()
    comment      = request.form.get("comment", "").strip()

    if not item_id or sugestao not in _STATUS_SUGERIVEIS:
        flash("Selecione um item e um status válido.", "error")
        return _back()
    if not comment:
        flash("A justificativa é obrigatória.", "error")
        return _back()

    try:
        create_suggestion(seller_id, "product_status", item_id, variation_id,
                          {"suggested_status": sugestao}, comment,
                          current_seller_user_id())
        flash("Sugestão enviada — pendente de aprovação do admin.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return _back()
