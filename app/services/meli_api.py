import requests
from app.config import MELI_API_URL
from app.utils.logger import get_logger

log = get_logger("meli_api")


class StockFetchError(Exception):
    """
    Levantado quando a API não retornou dado confiável de estoque FULL
    (404/403/etc). Nunca deve ser interpretado como estoque = 0 pelo chamador.
    """


# ── Helper base ───────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get(url: str, token: str, params: dict = None) -> dict:
    res = requests.get(url, headers=_headers(token), params=params, timeout=15)
    res.raise_for_status()
    return res.json()


# ── Usuário ───────────────────────────────────────────────────────────────────

def get_user_info(token: str) -> dict:
    """Dados básicos da conta."""
    return _get(f"{MELI_API_URL}/users/me", token)


# ── Anúncios ──────────────────────────────────────────────────────────────────

def get_item_ids(token: str, seller_id: str, status: str = None) -> list:
    """
    Retorna IDs de anúncios do seller.
    status: 'active', 'paused', None (todos)
    """
    ids    = []
    offset = 0
    limit  = 50

    while True:
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status

        data = _get(
            f"{MELI_API_URL}/users/{seller_id}/items/search",
            token,
            params=params
        )

        results = data.get("results", [])
        ids.extend(results)

        total = data.get("paging", {}).get("total", 0)
        offset += limit

        if offset >= total:
            break

    return ids

def get_item_detail(token: str, item_id: str) -> dict:
    """Detalhes completos de um anúncio."""
    return _get(f"{MELI_API_URL}/items/{item_id}", token)


def get_items_batch(token: str, item_ids: list) -> list:
    """
    Busca detalhes de até 20 anúncios por chamada (limite da API).
    Retorna lista com os dados de cada item.
    """
    results = []
    chunk_size = 20

    for i in range(0, len(item_ids), chunk_size):
        chunk = item_ids[i:i + chunk_size]
        ids_str = ",".join(chunk)

        data = _get(
            f"{MELI_API_URL}/items",
            token,
            params={"ids": ids_str}
        )

        for entry in data:
            if entry.get("code") == 200:
                results.append(entry["body"])

    return results


# ── Tarifa/comissão do anúncio ──────────────────────────────────────────────────

def get_listing_fee(token: str, item_id: str) -> dict:
    """
    Busca a comissão real do anúncio via GET /sites/{site_id}/listing_prices,
    usando preço/categoria/tipo de anúncio atuais (chamada autenticada —
    a API bloqueia sem token). Não fixa nenhum threshold de preço no código:
    a API já retorna fixed_fee/percentage_fee compostos, o que for aplicável
    pro preço atual do item.
    """
    detail = get_item_detail(token, item_id)
    site_id     = detail["site_id"]
    price       = detail["price"]

    resp = _get(f"{MELI_API_URL}/sites/{site_id}/listing_prices", token, params={
        "price":           price,
        "listing_type_id": detail["listing_type_id"],
        "category_id":     detail["category_id"],
    })

    fee_details = resp.get("sale_fee_details", {})
    return {
        "item_id":         item_id,
        "price":           price,
        "sale_fee_amount": resp.get("sale_fee_amount", 0),
        "percentage_fee":  fee_details.get("percentage_fee", 0),
        "fixed_fee":       fee_details.get("fixed_fee", 0),
    }


# ── Estoque FULL ──────────────────────────────────────────────────────────────

def get_inventory_ids_from_item(item: dict) -> list:
    """
    Extrai todos os inventory_id de um item já carregado.
    Pode estar na raiz (item sem variações) ou em cada variação.
    Retorna lista de strings (pode ser vazia).
    """
    ids = set()
    if item.get("inventory_id"):
        ids.add(item["inventory_id"])
    for v in item.get("variations", []):
        if v.get("inventory_id"):
            ids.add(v["inventory_id"])
    return list(ids)


