"""
Etapa A — Painel de Custos usa o PREÇO EFETIVO VIGENTE na margem projetada.

Bug corrigido: a "Margem RT" (projetada) do template usava `item.price` (preço
LISTADO / "de"). Com promoção ativa isso superestima a margem. Agora usa
`price_calc = COALESCE(effective_sale_price, regular_price, price)`.

Cenário do teste (piloto Liquid Iron):
    efetivo  R$ 68,19   ·   listado/regular  R$ 74,93
    custo 30,00 · alíquota 6% · comissão 12% · frete 0
        margem c/ efetivo 68,19  → 38.0%   (correto)
        margem c/ listado 74,93  → 42.0%   (o que aparecia antes — errado)
"""
import pytest

from tests.conftest import PILOT_ITEM_ID, PILOT_SELLER_ID


@pytest.fixture
def client(db, monkeypatch):
    import app.services.scheduler as scheduler
    monkeypatch.setattr(scheduler, "start_scheduler", lambda *a, **k: None)

    from app.main import create_app
    app = create_app()
    app.config.update(TESTING=True)

    # conta + item piloto com preço efetivo já resolvido (promoção ativa)
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO accounts (name, seller_id) VALUES ('NEXORAHUB', ?) "
        "ON CONFLICT(name) DO UPDATE SET seller_id=excluded.seller_id",
        (PILOT_SELLER_ID,),
    )
    conn.execute(
        """INSERT INTO items
             (id, seller_id, title, price, status,
              regular_price, effective_sale_price, has_active_promotion,
              promotion_id, promotion_type, price_source, price_synced_at)
           VALUES (?, ?, ?, 74.93, 'active',
                   74.93, 68.19, 1,
                   'OFFER-MLB4902383615-13588277288', 'custom', 'sale_price', 1756339200)""",
        (PILOT_ITEM_ID, PILOT_SELLER_ID, "Iron Liquid True Source 30ml"),
    )
    conn.execute(
        """INSERT INTO product_costs
             (seller_id, item_id, variation_id, unit_cost, tax_rate, ml_fee_rate, shipping_cost, marca)
           VALUES (?, ?, '', 30.0, 0.06, 0.12, 0.0, 'True Source')""",
        (PILOT_SELLER_ID, PILOT_ITEM_ID),
    )
    conn.commit()
    conn.close()

    c = app.test_client()
    with c.session_transaction() as s:
        s["admin"] = True
    return c


def test_custos_page_renders_effective_price_and_projected_margin(client):
    resp = client.get(f"/custos/{PILOT_SELLER_ID}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # preço efetivo mostrado, regular riscado
    assert 'class="price-eff"' in html
    assert 'class="price-reg"' in html
    assert "R$ 68.19" in html
    assert "R$ 74.93" in html
    assert "custom" in html

    # margem projetada calculada sobre 68,19 (38.0%), NÃO sobre 74,93 (42.0%)
    assert "38.0%" in html
    assert "42.0%" not in html

    # data-price (usado pelo recálculo em JS) = preço efetivo
    assert 'data-price="68.19"' in html

    # card de resumo
    assert "Com promoção ativa" in html
    assert "Atualizar preços ML" in html


def test_custos_page_without_promo_uses_listed_price(db, client):
    """Item sem promoção resolvida: price_calc cai em price, margem sobre 74,93."""
    conn = db.get_conn()
    conn.execute("UPDATE items SET has_active_promotion=0, effective_sale_price=NULL, "
                 "regular_price=NULL WHERE id=?", (PILOT_ITEM_ID,))
    conn.commit()
    conn.close()

    resp = client.get(f"/custos/{PILOT_SELLER_ID}")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'class="price-eff"' not in html          # sem badge de promoção
    assert "42.0%" in html                          # margem volta a ser sobre 74,93
    assert 'data-price="74.93"' in html
