"""
Infra de teste do módulo custos/promoções (Etapa A — criada do zero).

- Banco isolado: cada sessão de teste usa um SQLite temporário próprio
  (`DB_PATH` é apontado para um arquivo em tmp ANTES de qualquer import de
  `app.*`, então todo `from app.config import DB_PATH` já pega o caminho fake).
- Nenhuma chamada real à API do ML: os testes que exercem `price_sync`
  monkeypatcham `app.services.meli_api.*`.
- Fixtures de payload = respostas REAIS e sanitizadas capturadas do piloto
  Liquid Iron 30 ml (MLB4902383615 / seller NEXORAHUB 1109903460), somente GET,
  salvas em `tests/fixtures/liquid_iron_pilot.json`.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

# ── DB isolado ANTES de importar qualquer coisa de app.* ─────────────────────
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="aegis-tests-"), "aegis_test.db")
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("AUTO_SYNC_ENABLED", "false")
os.environ.setdefault("ADS_SYNC_ENABLED", "false")

FIXTURES = Path(__file__).parent / "fixtures"

PILOT_SELLER_ID = "1109903460"          # NEXORAHUB
PILOT_ITEM_ID = "MLB4902383615"         # Iron Liquid True Source 30 ml


def _load_pilot():
    with open(FIXTURES / "liquid_iron_pilot.json", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def pilot_raw():
    """JSON completo do probe (todas as rotas capturadas)."""
    return _load_pilot()


@pytest.fixture
def pilot_sale_price(pilot_raw):
    """Body real de GET /items/MLB4902383615/sale_price."""
    return json.loads(json.dumps(pilot_raw["sale_price"]["body"]))


@pytest.fixture
def pilot_prices(pilot_raw):
    """Body real de GET /items/MLB4902383615/prices."""
    return json.loads(json.dumps(pilot_raw["prices"]["body"]))


@pytest.fixture
def pilot_item_detail(pilot_raw):
    """Body real de GET /items/MLB4902383615 (atributos de preço)."""
    return json.loads(json.dumps(pilot_raw["items_detail"]["body"]))


@pytest.fixture
def pilot_promo_items(pilot_raw):
    """Body real de GET /seller-promotions/items/MLB4902383615?app_version=v2
    (lista com ofertas CANDIDATE + a PRICE_DISCOUNT started)."""
    return json.loads(json.dumps(pilot_raw["promo_item_v2"]["body"]))


# ── Banco temporário inicializado ───────────────────────────────────────────
@pytest.fixture
def db(tmp_path, monkeypatch):
    """
    Cada teste ganha um arquivo SQLite próprio e novo (isolamento total —
    conexão vazada por um teste não trava o próximo). `app.database.DB_PATH` é
    repontado; todas as funções de DB leem essa global.
    Devolve o módulo `app.database`.
    """
    import app.database as database

    db_file = str(tmp_path / "aegis_test.db")
    monkeypatch.setattr(database, "DB_PATH", db_file)

    database.init_db()            # accounts (+ seed de ACCOUNTS)
    database.init_data_tables()   # items (+ colunas de preço efetivo da Etapa A)
    database.init_costs_table()   # product_costs / seller_defaults
    yield database


@pytest.fixture
def seed_pilot_item(db):
    """Insere o item piloto em `items` com o preço LISTADO (74.93), sem promoção
    resolvida ainda — estado equivalente ao pós-coleta, pré-refresh."""
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO accounts (name, seller_id) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET seller_id=excluded.seller_id",
        ("NEXORAHUB", PILOT_SELLER_ID),
    )
    conn.execute(
        "INSERT INTO items (id, seller_id, title, price, status) VALUES (?, ?, ?, ?, ?)",
        (PILOT_ITEM_ID, PILOT_SELLER_ID,
         "Iron Liquid True Source Lipofer Ferro 30ml", 74.93, "active"),
    )
    conn.commit()
    conn.close()
    return db