def get_inventory_stock(token: str, inventory_id: str) -> dict:
    """
    Retorna estoque FULL via endpoint correto:
      GET /inventories/{inventory_id}/stock/fulfillment
    Resposta esperada: {"total": N, "locations": [...]}
    Levanta StockFetchError em 404/403 — NÃO retorna {} silenciosamente,
    porque isso já causou estoque sendo zerado incorretamente (404/403
    não significa estoque = 0, pode ser erro transitório/permissão).
    """
    try:
        return _get(
            f"{MELI_API_URL}/inventories/{inventory_id}/stock/fulfillment",
            token
        )
    except requests.HTTPError as e:
        if e.response.status_code in (404, 403):
            raise StockFetchError(
                f"inventory_id={inventory_id}: HTTP {e.response.status_code}"
            ) from e
        raise


def get_full_inventory(token: str, item_id: str) -> dict:
    """
    Retorna dados de estoque FULL do anúncio via endpoint legado.
    Endpoint: /items/{item_id}/fulfillment_stock
    Prefira get_inventory_stock() quando o inventory_id estiver disponível.
    Levanta StockFetchError em 404 — ver nota em get_inventory_stock().
    """
    try:
        return _get(
            f"{MELI_API_URL}/items/{item_id}/fulfillment_stock",
            token
        )
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            raise StockFetchError(f"item_id={item_id}: HTTP 404") from e
        raise


# ── Vendas / Pedidos ──────────────────────────────────────────────────────────

def get_orders(token: str, seller_id: str, days: int = None) -> list:
    """
    Retorna pedidos do seller.
    days=None retorna todos.
    days=30 retorna últimos 30 dias.
    """
    from datetime import datetime, timedelta, timezone

    orders = []
    offset = 0
    limit  = 50

    params = {
        "seller": seller_id,
        "limit":  limit,
        "offset": offset,
    }

    if days:
        date_from = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["order.date_created.from"] = date_from

    while True:
        params["offset"] = offset
        try:
            data = _get(f"{MELI_API_URL}/orders/search", token, params=params)
        except Exception as e:
            log.error(
                f"[get_orders] seller={seller_id}: falha ao buscar página "
                f"offset={offset} — {e}. Mantendo {len(orders)} pedido(s) já "
                f"coletado(s) nesta chamada em vez de descartar tudo."
            )
            break

        results = data.get("results", [])
        orders.extend(results)

        total = data.get("paging", {}).get("total", 0)
        offset += limit

        if offset >= total:
            break

    return orders


def get_order_detail(token: str, order_id: str) -> dict:
    """Retorna o payload completo de um pedido específico."""
    return _get(f"{MELI_API_URL}/orders/{order_id}", token)


def get_payments_for_order(token: str, order: dict) -> list:
    """
    Retorna os pagamentos de um pedido.
    Usa os pagamentos presentes no search; se não vierem, faz fallback no detalhe.
    """
    payments = order.get("payments") or []
    if payments:
        return payments

    order_id = order.get("id")
    if not order_id:
        return []

    detail = get_order_detail(token, str(order_id))
    return detail.get("payments") or []


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_charge_amount(charges: list, *, names: tuple = (), types: tuple = ()) -> float:
    total = 0.0
    for charge in charges or []:
        name = charge.get("name", "")
        ctype = charge.get("type", "")
        if names and name in names:
            total += _safe_float((charge.get("amounts") or {}).get("original"))
            continue
        if types and ctype in types:
            total += _safe_float((charge.get("amounts") or {}).get("original"))
    return round(total, 4)


