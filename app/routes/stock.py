from flask import Blueprint, render_template, redirect, url_for, flash, request
from app.database import (
    get_account_by_seller_id, init_stock_own_tables,
    get_consolidated_stock, set_stock_own, register_full_shipment,
    reconcile_pending_shipments, get_shipments_history,
)

stock_bp = Blueprint("stock", __name__)


def _require_seller(seller_id: str):
    init_stock_own_tables()
    return get_account_by_seller_id(seller_id)


@stock_bp.route("/estoque/<seller_id>")
def estoque(seller_id):
    account = _require_seller(seller_id)
    if not account:
        flash("Conta não encontrada.", "error")
        return redirect(url_for("web.index"))

    reconcile_pending_shipments(seller_id)
    items     = get_consolidated_stock(seller_id)
    shipments = get_shipments_history(seller_id)
    valor_total = sum(i.get("valor_estoque") or 0 for i in items)

    return render_template(
        "estoque.html",
        account     = account,
        items       = items,
        shipments   = shipments,
        valor_total = valor_total,
        seller_id   = seller_id,
    )


@stock_bp.route("/estoque/<seller_id>/entrada", methods=["POST"])
def entrada_estoque(seller_id):
    account = _require_seller(seller_id)
    if not account:
        flash("Conta não encontrada.", "error")
        return redirect(url_for("web.index"))

    item_id = request.form.get("item_id", "").strip()
    qty_raw = request.form.get("available_qty", "").strip()

    if not item_id or not qty_raw:
        flash("Informe item e quantidade.", "error")
        return redirect(url_for("stock.estoque", seller_id=seller_id))

    try:
        qty = int(qty_raw)
    except ValueError:
        flash("Quantidade inválida.", "error")
        return redirect(url_for("stock.estoque", seller_id=seller_id))

    if qty < 0:
        flash("Quantidade não pode ser negativa.", "error")
        return redirect(url_for("stock.estoque", seller_id=seller_id))

    set_stock_own(seller_id, item_id, qty)
    flash(f"Estoque próprio de '{item_id}' ajustado para {qty}.", "success")
    return redirect(url_for("stock.estoque", seller_id=seller_id))


@stock_bp.route("/estoque/<seller_id>/envio-full", methods=["POST"])
def envio_full(seller_id):
    account = _require_seller(seller_id)
    if not account:
        flash("Conta não encontrada.", "error")
        return redirect(url_for("web.index"))

    item_id    = request.form.get("item_id", "").strip()
    qty_raw    = request.form.get("quantity", "").strip()
    shipped_at = request.form.get("shipped_at", "").strip()

    if not item_id or not qty_raw or not shipped_at:
        flash("Informe item, quantidade e data do envio.", "error")
        return redirect(url_for("stock.estoque", seller_id=seller_id))

    try:
        qty = int(qty_raw)
    except ValueError:
        flash("Quantidade inválida.", "error")
        return redirect(url_for("stock.estoque", seller_id=seller_id))

    try:
        register_full_shipment(seller_id, item_id, qty, shipped_at)
        flash(f"Envio de {qty} unidade(s) de '{item_id}' registrado — debitado do estoque próprio.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("stock.estoque", seller_id=seller_id))
