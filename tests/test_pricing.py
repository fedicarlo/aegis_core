"""
Etapa A — resolvedor puro do PREÇO EFETIVO VIGENTE (app/services/pricing.py).

Piloto: Iron Liquid True Source 30 ml (MLB4902383615 / NEXORAHUB).
Retorno real e sanitizado da API (somente GET) — ver tests/fixtures/liquid_iron_pilot.json:

  GET /items/MLB4902383615/sale_price
    amount          = 68.19   (preço vencedor efetivo)
    regular_amount  = 74.93   (preço regular / "de")
    metadata.promotion_id   = "OFFER-MLB4902383615-13588277288"
    metadata.promotion_type = "custom"

  Achado da auditoria: o painel do ML exibia R$ 71,18 como se fosse o preço
  vigente. R$ 71,18 é o `max_discounted_price` de uma oferta DEAL "9.9" ainda
  em estado CANDIDATE (não aplicada). O efetivo real é R$ 68,19.
"""
from datetime import datetime, timezone

import pytest

from app.services import pricing


# ─────────────────────────────────────────────────────────────────────────────
# 1. Piloto real: /sale_price resolve efetivo 68,19 (NÃO 74,93, NÃO 71,18)
# ─────────────────────────────────────────────────────────────────────────────
def test_pilot_sale_price_resolves_effective_68_19(pilot_sale_price, pilot_item_detail):
    r = pricing.resolve_effective_price(
        item_detail=pilot_item_detail,
        sale_price=pilot_sale_price,
    )

    assert r["effective_sale_price"] == 68.19
    assert r["regular_price"] == 74.93
    assert r["has_active_promotion"] is True
    assert r["price_source"] == pricing.PRICE_SOURCE_SALE_PRICE
    assert r["promotion_id"] == "OFFER-MLB4902383615-13588277288"
    assert r["promotion_type"] == "custom"
    assert r["discount_amount"] == 6.74
    assert 8.9 < r["discount_pct"] < 9.1          # 6.74 / 74.93 ≈ 8.99 %
    assert r["effective_sale_price"] != 71.18     # oferta candidata nunca vira efetivo
    assert r["effective_sale_price"] != r["regular_price"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Sem /sale_price nem /prices → NÃO inventa promoção a partir de candidatas.
#    (o resolvedor não consome /seller-promotions/items na Etapa A; garante que
#     71,18 / 66,14 / 64,78 das ofertas CANDIDATE jamais entram como preço)
# ─────────────────────────────────────────────────────────────────────────────
def test_candidate_offers_never_become_effective_price(pilot_item_detail):
    r = pricing.resolve_effective_price(
        item_detail=pilot_item_detail,
        sale_price=None,
        prices=None,
    )

    assert r["effective_sale_price"] == 74.93          # cai no preço listado
    assert r["has_active_promotion"] is False
    assert r["price_source"] == pricing.PRICE_SOURCE_ITEMS_FALLBACK
    assert r["promotion_id"] is None
    assert r["promotion_type"] is None
    assert r["discount_amount"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fallback: sem /sale_price, /prices tem a entrada type=promotion vigente
# ─────────────────────────────────────────────────────────────────────────────
def test_prices_fallback_uses_active_promotion_entry(pilot_prices, pilot_item_detail):
    now = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)   # dentro da vigência
    r = pricing.resolve_effective_price(
        item_detail=pilot_item_detail,
        sale_price=None,
        prices=pilot_prices,
        now=now,
    )

    assert r["effective_sale_price"] == 68.19
    assert r["regular_price"] == 74.93
    assert r["has_active_promotion"] is True
    assert r["price_source"] == pricing.PRICE_SOURCE_PRICES
    assert r["promo_start"] == "2026-08-18T03:00:00Z"
    assert r["promo_end"] == "2026-09-18T02:59:59Z"


# ─────────────────────────────────────────────────────────────────────────────
# 4. /prices com promoção fora de vigência (ainda não começou) → ignorada
# ─────────────────────────────────────────────────────────────────────────────
def test_prices_promotion_not_yet_started_is_ignored():
    prices = {
        "prices": [
            {"id": "1", "type": "standard", "amount": 100.0, "regular_amount": None,
             "conditions": {"start_time": None, "end_time": None}},
            {"id": "9", "type": "promotion", "amount": 80.0, "regular_amount": 100.0,
             "conditions": {"start_time": "2030-01-01T00:00:00Z",
                            "end_time": "2030-02-01T00:00:00Z"}},
        ]
    }
    r = pricing.resolve_effective_price(
        item_detail={"price": 100.0}, sale_price=None, prices=prices,
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert r["has_active_promotion"] is False
    assert r["effective_sale_price"] == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# 5. /sale_price sem desconto (amount == regular_amount) → sem promoção,
#    e id/tipo de promoção NÃO são expostos
# ─────────────────────────────────────────────────────────────────────────────
def test_sale_price_at_full_price_has_no_promotion():
    sale_price = {
        "amount": 74.93, "regular_amount": 74.93, "currency_id": "BRL",
        "metadata": {"promotion_id": "X", "promotion_type": "custom"},
    }
    r = pricing.resolve_effective_price(item_detail={"price": 74.93}, sale_price=sale_price)

    assert r["has_active_promotion"] is False
    assert r["discount_amount"] == 0.0
    assert r["discount_pct"] == 0.0
    assert r["promotion_id"] is None
    assert r["promotion_type"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tipo de promoção desconhecido NÃO é descartado (regra da auditoria)
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_promotion_type_is_preserved_verbatim():
    sale_price = {
        "amount": 60.0, "regular_amount": 100.0,
        "metadata": {"promotion_id": "NEW-1", "promotion_type": "SOME_FUTURE_TYPE"},
    }
    r = pricing.resolve_effective_price(item_detail={"price": 100.0}, sale_price=sale_price)

    assert r["has_active_promotion"] is True
    assert r["promotion_type"] == "SOME_FUTURE_TYPE"
    assert r["promotion_id"] == "NEW-1"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Sem dado nenhum → tudo None, sem promoção, sem exceção
# ─────────────────────────────────────────────────────────────────────────────
def test_no_data_at_all_returns_none_fields():
    r = pricing.resolve_effective_price()
    assert r["regular_price"] is None
    assert r["effective_sale_price"] is None
    assert r["has_active_promotion"] is False
    assert r["discount_amount"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 8. Tolerância de 1 centavo: diferença < _EPS não conta como promoção
# ─────────────────────────────────────────────────────────────────────────────
def test_sub_cent_difference_is_not_a_promotion():
    sale_price = {"amount": 74.928, "regular_amount": 74.93}
    r = pricing.resolve_effective_price(item_detail={"price": 74.93}, sale_price=sale_price)
    assert r["has_active_promotion"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 9. regular_price vem de /sale_price.regular_amount mesmo quando /items
#    manda um base_price divergente (fonte canônica manda)
# ─────────────────────────────────────────────────────────────────────────────
def test_regular_price_prefers_sale_price_regular_amount():
    r = pricing.resolve_effective_price(
        item_detail={"price": 999.0, "base_price": 999.0},
        sale_price={"amount": 68.19, "regular_amount": 74.93},
    )
    assert r["regular_price"] == 74.93
    assert r["discount_amount"] == 6.74