def normalize_order_payment(order: dict, payment: dict) -> dict:
    """
    Converte order+payment do Mercado Livre para o contrato atual de mp_payments.
    Mantém o schema que o finance_pipeline já consome.
    """
    order_items = order.get("order_items") or []
    first_item = order_items[0] if order_items else {}
    item_info = first_item.get("item") or {}

    charges = payment.get("charges_details") or payment.get("fee_details") or []

    transaction_amount = _safe_float(
        payment.get("transaction_amount")
        or payment.get("total_paid_amount")
        or payment.get("total_amount")
        or order.get("total_amount")
    )

    ml_fee = _extract_charge_amount(
        charges,
        names=("ml_sale_fee", "marketplace_fee"),
        types=("sale_fee", "marketplace_fee"),
    )
    mp_fee = _extract_charge_amount(
        charges,
        types=("fee", "financial_fee", "mercadopago_fee"),
    )
    shipping_fee = _extract_charge_amount(
        charges,
        types=("shipping",),
    )

    shipping_amount = _safe_float(
        payment.get("shipping_cost")
        or payment.get("shipping_amount")
        or (order.get("shipping") or {}).get("cost")
    )

    net_received_amount = _safe_float(
        payment.get("net_received_amount")
        or (payment.get("transaction_details") or {}).get("net_received_amount")
    )
    if not net_received_amount and transaction_amount:
        net_received_amount = round(
            transaction_amount - ml_fee - mp_fee - shipping_fee,
            4,
        )

    status_detail = payment.get("status_detail", "")
    if isinstance(status_detail, dict):
        status_detail = status_detail.get("code") or status_detail.get("description") or ""

    return {
        "payment_id": str(payment.get("id", "")),
        "order_id": str(order.get("id", "")),
        "item_id": str(item_info.get("id", "")),
        "item_title": str(
            item_info.get("title")
            or first_item.get("title")
            or order.get("title")
            or ""
        )[:200],
        "status": payment.get("status") or order.get("status") or "",
        "status_detail": str(status_detail),
        "transaction_amount": transaction_amount,
        "net_received_amount": net_received_amount,
        "ml_fee": ml_fee or _safe_float(payment.get("marketplace_fee")),
        "mp_fee": mp_fee,
        "shipping_fee": shipping_fee,
        "shipping_amount": shipping_amount,
        "payment_method_id": payment.get("payment_method_id")
        or payment.get("payment_method_reference_id")
        or "",
        "payment_type_id": payment.get("payment_type")
        or payment.get("payment_type_id")
        or "",
        "installments": _safe_int(payment.get("installments"), 1) or 1,
        "date_created": payment.get("date_created") or order.get("date_created") or "",
        "date_approved": payment.get("date_approved") or "",
        "money_release_date": payment.get("money_release_date") or "",
        "money_release_status": payment.get("money_release_status") or "",
        "source": "meli_orders",
        "order_status": order.get("status", ""),
    }


def collect_meli_order_payments(token: str, seller_id: str, days: int = 30) -> list:
    """
    Coleta oficial financeira via orders do Mercado Livre e seus respectivos payments.
    Retorna registros já normalizados no mesmo schema usado por mp_payments.
    """
    rows = []
    seen = set()

    for order in get_orders(token, seller_id, days=days):
        payments = get_payments_for_order(token, order)
        for payment in payments:
            normalized = normalize_order_payment(order, payment)
            key = (seller_id, normalized["payment_id"], normalized["order_id"])
            if key in seen or not normalized["payment_id"]:
                continue
            seen.add(key)
            rows.append(normalized)

    return rows

# ── Catálogo e Promoções (monitoramento crítico) ──────────────────────────────

def get_item_promotions(token: str, seller_id: str) -> list:
    """
    Lista promoções ativas do seller.
    Retorna lista de objetos de promoção (sem itens individuais).
    """
    try:
        data = _get(
            f"{MELI_API_URL}/seller-promotions/users/{seller_id}/promotions",
            token,
            params={"status": "started"}
        )
        return data if isinstance(data, list) else data.get("results", [])
    except requests.HTTPError:
        return []


