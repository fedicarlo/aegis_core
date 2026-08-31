"""
Etapa A — persistência do preço efetivo (app/services/price_sync.py).

`refresh_effective_prices()` só faz GET (/items/{id}/sale_price, com /prices de
fallback), resolve via pricing.resolve_effective_price e grava em `items.*`.
Nenhuma chamada real aqui: meli_api é monkeypatchado com os bodies reais do
piloto.
"""
import pytest

from app.services import price_sync
from tests.conftest import PILOT_ITEM_ID, PILOT_SELLER_ID


def _row(db, item_id):
    conn = db.get_conn()
    r = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    return dict(r)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Caminho feliz: /sale_price do piloto → grava efetivo 68,19 + promoção
# ─────────────────────────────────────────────────────────────────────────────
def test_refresh_persists_pilot_effective_price(seed_pilot_item, pilot_sale_price, monkeypatch):
    db = seed_pilot_item
    monkeypatch.setattr(price_sync.meli_api, "get_sale_price",
                        lambda token, iid, **kw: pilot_sale_price)
    monkeypatch.setattr(price_sync.meli_api, "get_item_prices",
                        lambda token, iid, **kw: pytest.fail("não deveria chamar /prices"))

    res = price_sync.refresh_effective_prices(PILOT_SELLER_ID, token="FAKE")

    assert res["checked"] == 1
    assert res["updated"] == 1
    assert res["with_promo"] == 1
    assert res["errors"] == []

    row = _row(db, PILOT_ITEM_ID)
    assert row["effective_sale_price"] == 68.19
    assert row["regular_price"] == 74.93
    assert row["has_active_promotion"] == 1
    assert row["has_promotion"] == 1                 # coluna legada mantida em sincronia
    assert row["promotion_type"] == "custom"
    assert row["promotion_id"] == "OFFER-MLB4902383615-13588277288"
    assert row["price_source"] == "sale_price"
    assert row["price_synced_at"] is not None
    assert row["price"] == 74.93                     # preço LISTADO permanece intocado


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fallback: /sale_price 404 → usa /prices
# ─────────────────────────────────────────────────────────────────────────────
def test_refresh_falls_back_to_prices_when_sale_price_missing(seed_pilot_item, pilot_prices, monkeypatch):
    db = seed_pilot_item
    monkeypatch.setattr(price_sync.meli_api, "get_sale_price",
                        lambda token, iid, **kw: None)
    monkeypatch.setattr(price_sync.meli_api, "get_item_prices",
                        lambda token, iid, **kw: pilot_prices)

    res = price_sync.refresh_effective_prices(PILOT_SELLER_ID, token="FAKE")

    assert res["with_promo"] == 1
    row = _row(db, PILOT_ITEM_ID)
    assert row["effective_sale_price"] == 68.19
    assert row["price_source"] == "prices"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Um item que estoura não derruba o lote
# ─────────────────────────────────────────────────────────────────────────────
def test_one_failing_item_does_not_break_batch(db, pilot_sale_price, monkeypatch):
    conn = db.get_conn()
    conn.executemany(
        "INSERT INTO items (id, seller_id, title, price, status) VALUES (?, ?, ?, ?, 'active')",
        [("A", "S1", "ok", 100.0), ("B", "S1", "boom", 100.0)],
    )
    conn.commit()
    conn.close()

    def fake_sale_price(token, iid, **kw):
        if iid == "B":
            raise RuntimeError("500 do ML")
        return {"amount": 80.0, "regular_amount": 100.0,
                "metadata": {"promotion_id": "P", "promotion_type": "custom"}}

    monkeypatch.setattr(price_sync.meli_api, "get_sale_price", fake_sale_price)
    monkeypatch.setattr(price_sync.meli_api, "get_item_prices", lambda *a, **k: None)

    res = price_sync.refresh_effective_prices("S1", token="FAKE")

    assert res["checked"] == 2
    assert res["updated"] == 1
    assert len(res["errors"]) == 1
    assert res["errors"][0].startswith("B:")

    conn = db.get_conn()
    a = dict(conn.execute("SELECT * FROM items WHERE id='A'").fetchone())
    conn.close()
    assert a["effective_sale_price"] == 80.0


# ─────────────────────────────────────────────────────────────────────────────
# 4. only_active: itens pausados/encerrados não são consultados
# ─────────────────────────────────────────────────────────────────────────────
def test_only_active_skips_inactive_items(db, monkeypatch):
    conn = db.get_conn()
    conn.executemany(
        "INSERT INTO items (id, seller_id, title, price, status) VALUES (?, ?, ?, ?, ?)",
        [("ON", "S1", "ativo", 50.0, "active"),
         ("OFF", "S1", "pausado", 50.0, "paused")],
    )
    conn.commit()
    conn.close()

    seen = []
    monkeypatch.setattr(price_sync.meli_api, "get_sale_price",
                        lambda token, iid, **kw: seen.append(iid) or None)
    monkeypatch.setattr(price_sync.meli_api, "get_item_prices", lambda *a, **k: None)

    res = price_sync.refresh_effective_prices("S1", token="FAKE", only_active=True)

    assert seen == ["ON"]
    assert res["checked"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. Sem promoção: efetivo == regular → grava has_active_promotion = 0
# ─────────────────────────────────────────────────────────────────────────────
def test_refresh_clears_promo_flag_when_full_price(db, monkeypatch):
    conn = db.get_conn()
    conn.execute("INSERT INTO items (id, seller_id, title, price, status, has_promotion, has_active_promotion) "
                 "VALUES ('X', 'S1', 't', 100.0, 'active', 1, 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(price_sync.meli_api, "get_sale_price",
                        lambda token, iid, **kw: {"amount": 100.0, "regular_amount": 100.0})
    monkeypatch.setattr(price_sync.meli_api, "get_item_prices", lambda *a, **k: None)

    price_sync.refresh_effective_prices("S1", token="FAKE")

    conn = db.get_conn()
    x = dict(conn.execute("SELECT * FROM items WHERE id='X'").fetchone())
    conn.close()
    assert x["has_active_promotion"] == 0
    assert x["has_promotion"] == 0
    assert x["effective_sale_price"] == 100.0
