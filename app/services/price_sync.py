"""
Atualiza o preço efetivo vigente (`items.effective_sale_price` etc.) a partir da
API oficial do ML. Somente GET.

Etapa A: chamado sob demanda pela rota POST /custos/<seller>/refresh-precos.
Etapa B: será plugado no collector.collect_account (passo 1) para rodar na sync.
"""
from app.database import get_conn, get_account_by_seller_id, update_item_effective_price
from app.services import meli_api, pricing
from app.services.meli_auth import get_valid_token
from app.utils.logger import get_logger

log = get_logger("price_sync")


def _items_to_check(seller_id, only_active, item_ids):
    conn = get_conn()
    sql = "SELECT id, price, status FROM items WHERE seller_id = ?"
    args = [seller_id]
    if item_ids:
        ph = ", ".join("?" for _ in item_ids)
        sql += f" AND id IN ({ph})"
        args += list(item_ids)
    elif only_active:
        sql += " AND status = 'active'"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def refresh_effective_prices(seller_id, *, token=None, only_active=True,
                             item_ids=None, use_prices_fallback=True):
    """
    Para cada item (ativos por padrão): GET /items/{id}/sale_price (+ /prices como
    fallback), resolve o preço efetivo e grava em items.*.

    Retorna {seller_id, checked, updated, with_promo, unchanged, errors:[...]}.
    """
    if token is None:
        acc = get_account_by_seller_id(seller_id)
        if not acc or not acc.get("access_token"):
            return {"seller_id": seller_id, "checked": 0, "updated": 0,
                    "with_promo": 0, "errors": ["conta sem token"]}
        token = get_valid_token(acc)

    items = _items_to_check(seller_id, only_active, item_ids)
    r = {"seller_id": seller_id, "checked": 0, "updated": 0,
         "with_promo": 0, "unchanged": 0, "errors": []}

    for it in items:
        iid = it["id"]
        r["checked"] += 1
        try:
            sp = meli_api.get_sale_price(token, iid)
            prices = None
            if sp is None and use_prices_fallback:
                prices = meli_api.get_item_prices(token, iid)
            resolved = pricing.resolve_effective_price(
                item_detail={"price": it.get("price"), "base_price": it.get("price")},
                sale_price=sp, prices=prices,
            )
            if update_item_effective_price(seller_id, iid, resolved):
                r["updated"] += 1
            else:
                r["unchanged"] += 1
            if resolved["has_active_promotion"]:
                r["with_promo"] += 1
        except Exception as e:  # noqa: BLE001 — um item não pode derrubar o lote
            r["errors"].append(f"{iid}: {e}")
            log.warning(f"[{seller_id}] refresh preço {iid} falhou: {e}")

    log.info(f"[{seller_id}] refresh preços efetivos — {r['updated']}/{r['checked']} "
             f"atualizado(s), {r['with_promo']} com promoção, {len(r['errors'])} erro(s)")
    return r