def get_promotion_items(token: str, seller_id: str, promotion_id: str) -> list:
    """
    Retorna itens de uma promoção específica com preços original e promocional.
    Endpoint: GET /seller-promotions/users/{seller_id}/promotions/{id}/items
    Lida com paginação automática.
    """
    items  = []
    offset = 0
    limit  = 100

    while True:
        try:
            data = _get(
                f"{MELI_API_URL}/seller-promotions/users/{seller_id}"
                f"/promotions/{promotion_id}/items",
                token,
                params={"limit": limit, "offset": offset},
            )
        except requests.HTTPError:
            break

        results = data if isinstance(data, list) else data.get("results", [])
        if not results:
            break

        items.extend(results)

        total   = data.get("paging", {}).get("total", 0) if isinstance(data, dict) else 0
        offset += limit
        if offset >= total:
            break

    return items

def is_catalog_item(item: dict) -> bool:
    """
    Anúncio está em catálogo somente se tiver a tag 'catalog_listing'.
    Ter catalog_product_id apenas significa que o produto existe no catálogo,
    mas não que o anúncio está sendo veiculado por lá.
    """
    return "catalog_listing" in item.get("tags", [])


def get_paused_items(items: list) -> list:
    """
    Filtra anúncios com status 'paused' de uma lista já carregada.
    """
    return [i for i in items if i.get("status") == "paused"]


# ── Concorrentes ──────────────────────────────────────────────────────────────

def _normalize_item_result(r: dict) -> dict:
    seller = r.get("seller") or {}
    rep    = seller.get("seller_reputation") or {}
    ship   = r.get("shipping") or {}
    return {
        "id":              r.get("id", ""),
        "title":           r.get("title", ""),
        "price":           float(r.get("price") or 0),
        "sold_quantity":   int(r.get("sold_quantity") or 0),
        "seller_id":       str(seller.get("id") or r.get("seller_id") or ""),
        "seller_nickname": seller.get("nickname", ""),
        "seller_rep":      rep.get("level_id", ""),
        "logistic_type":   ship.get("logistic_type", ""),
        "free_shipping":   bool(ship.get("free_shipping")),
        "is_catalog":      bool(r.get("catalog_listing")),
        "thumbnail":       r.get("thumbnail", ""),
        "permalink":       r.get("permalink", ""),
    }


def _search_market_via_catalog(token: str, query: str, limit: int) -> list:
    """
    Fallback para /sites/MLB/search quando retorna 403.
    Fluxo: /products/search → /products/{id}/items → /items batch.
    """
    try:
        prod_data = _get(
            f"{MELI_API_URL}/products/search",
            token,
            params={"site_id": "MLB", "status": "active", "q": query, "limit": 8},
        )
    except Exception:
        return []

    products = prod_data.get("results", [])
    if not products:
        return []

    # Collect item_ids + basic price info from product winners
    candidate_ids = []
    for prod in products[:6]:
        prod_id = prod.get("catalog_product_id") or prod.get("id")
        if not prod_id:
            continue
        try:
            items_data = _get(
                f"{MELI_API_URL}/products/{prod_id}/items",
                token,
                params={"site_id": "MLB", "limit": 3},
            )
            for item in items_data.get("results", []):
                iid = item.get("item_id") or item.get("id")
                if iid:
                    candidate_ids.append(iid)
        except Exception:
            continue

    if not candidate_ids:
        return []

    # Batch-fetch item details for titles, sold_quantity, seller info
    ids_str = ",".join(candidate_ids[:limit + 5])
    try:
        batch = _get(f"{MELI_API_URL}/items", token, params={"ids": ids_str})
    except Exception:
        return []

    results = []
    for entry in batch:
        if entry.get("code") == 200:
            results.append(_normalize_item_result(entry["body"]))

    return results[:limit]


def search_market(token: str, query: str, limit: int = 20) -> list:
    """
    Busca anúncios no MLB por query.
    Retorna lista normalizada com campos relevantes para análise de preços.
    Tenta /sites/MLB/search primeiro; fallback via /products/search se 403.
    """
    try:
        data = _get(
            f"{MELI_API_URL}/sites/MLB/search",
            token,
            params={"q": query, "limit": limit},
        )
        results = []
        for r in data.get("results", []):
            results.append(_normalize_item_result(r))
        return results
    except requests.HTTPError as e:
        if e.response.status_code == 403:
            return _search_market_via_catalog(token, query, limit)
        return []
    except Exception:
        return []


