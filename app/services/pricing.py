"""
Resolução do PREÇO EFETIVO VIGENTE de um anúncio do Mercado Livre.

Fonte canônica: GET /items/{id}/sale_price
  - amount          = preço vencedor efetivo (o que o comprador paga)
  - regular_amount  = preço regular quando há promoção
  - metadata.promotion_id / promotion_type  (promotion_type pode ser 'custom' ou
    qualquer valor novo — NUNCA descartar tipo desconhecido)

Complemento: GET /items/{id}/prices  (entrada type='promotion' vigente).
NUNCA usar price / base_price / original_price de GET /items como base de cálculo
(documentação do ML indica descontinuação para esse fim). Eles entram só como
último fallback quando /sale_price e /prices não respondem.

Este módulo é puro (sem I/O) — recebe os payloads já buscados e devolve o dict
normalizado que o painel de custos e a persistência consomem.
"""
from datetime import datetime, timezone

PRICE_SOURCE_SALE_PRICE = "sale_price"
PRICE_SOURCE_PRICES = "prices"
PRICE_SOURCE_ITEMS_FALLBACK = "items_fallback"

_EPS = 0.005  # tolerância de centavo p/ comparar preços


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # descarta NaN
    except (TypeError, ValueError):
        return None


def _parse_dt(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except (ValueError, TypeError):
        return None


def _promotion_entry_active(entry, now):
    """True se a entrada type='promotion' de /prices está vigente em `now`."""
    cond = entry.get("conditions") or {}
    start = _parse_dt(cond.get("start_time"))
    end = _parse_dt(cond.get("end_time"))
    if start and now < start:
        return False
    if end and now > end:
        return False
    return True


def resolve_effective_price(item_detail=None, sale_price=None, prices=None, now=None):
    """
    Retorna:
      {
        regular_price, effective_sale_price,
        has_active_promotion, promotion_id, promotion_type,
        price_source, promo_start, promo_end,
        discount_amount, discount_pct,
        reference_date            # /sale_price.reference_date, se houver
      }
    Todos os campos numéricos podem ser None se não houver dado nenhum.
    """
    item_detail = item_detail or {}
    sale_price = sale_price or {}
    now = now or datetime.now(timezone.utc)

    sp_amount = _num(sale_price.get("amount"))
    sp_regular = _num(sale_price.get("regular_amount"))
    it_base = _num(item_detail.get("base_price"))
    it_price = _num(item_detail.get("price"))

    # ── regular_price: /sale_price.regular_amount → base_price → price ──────────
    regular_price = sp_regular or it_base or it_price

    # ── effective + fonte ────────────────────────────────────────────────────
    effective = None
    source = None
    promotion_id = None
    promotion_type = None
    promo_start = None
    promo_end = None

    if sp_amount is not None:
        effective = sp_amount
        source = PRICE_SOURCE_SALE_PRICE
        meta = sale_price.get("metadata") or {}
        promotion_id = meta.get("promotion_id")
        promotion_type = meta.get("promotion_type")

    if effective is None and prices:
        entries = (prices.get("prices") if isinstance(prices, dict) else prices) or []
        promo_entries = [
            e for e in entries
            if str(e.get("type")).lower() == "promotion" and _promotion_entry_active(e, now)
        ]
        if promo_entries:
            # se houver mais de uma, a de amount menor (mais agressiva) vence
            e = min(promo_entries, key=lambda x: _num(x.get("amount")) or 1e18)
            effective = _num(e.get("amount"))
            source = PRICE_SOURCE_PRICES
            promotion_id = promotion_id or e.get("id")
            promotion_type = promotion_type or e.get("type")
            cond = e.get("conditions") or {}
            promo_start = cond.get("start_time")
            promo_end = cond.get("end_time")
            if regular_price is None:
                regular_price = _num(e.get("regular_amount"))

    if effective is None:
        effective = regular_price
        source = PRICE_SOURCE_ITEMS_FALLBACK

    # ── promoção ativa? ──────────────────────────────────────────────────────
    has_promo = bool(
        effective is not None and regular_price is not None
        and effective < regular_price - _EPS
    )
    if not has_promo:
        # preço cheio: não expõe id/tipo de promoção
        promotion_id = None
        promotion_type = None
        promo_start = None
        promo_end = None

    discount_amount = round(regular_price - effective, 2) if (has_promo) else 0.0
    discount_pct = (
        round(discount_amount / regular_price * 100, 2)
        if (has_promo and regular_price) else 0.0
    )

    return {
        "regular_price": round(regular_price, 2) if regular_price is not None else None,
        "effective_sale_price": round(effective, 2) if effective is not None else None,
        "has_active_promotion": has_promo,
        "promotion_id": promotion_id,
        "promotion_type": promotion_type,
        "price_source": source,
        "promo_start": promo_start,
        "promo_end": promo_end,
        "discount_amount": discount_amount,
        "discount_pct": discount_pct,
        "reference_date": sale_price.get("reference_date"),
    }
