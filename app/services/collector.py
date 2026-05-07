import time
from app.services.meli_api import (
    get_item_ids,
    get_items_batch,
    get_full_inventory,
    get_inventory_ids_from_item,
    get_inventory_stock,
    get_orders,
    get_item_promotions,
    get_promotion_items,
    get_mp_payments,
    collect_meli_order_payments,
)
from app.services.meli_auth import get_valid_token
from app.database import (
    get_account_by_name,
    save_items,
    save_stock,
    save_orders,
    save_promotions,
    save_stock_snapshot,
    save_mp_payments,
    save_meli_order_payments,
    init_data_tables,
    init_promotions_table,
    init_history_tables,
    init_mp_tables,
    init_meli_finance_tables,
)


def collect_account(account: dict) -> dict:
    """
    Coleta todos os dados de uma conta:
    - Anúncios
    - Estoque FULL
    - Pedidos (7, 15, 30 dias)

    Retorna um resumo da coleta.
    """
    init_data_tables()
    init_history_tables()
    init_mp_tables()
    init_meli_finance_tables()

    seller_id = account["seller_id"]
    token     = get_valid_token(account)

    init_promotions_table()

    result = {
        "account":              account["name"],
        "seller_id":            seller_id,
        "items_collected":      0,
        "stock_collected":      0,
        "orders_collected":     0,
        "promotions_collected": 0,
        "payments_collected":   0,
        "official_payments_collected": 0,
        "catalog_detected":     0,
        "paused_detected":      0,
        "errors":               [],
    }

    # ── 1. Anúncios ───────────────────────────────────────────────────────────
    try:
        print(f"[{account['name']}] Coletando anúncios...")

        # Coleta ativos e pausados separadamente
        active_ids = get_item_ids(token, seller_id, status="active")
        paused_ids = get_item_ids(token, seller_id, status="paused")
        item_ids   = active_ids + paused_ids

        items = get_items_batch(token, item_ids)

        save_items(seller_id, items)
        result["items_collected"] = len(items)
        result["catalog_detected"] = sum(
            1 for i in items if "catalog_listing" in i.get("tags", [])
        )
        result["paused_detected"] = sum(
            1 for i in items if i.get("status") == "paused"
        )
        print(f"[{account['name']}] {len(items)} anúncios coletados.")

    except Exception as e:
        result["errors"].append(f"Anúncios: {str(e)}")
        print(f"[{account['name']}] ERRO anúncios: {e}")
        return result  # sem items não adianta continuar

    # ── 2. Estoque FULL ───────────────────────────────────────────────────────
    try:
        print(f"[{account['name']}] Coletando estoque FULL...")
        stock_count = 0

        for item in items:
            item_id = item.get("id")

            # Só tenta coletar FULL em itens com logística de fulfillment
            logistic = item.get("shipping", {}).get("logistic_type")
            if logistic != "fulfillment":
                continue

            try:
                available = 0

                # Tenta o endpoint correto via inventory_id (um por variação ou raiz)
                inv_ids = get_inventory_ids_from_item(item)
                if inv_ids:
                    for inv_id in inv_ids:
                        stock_data = get_inventory_stock(token, inv_id)
                        # Endpoint retorna available_quantity no nível raiz
                        if "available_quantity" in stock_data:
                            available += int(stock_data.get("available_quantity") or 0)
                        else:
                            # Formato legado com array locations
                            for loc in stock_data.get("locations", []):
                                available += loc.get("quantity", 0)
                else:
                    # Fallback: endpoint legado /items/{id}/fulfillment_stock
                    stock_data = get_full_inventory(token, item_id)
                    locations = stock_data.get("fulfillment_stock", {}).get("locations", [])
                    for loc in locations:
                        available += loc.get("available_quantity", 0)

                save_stock(seller_id, item_id, available)
                save_stock_snapshot(seller_id, item_id, available)
                stock_count += 1

            except Exception as stock_err:
                print(f"  [FULL] {item_id}: {stock_err}")

        result["stock_collected"] = stock_count
        print(f"[{account['name']}] {stock_count} itens com estoque FULL.")

    except Exception as e:
        result["errors"].append(f"Estoque: {str(e)}")
        print(f"[{account['name']}] ERRO estoque: {e}")

    # ── 3. Pedidos (60 dias) ──────────────────────────────────────────────────
    try:
        print(f"[{account['name']}] Coletando pedidos (60 dias)...")
        orders = get_orders(token, seller_id, days=60)

        save_orders(seller_id, orders)
        result["orders_collected"] = len(orders)
        print(f"[{account['name']}] {len(orders)} pedidos coletados.")

    except Exception as e:
        result["errors"].append(f"Pedidos: {str(e)}")
        print(f"[{account['name']}] ERRO pedidos: {e}")

    # ── 4. Promoções ativas ───────────────────────────────────────────────────
    try:
        print(f"[{account['name']}] Coletando promoções ativas...")
        raw_promos = get_item_promotions(token, seller_id)

        flat_promos = []
        for promo in raw_promos:
            promo_id    = promo.get("id") or promo.get("promotion_id")
            promo_type  = promo.get("type") or promo.get("promotion_type", "UNKNOWN")
            start_date  = promo.get("start_date") or promo.get("start_time")
            finish_date = promo.get("finish_date") or promo.get("finish_time")

            if not promo_id:
                continue

            promo_items = get_promotion_items(token, seller_id, str(promo_id))

            for pi in promo_items:
                item_id = pi.get("item_id") or pi.get("id")
                if not item_id:
                    continue

                original_price = float(
                    pi.get("original_price") or pi.get("regular_amount") or 0
                )
                promo_price = float(
                    pi.get("new_price") or pi.get("deal_price")
                    or pi.get("price") or 0
                )

                flat_promos.append({
                    "promotion_id":   str(promo_id),
                    "item_id":        str(item_id),
                    "promotion_type": promo_type,
                    "original_price": original_price,
                    "promo_price":    promo_price,
                    "start_date":     start_date,
                    "finish_date":    finish_date,
                })

        save_promotions(seller_id, flat_promos)
        result["promotions_collected"] = len(flat_promos)
        print(f"[{account['name']}] {len(flat_promos)} item(s) em promoção coletados.")

    except Exception as e:
        result["errors"].append(f"Promoções: {str(e)}")
        print(f"[{account['name']}] ERRO promoções: {e}")

    # ── 5. Pagamentos Mercado Pago (30 dias) ──────────────────────────────────
    try:
        print(f"[{account['name']}] Coletando pagamentos MP (30 dias)...")
        payments = get_mp_payments(token, seller_id, days=30)
        save_mp_payments(seller_id, payments)
        result["payments_collected"] = len(payments)
        print(f"[{account['name']}] {len(payments)} pagamentos MP coletados.")

    except Exception as e:
        result["errors"].append(f"Pagamentos MP: {str(e)}")
        print(f"[{account['name']}] ERRO pagamentos MP: {e}")

    # ── 6. Pagamentos oficiais via orders + payments (staging paralelo) ──────
    try:
        print(f"[{account['name']}] Coletando pagamentos oficiais ML (30 dias)...")
        official_payments = collect_meli_order_payments(token, seller_id, days=30)
        save_meli_order_payments(seller_id, official_payments)
        result["official_payments_collected"] = len(official_payments)
        print(f"[{account['name']}] {len(official_payments)} pagamentos oficiais coletados.")

    except Exception as e:
        result["errors"].append(f"Pagamentos oficiais ML: {str(e)}")
        print(f"[{account['name']}] ERRO pagamentos oficiais ML: {e}")

    return result


def collect_all_authorized() -> list:
    """
    Roda a coleta em todas as contas autorizadas.
    Retorna lista de resultados por conta.
    """
    from app.database import get_all_accounts

    accounts = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]

    if not authorized:
        print("Nenhuma conta autorizada.")
        return []

    results = []
    for account in authorized:
        print(f"\n{'='*40}")
        print(f"Coletando: {account['name']}")
        print(f"{'='*40}")
        result = collect_account(account)
        results.append(result)

    return results