def search_competitors(token: str, query: str, own_seller_id: str,
                       limit: int = 10) -> list:
    """
    Retorna concorrentes para o produto, excluindo o próprio seller.
    """
    all_results = search_market(token, query, limit=limit + 5)
    return [
        r for r in all_results
        if str(r["seller_id"]) != str(own_seller_id)
    ][:limit]


# ── Mercado Pago ─────────────────────────────────────────────────────────────

MP_API_URL = "https://api.mercadopago.com"


def _mp_get(url: str, token: str, params: dict = None) -> dict:
    """GET contra a API do Mercado Pago (domínio diferente do ML)."""
    res = requests.get(url, headers=_headers(token), params=params, timeout=20)
    res.raise_for_status()
    return res.json()


def get_mp_balance(token: str, seller_id: str) -> dict:
    """
    Saldo da conta Mercado Pago.
    Testados: /v1/users/{id}/mercadopago_account/balance → 404 com token OAuth ML
              /v1/account/balance                         → 404
              /users/{id}/mercadopago_account/balance     → 403
    Nenhum endpoint de saldo está acessível com o token APP_USR do ML.
    Retorna available=False para indicar indisponibilidade na UI.
    """
    _BALANCE_UNAVAILABLE = {
        "available":         False,      # flag: endpoint não acessível
        "available_balance": None,
        "pending_balance":   None,
        "total_balance":     None,
        "error":             "Saldo indisponível — endpoint MP retorna 404/403 com token OAuth ML",
    }
    return _BALANCE_UNAVAILABLE


def _parse_payment(p: dict) -> dict:
    """Normaliza um objeto payment da API MP para o esquema interno."""
    charges = p.get("charges_details") or []

    ml_fee   = sum(c["amounts"]["original"] for c in charges
                   if c.get("name") == "ml_sale_fee")
    mp_fee   = sum(c["amounts"]["original"] for c in charges
                   if c.get("type") == "fee" and c.get("name") != "ml_sale_fee")
    ship_fee = sum(c["amounts"]["original"] for c in charges
                   if c.get("type") == "shipping")

    # Item data from additional_info
    items_info = (p.get("additional_info") or {}).get("items") or []
    item_id    = items_info[0].get("id", "")    if items_info else ""
    item_title = items_info[0].get("title", "") if items_info else (p.get("description") or "")

    td = p.get("transaction_details") or {}

    return {
        "payment_id":           str(p.get("id", "")),
        "order_id":             str((p.get("order") or {}).get("id", "")),
        "item_id":              item_id,
        "item_title":           item_title[:200],
        "status":               p.get("status", ""),
        "status_detail":        p.get("status_detail", ""),
        "operation_type":       p.get("operation_type", ""),
        "transaction_amount":   float(p.get("transaction_amount") or 0),
        "net_received_amount":  float(td.get("net_received_amount") or 0),
        "ml_fee":               round(ml_fee, 4),
        "mp_fee":               round(mp_fee, 4),
        "shipping_fee":         round(ship_fee, 4),
        "shipping_amount":      float(p.get("shipping_amount") or 0),
        "payment_method_id":    p.get("payment_method_id", ""),
        "payment_type_id":      p.get("payment_type_id", ""),
        "installments":         int(p.get("installments") or 1),
        "date_created":         p.get("date_created", ""),
        "date_approved":        p.get("date_approved", ""),
        "money_release_date":   p.get("money_release_date", ""),
        "money_release_status": p.get("money_release_status", ""),
    }


# Tipos de operação que representam recebimento de VENDA no ML
_SALE_OPERATION_TYPES = {"regular_payment"}

# Tipos de operação que NÃO são venda (para seção "Outras Movimentações")
_OTHER_OPERATION_TYPES = {
    "payment", "recurring_payment", "account_fund", "refund",
    "pos_payment", "investment_payment", "withdrawal",
    "mercadopago_saving", "money_transfer_out",
}


def _is_ml_sale_payment(p: dict, seller_id: str) -> bool:
    """
    Retorna True somente se o pagamento representa RECEBIMENTO DE VENDA:
      - operation_type == "regular_payment" (venda normal no ML)
      - collector.id == seller_id (o seller é o vendedor, não o comprador)
    """
    operation_type = p.get("operation_type", "")
    if operation_type not in _SALE_OPERATION_TYPES:
        return False
    collector_id = str((p.get("collector") or {}).get("id", "") or "")
    return collector_id == str(seller_id)


def _parse_other_movement(p: dict) -> dict:
    """Normaliza um pagamento não-venda para a tabela de outras movimentações."""
    td = p.get("transaction_details") or {}
    return {
        "payment_id":         str(p.get("id", "")),
        "operation_type":     p.get("operation_type", ""),
        "status":             p.get("status", ""),
        "transaction_amount": float(p.get("transaction_amount") or 0),
        "net_amount":         float(td.get("net_received_amount") or 0),
        "description":        str(p.get("description") or "")[:200],
        "payment_method_id":  p.get("payment_method_id", ""),
        "date_created":       p.get("date_created", ""),
        "date_approved":      p.get("date_approved", ""),
        "payer_id":           str((p.get("payer") or {}).get("id", "") or ""),
        "collector_id":       str((p.get("collector") or {}).get("id", "") or ""),
    }


def get_mp_payments(token: str, seller_id: str, days: int = 30) -> list:
    """
    Busca pagamentos do Mercado Pago para o seller, paginando automaticamente.
    Retorna apenas pagamentos de VENDA (operation_type=regular_payment, collector=seller).
    Use get_mp_other_movements() para operações não-venda.
    """
    sales, _ = _fetch_mp_payments_split(token, seller_id, days)
    return sales


def get_mp_other_movements(token: str, seller_id: str, days: int = 30) -> list:
    """
    Retorna movimentações que NÃO são vendas (compras, transferências, etc.).
    """
    _, others = _fetch_mp_payments_split(token, seller_id, days)
    return others


def _fetch_mp_payments_split(token: str, seller_id: str, days: int) -> tuple:
    """
    Faz uma única passagem na API MP e separa em:
      sales  — recebimentos de venda (operation_type=regular_payment, collector=seller)
      others — demais movimentações
    """
    from datetime import datetime, timedelta, timezone

    begin = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    sales  = []
    others = []
    offset = 0
    limit  = 100

    while True:
        try:
            data = _mp_get(
                f"{MP_API_URL}/v1/payments/search",
                token,
                params={
                    "range":      "date_created",
                    "begin_date": begin,
                    "end_date":   end,
                    "limit":      limit,
                    "offset":     offset,
                },
            )
        except Exception as e:
            print(f"[MP] payments/search error (offset={offset}): {e}")
            break

        results = data.get("results") or []
        for p in results:
            if _is_ml_sale_payment(p, seller_id):
                sales.append(_parse_payment(p))
            else:
                others.append(_parse_other_movement(p))

        total   = (data.get("paging") or {}).get("total", 0)
        offset += limit
        if offset >= total:
            break

    return sales, others


# ── Visitas ────────────────────────────────────────────────────────────────────

def get_item_visits(token: str, item_id: str, days: int = 30) -> dict:
    """
    Retorna visitas do anúncio em janela de tempo.
    Endpoint: GET /items/{item_id}/visits/time_window?last=N&unit=day
    Retorna {} em caso de 404/403.
    """
    try:
        return _get(
            f"{MELI_API_URL}/items/{item_id}/visits/time_window",
            token,
            params={"last": days, "unit": "day"}
        )
    except requests.HTTPError as e:
        if e.response.status_code in (404, 403):
            return {}
        raise
