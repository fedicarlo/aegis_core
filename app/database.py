import re
import sqlite3
import time
from app.config import DB_PATH, ACCOUNTS
from app.utils.logger import get_logger as _get_logger

_orders_log = _get_logger("database.orders")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE,
            seller_id     TEXT,
            nickname      TEXT,
            access_token  TEXT,
            refresh_token TEXT,
            expires_at    INTEGER,
            authorized_at INTEGER,
            active        INTEGER DEFAULT 1
        )
    """)

    for name in ACCOUNTS:
        c.execute(
            "INSERT OR IGNORE INTO accounts (name) VALUES (?)",
            (name,)
        )

    conn.commit()
    conn.close()


def get_all_accounts():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_account_by_name(name: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM accounts WHERE name = ?", (name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_account_by_seller_id(seller_id: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM accounts WHERE seller_id = ?", (seller_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_tokens(name: str, seller_id: str, nickname: str,
                access_token: str, refresh_token: str, expires_in: int):
    expires_at = int(time.time()) + expires_in
    conn = get_conn()
    conn.execute("""
        UPDATE accounts
        SET seller_id     = ?,
            nickname      = ?,
            access_token  = ?,
            refresh_token = ?,
            expires_at    = ?,
            authorized_at = ?
        WHERE name = ?
    """, (seller_id, nickname, access_token, refresh_token,
          expires_at, int(time.time()), name))
    conn.commit()
    conn.close()


def update_access_token(seller_id: str, access_token: str,
                        refresh_token: str, expires_in: int):
    expires_at = int(time.time()) + expires_in
    conn = get_conn()
    conn.execute("""
        UPDATE accounts
        SET access_token  = ?,
            refresh_token = ?,
            expires_at    = ?
        WHERE seller_id = ?
    """, (access_token, refresh_token, expires_at, seller_id))
    conn.commit()
    conn.close()


def revoke_account(name: str):
    conn = get_conn()
    conn.execute("""
        UPDATE accounts
        SET seller_id=NULL, nickname=NULL, access_token=NULL,
            refresh_token=NULL, expires_at=NULL, authorized_at=NULL
        WHERE name = ?
    """, (name,))
    conn.commit()
    conn.close()

# ── Fase 2: Tabelas de dados ──────────────────────────────────────────────────

def init_data_tables():
    """Cria tabelas de itens, estoque e pedidos."""
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id              TEXT PRIMARY KEY,
            seller_id       TEXT,
            title           TEXT,
            price           REAL,
            status          TEXT,
            is_catalog      INTEGER DEFAULT 0,
            has_promotion   INTEGER DEFAULT 0,
            sold_quantity   INTEGER DEFAULT 0,
            updated_at      INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS stock_full (
            item_id         TEXT PRIMARY KEY,
            seller_id       TEXT,
            available_qty   INTEGER DEFAULT 0,
            updated_at      INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id              TEXT NOT NULL,
            seller_id       TEXT NOT NULL,
            item_id         TEXT NOT NULL DEFAULT '',
            quantity        INTEGER,
            price           REAL,
            date_created    TEXT,
            status          TEXT,
            PRIMARY KEY (id, item_id)
        )
    """)
    # Migration: if orders table has old single-PK schema, recreate it
    pk_cols = [r[0] for r in c.execute("SELECT name FROM pragma_table_info('orders') WHERE pk > 0").fetchall()]
    if pk_cols == ['id']:
        c.execute("ALTER TABLE orders RENAME TO orders_old")
        c.execute("""
            CREATE TABLE orders (
                id              TEXT NOT NULL,
                seller_id       TEXT NOT NULL,
                item_id         TEXT NOT NULL DEFAULT '',
                quantity        INTEGER,
                price           REAL,
                date_created    TEXT,
                status          TEXT,
                PRIMARY KEY (id, item_id)
            )
        """)
        c.execute("""
            INSERT OR IGNORE INTO orders (id, seller_id, item_id, quantity, price, date_created, status)
            SELECT id, seller_id, COALESCE(item_id,''), quantity, price, date_created, status
            FROM orders_old
        """)
        c.execute("DROP TABLE orders_old")

    c.execute("""
        CREATE TABLE IF NOT EXISTS item_visits (
            item_id     TEXT PRIMARY KEY,
            seller_id   TEXT,
            visits_7d   INTEGER DEFAULT 0,
            visits_30d  INTEGER DEFAULT 0,
            updated_at  INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     TEXT NOT NULL,
            seller_id   TEXT,
            old_price   REAL,
            new_price   REAL,
            changed_at  INTEGER
        )
    """)

    conn.commit()
    conn.close()


def save_items(seller_id: str, items: list):
    conn = get_conn()
    now = int(time.time())
    for item in items:
        tags = item.get("tags", [])
        is_catalog = 1 if "catalog_listing" in tags else 0
        conn.execute("""
            INSERT OR REPLACE INTO items
            (id, seller_id, title, price, status, is_catalog, sold_quantity, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.get("id"),
            seller_id,
            item.get("title"),
            item.get("price"),
            item.get("status"),
            is_catalog,
            item.get("sold_quantity", 0),
            now,
        ))
    conn.commit()
    conn.close()


def save_stock(seller_id: str, item_id: str, available_qty: int):
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO stock_full
        (item_id, seller_id, available_qty, updated_at)
        VALUES (?, ?, ?, ?)
    """, (item_id, seller_id, available_qty, int(time.time())))
    conn.commit()
    conn.close()


def save_orders(seller_id: str, orders: list) -> dict:
    """
    Salva pedidos com isolamento por pedido: um pedido com dado inválido
    não derruba o lote inteiro. Commit incremental (por pedido) em vez de
    um único commit no final — se a coleta for interrompida no meio, os
    pedidos já processados até ali permanecem salvos.
    """
    conn = get_conn()
    saved = 0
    failed = 0
    for order in orders:
        order_id = str(order.get("id"))
        try:
            for item in order.get("order_items", []):
                conn.execute("""
                    INSERT OR REPLACE INTO orders
                    (id, seller_id, item_id, quantity, price, date_created, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id,
                    seller_id,
                    str(item.get("item", {}).get("id") or ""),
                    item.get("quantity"),
                    item.get("unit_price"),
                    order.get("date_created"),
                    order.get("status"),
                ))
            conn.commit()
            saved += 1
        except Exception as e:
            conn.rollback()
            failed += 1
            _orders_log.error(f"[{seller_id}] pedido {order_id} não salvo: {e}")

    conn.close()
    if failed:
        _orders_log.warning(f"[{seller_id}] save_orders: {saved} salvo(s), {failed} falhou(aram)")
    return {"saved": saved, "failed": failed}


def get_items_by_seller(seller_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM items WHERE seller_id = ? ORDER BY sold_quantity DESC",
        (seller_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stock_by_seller(seller_id: str) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM stock_full WHERE seller_id = ?", (seller_id,)
    ).fetchall()
    conn.close()
    return {r["item_id"]: r["available_qty"] for r in rows}


def get_daily_sales(seller_id: str, days: int = 30) -> list:
    """Daily aggregated sales for chart."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            date(substr(date_created, 1, 10))   AS day,
            COALESCE(SUM(quantity * price), 0)  AS revenue,
            COALESCE(SUM(quantity), 0)           AS units,
            COUNT(DISTINCT id)                   AS orders_count
        FROM orders
        WHERE seller_id = ?
          AND status != 'cancelled'
          AND datetime(substr(date_created, 1, 19))
              >= datetime('now', '-' || ? || ' days')
        GROUP BY day
        ORDER BY day
    """, (seller_id, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_kpi_raw(seller_id: str) -> dict:
    """Raw KPI stats: current (last 30d) vs previous (days 31-60)."""
    conn = get_conn()

    def _period(from_expr: str, to_expr: str) -> dict:
        row = conn.execute("""
            SELECT
                COALESCE(SUM(quantity * price), 0)   AS revenue,
                COALESCE(SUM(quantity), 0)            AS units,
                COALESCE(COUNT(DISTINCT id), 0)       AS orders
            FROM orders
            WHERE seller_id = ?
              AND status != 'cancelled'
              AND datetime(substr(date_created, 1, 19)) >= datetime('now', ?)
              AND datetime(substr(date_created, 1, 19)) <  datetime('now', ?)
        """, (seller_id, from_expr, to_expr)).fetchone()
        return dict(row)

    current  = _period('-30 days', '+1 day')
    previous = _period('-60 days', '-30 days')
    conn.close()
    return {'current': current, 'previous': previous}


def get_last_updated(seller_id: str):
    """Returns max updated_at timestamp from items."""
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(updated_at) AS ts FROM items WHERE seller_id = ?",
        (seller_id,)
    ).fetchone()
    conn.close()
    return row['ts'] if row else None


def get_recent_price_changes(seller_id: str, days: int = 7) -> list:
    """Returns recent price changes from price_history (if table exists)."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT ph.item_id, i.title, ph.old_price, ph.new_price, ph.changed_at
            FROM price_history ph
            JOIN items i ON i.id = ph.item_id
            WHERE ph.seller_id = ?
              AND ph.changed_at >= ?
            ORDER BY ph.changed_at DESC
            LIMIT 20
        """, (seller_id, int(time.time()) - days * 86400)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        conn.close()
        return []


# ── Módulo de custos e margem ─────────────────────────────────────────────────

def init_costs_table():
    """Cria tabelas de custos por produto e defaults por seller."""
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS product_costs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id     TEXT NOT NULL,
            item_id       TEXT NOT NULL,
            variation_id  TEXT NOT NULL DEFAULT '',
            unit_cost     REAL DEFAULT 0.0,
            tax_rate      REAL DEFAULT 0.0,
            ml_fee_rate   REAL DEFAULT 0.14,
            shipping_cost REAL DEFAULT 0.0,
            marca         TEXT DEFAULT '',
            cost_source   TEXT DEFAULT 'compra',
            updated_at    INTEGER,
            UNIQUE(seller_id, item_id, variation_id)
        )
    """)

    # Migrate: add marca/cost_source columns if they don't exist yet
    existing = {row[1] for row in c.execute("PRAGMA table_info(product_costs)").fetchall()}
    if "marca" not in existing:
        c.execute("ALTER TABLE product_costs ADD COLUMN marca TEXT DEFAULT ''")
    if "cost_source" not in existing:
        # 'compra' | 'bonificacao' | 'outro' — validado em Python, sem CHECK constraint
        # (mesmo padrão de 'marca': texto livre validado na camada de aplicação)
        c.execute("ALTER TABLE product_costs ADD COLUMN cost_source TEXT DEFAULT 'compra'")

    c.execute("""
        CREATE TABLE IF NOT EXISTS seller_defaults (
            seller_id    TEXT PRIMARY KEY,
            tax_rate     REAL DEFAULT 0.0,
            ml_fee_rate  REAL DEFAULT 0.14,
            representatividade_threshold_pct REAL DEFAULT 3.0,
            updated_at   INTEGER
        )
    """)

    existing_sd = {row[1] for row in c.execute("PRAGMA table_info(seller_defaults)").fetchall()}
    if "representatividade_threshold_pct" not in existing_sd:
        c.execute("ALTER TABLE seller_defaults ADD COLUMN representatividade_threshold_pct REAL DEFAULT 3.0")

    c.execute("""
        CREATE TABLE IF NOT EXISTS cost_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id    TEXT NOT NULL,
            item_id      TEXT NOT NULL,
            variation_id TEXT NOT NULL DEFAULT '',
            old_cost     REAL,
            new_cost     REAL,
            changed_at   INTEGER NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def get_seller_defaults(seller_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM seller_defaults WHERE seller_id = ?", (seller_id,)
    ).fetchone()
    conn.close()
    if row:
        d = dict(row)
        if d.get("representatividade_threshold_pct") is None:
            d["representatividade_threshold_pct"] = 3.0
        return d
    return {"tax_rate": 0.0, "ml_fee_rate": 0.14, "representatividade_threshold_pct": 3.0}


def get_representatividade_threshold(seller_id: str) -> float:
    """% mínimo de representatividade no faturamento pra um produto de margem baixa
    entrar em 'saída planejada' em vez de 'descontinuar'. Configurável por seller
    (contas com portfólio mais concentrado precisam de um corte diferente) — default 3%."""
    return float(get_seller_defaults(seller_id).get("representatividade_threshold_pct") or 3.0)


def save_representatividade_threshold(seller_id: str, pct: float):
    conn = get_conn()
    now = int(time.time())
    conn.execute("INSERT OR IGNORE INTO seller_defaults (seller_id, updated_at) VALUES (?, ?)",
                 (seller_id, now))
    conn.execute("""
        UPDATE seller_defaults
        SET representatividade_threshold_pct = ?, updated_at = ?
        WHERE seller_id = ?
    """, (pct, now, seller_id))
    conn.commit()
    conn.close()


def save_seller_defaults(seller_id: str, tax_rate: float, ml_fee_rate: float):
    conn = get_conn()
    now = int(time.time())
    conn.execute("INSERT OR IGNORE INTO seller_defaults (seller_id, updated_at) VALUES (?, ?)",
                 (seller_id, now))
    conn.execute("""
        UPDATE seller_defaults
        SET tax_rate = ?, ml_fee_rate = ?, updated_at = ?
        WHERE seller_id = ?
    """, (tax_rate, ml_fee_rate, now, seller_id))
    conn.commit()
    conn.close()


def save_cost(seller_id: str, item_id: str, variation_id: str | None,
              unit_cost: float, tax_rate: float,
              ml_fee_rate: float = 0.14, shipping_cost: float = 0.0,
              marca: str = "", cost_source: str = "compra"):
    vid = variation_id or ""
    now = int(time.time())
    conn = get_conn()

    # Capture old cost for history
    old_row = conn.execute(
        "SELECT unit_cost FROM product_costs WHERE seller_id=? AND item_id=? AND variation_id=?",
        (seller_id, item_id, vid)
    ).fetchone()
    old_cost = float(old_row["unit_cost"]) if old_row else None

    conn.execute("""
        INSERT OR IGNORE INTO product_costs
            (seller_id, item_id, variation_id, unit_cost, tax_rate, ml_fee_rate, shipping_cost, marca, cost_source, updated_at)
        VALUES (?, ?, ?, 0, 0, 0.14, 0, '', 'compra', ?)
    """, (seller_id, item_id, vid, now))
    conn.execute("""
        UPDATE product_costs
        SET unit_cost = ?, tax_rate = ?, ml_fee_rate = ?, shipping_cost = ?, marca = ?, cost_source = ?, updated_at = ?
        WHERE seller_id = ? AND item_id = ? AND variation_id = ?
    """, (unit_cost, tax_rate, ml_fee_rate, shipping_cost, marca, cost_source, now, seller_id, item_id, vid))

    # Record history only when unit_cost actually changes
    if old_cost is None or abs(old_cost - unit_cost) > 0.001:
        conn.execute("""
            INSERT INTO cost_history (seller_id, item_id, variation_id, old_cost, new_cost, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (seller_id, item_id, vid, old_cost, unit_cost, now))

    conn.commit()
    conn.close()


def apply_defaults_to_all(seller_id: str, tax_rate: float, ml_fee_rate: float):
    """Aplica alíquota e comissão padrão a todos os itens do seller."""
    now = int(time.time())
    conn = get_conn()
    # Upsert for all items of this seller
    rows = conn.execute(
        "SELECT id FROM items WHERE seller_id = ?", (seller_id,)
    ).fetchall()
    for r in rows:
        item_id = r["id"]
        conn.execute("""
            INSERT OR IGNORE INTO product_costs
                (seller_id, item_id, variation_id, unit_cost, tax_rate, ml_fee_rate, shipping_cost, updated_at)
            VALUES (?, ?, '', 0, ?, ?, 0, ?)
        """, (seller_id, item_id, tax_rate, ml_fee_rate, now))
        conn.execute("""
            UPDATE product_costs
            SET tax_rate = ?, ml_fee_rate = ?, updated_at = ?
            WHERE seller_id = ? AND item_id = ? AND variation_id = ''
        """, (tax_rate, ml_fee_rate, now, seller_id, item_id))
    conn.commit()
    conn.close()


def init_ml_fee_cache_table():
    """Cache curto (24-48h) da tarifa real do anúncio buscada sob demanda na API do ML."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_fee_cache (
            item_id          TEXT NOT NULL,
            seller_id        TEXT NOT NULL,
            percentage_fee   REAL,
            fixed_fee        REAL,
            sale_fee_amount  REAL,
            price_at_fetch   REAL,
            fetched_at       INTEGER NOT NULL,
            PRIMARY KEY (item_id, seller_id)
        )
    """)
    conn.commit()
    conn.close()


def get_cached_ml_fee(seller_id: str, item_id: str, max_age_hours: int = 36):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM ml_fee_cache WHERE seller_id = ? AND item_id = ?",
        (seller_id, item_id)
    ).fetchone()
    conn.close()
    if not row:
        return None
    row = dict(row)
    age_hours = (int(time.time()) - row["fetched_at"]) / 3600
    if age_hours > max_age_hours:
        return None
    return row


def save_ml_fee_cache(seller_id: str, item_id: str, percentage_fee: float,
                       fixed_fee: float, sale_fee_amount: float, price_at_fetch: float):
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO ml_fee_cache
            (item_id, seller_id, percentage_fee, fixed_fee, sale_fee_amount, price_at_fetch, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (item_id, seller_id, percentage_fee, fixed_fee, sale_fee_amount, price_at_fetch, int(time.time())))
    conn.commit()
    conn.close()


def update_ml_fee_rate(seller_id: str, item_id: str, variation_id: str, ml_fee_rate: float):
    """Atualiza só o ml_fee_rate (via save_cost), preservando unit_cost/tax_rate/shipping_cost/marca atuais."""
    conn = get_conn()
    row = conn.execute(
        "SELECT unit_cost, tax_rate, shipping_cost, marca, cost_source FROM product_costs "
        "WHERE seller_id=? AND item_id=? AND variation_id=?",
        (seller_id, item_id, variation_id or "")
    ).fetchone()
    conn.close()

    defaults = get_seller_defaults(seller_id)
    unit_cost     = row["unit_cost"]     if row else 0.0
    tax_rate      = row["tax_rate"]      if row else defaults["tax_rate"]
    shipping_cost = row["shipping_cost"] if row else 0.0
    marca         = row["marca"]         if row else ""
    cost_source   = row["cost_source"]   if row else "compra"

    save_cost(seller_id, item_id, variation_id, unit_cost, tax_rate, ml_fee_rate, shipping_cost, marca, cost_source)


def get_items_for_costs(seller_id: str) -> list:
    """Retorna todos os itens do seller com custos atuais (LEFT JOIN)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            i.id, i.title, i.price, i.status,
            pc.unit_cost, pc.tax_rate, pc.ml_fee_rate, pc.shipping_cost,
            pc.marca, pc.variation_id, pc.updated_at AS cost_updated_at
        FROM items i
        LEFT JOIN product_costs pc
            ON pc.item_id = i.id AND pc.seller_id = ? AND pc.variation_id = ''
        WHERE i.seller_id = ?
        ORDER BY i.title
    """, (seller_id, seller_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cost_history(seller_id: str, item_id: str, limit: int = 5) -> list:
    """Retorna as últimas N alterações de custo para um item."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT old_cost, new_cost, changed_at
        FROM cost_history
        WHERE seller_id = ? AND item_id = ?
        ORDER BY changed_at DESC
        LIMIT ?
    """, (seller_id, item_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_margin_data(seller_id: str, days: int = 30) -> list:
    """
    Dados brutos para cálculo de margem por item no período.
    Retorna apenas itens com vendas no período.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            i.id,
            i.title,
            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created, 1, 19))
                     >= datetime('now', '-' || ? || ' days')
                THEN o.quantity ELSE 0 END), 0)                    AS qty,
            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created, 1, 19))
                     >= datetime('now', '-' || ? || ' days')
                THEN o.quantity * o.price ELSE 0 END), 0)           AS receita_bruta,
            pc.unit_cost,
            pc.tax_rate,
            pc.ml_fee_rate,
            pc.shipping_cost
        FROM items i
        LEFT JOIN orders o
            ON o.item_id = i.id
        LEFT JOIN product_costs pc
            ON pc.item_id = i.id AND pc.seller_id = ? AND pc.variation_id = ''
        WHERE i.seller_id = ?
        GROUP BY i.id, i.title, pc.unit_cost, pc.tax_rate, pc.ml_fee_rate, pc.shipping_cost
        HAVING qty > 0
        ORDER BY receita_bruta DESC
    """, (days, days, seller_id, seller_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product_diagnostics_data(seller_id: str, days: int = 90) -> list:
    """
    Dados brutos pro diagnóstico de conta (margem + representatividade + marca +
    origem do custo). Ao contrário de get_margin_data, retorna TODOS os itens do
    seller mesmo sem venda no período — o diagnóstico precisa saber quem não vendeu
    pra classificar como 'sem_dado_suficiente' em vez de simplesmente ignorar.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            i.id,
            i.title,
            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created, 1, 19))
                     >= datetime('now', '-' || ? || ' days')
                THEN o.quantity ELSE 0 END), 0)                    AS qty,
            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created, 1, 19))
                     >= datetime('now', '-' || ? || ' days')
                THEN o.quantity * o.price ELSE 0 END), 0)           AS receita_bruta,
            pc.unit_cost,
            pc.tax_rate,
            pc.ml_fee_rate,
            pc.shipping_cost,
            pc.marca,
            pc.cost_source
        FROM items i
        LEFT JOIN orders o
            ON o.item_id = i.id
        LEFT JOIN product_costs pc
            ON pc.item_id = i.id AND pc.seller_id = ? AND pc.variation_id = ''
        WHERE i.seller_id = ?
        GROUP BY i.id, i.title, pc.unit_cost, pc.tax_rate, pc.ml_fee_rate, pc.shipping_cost, pc.marca, pc.cost_source
        ORDER BY receita_bruta DESC
    """, (days, days, seller_id, seller_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── NF-e (nota fiscal) ────────────────────────────────────────────────────────

def init_nfe_tables():
    """Cria tabelas de notas fiscais importadas, itens da nota e vínculo com anúncios."""
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS nfe_documents (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id     TEXT NOT NULL,
            chave_acesso  TEXT NOT NULL,
            supplier_cnpj TEXT NOT NULL,
            supplier_name TEXT,
            emitted_at    TEXT,
            imported_at   INTEGER NOT NULL,
            UNIQUE(seller_id, chave_acesso)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS nfe_items (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            nfe_document_id  INTEGER NOT NULL REFERENCES nfe_documents(id),
            cprod            TEXT NOT NULL,
            xprod            TEXT,
            ncm              TEXT,
            qtd              REAL,
            valor_unit       REAL,
            item_id          TEXT,
            variation_id     TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'pendente',
            cost_review_needed INTEGER NOT NULL DEFAULT 0,
            cost_review_reason TEXT DEFAULT ''
        )
    """)

    existing_ni = {row[1] for row in c.execute("PRAGMA table_info(nfe_items)").fetchall()}
    if "cost_review_needed" not in existing_ni:
        c.execute("ALTER TABLE nfe_items ADD COLUMN cost_review_needed INTEGER NOT NULL DEFAULT 0")
    if "cost_review_reason" not in existing_ni:
        c.execute("ALTER TABLE nfe_items ADD COLUMN cost_review_reason TEXT DEFAULT ''")

    c.execute("""
        CREATE TABLE IF NOT EXISTS nfe_product_links (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id     TEXT NOT NULL,
            supplier_cnpj TEXT NOT NULL,
            cprod         TEXT NOT NULL,
            item_id       TEXT NOT NULL,
            variation_id  TEXT NOT NULL DEFAULT '',
            created_at    INTEGER NOT NULL,
            UNIQUE(seller_id, supplier_cnpj, cprod)
        )
    """)

    conn.commit()
    conn.close()


def get_nfe_document_by_chave(seller_id: str, chave_acesso: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM nfe_documents WHERE seller_id = ? AND chave_acesso = ?",
        (seller_id, chave_acesso)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_nfe_document(seller_id: str, chave_acesso: str, supplier_cnpj: str,
                       supplier_name: str, emitted_at: str | None) -> int:
    """Insere o cabeçalho da nota. Levanta ValueError se a chave já foi importada pra esse seller."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM nfe_documents WHERE seller_id = ? AND chave_acesso = ?",
        (seller_id, chave_acesso)
    ).fetchone()
    if existing:
        conn.close()
        raise ValueError(f"Nota {chave_acesso} já foi importada anteriormente.")

    cur = conn.execute("""
        INSERT INTO nfe_documents
            (seller_id, chave_acesso, supplier_cnpj, supplier_name, emitted_at, imported_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (seller_id, chave_acesso, supplier_cnpj, supplier_name, emitted_at, int(time.time())))
    doc_id = cur.lastrowid
    conn.commit()
    conn.close()
    return doc_id


def save_nfe_items(nfe_document_id: int, items: list) -> int:
    """Insere os itens da nota, todos com status='pendente'. Retorna quantidade inserida."""
    conn = get_conn()
    for it in items:
        conn.execute("""
            INSERT INTO nfe_items
                (nfe_document_id, cprod, xprod, ncm, qtd, valor_unit, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pendente')
        """, (
            nfe_document_id, it["cprod"], it.get("xprod", ""),
            it.get("ncm", ""), it.get("qtd", 0), it.get("valor_unit", 0),
        ))
    conn.commit()
    conn.close()
    return len(items)


def get_nfe_link(seller_id: str, supplier_cnpj: str, cprod: str):
    """Retorna {item_id, variation_id} se já existe vínculo salvo, senão None."""
    conn = get_conn()
    row = conn.execute("""
        SELECT item_id, variation_id FROM nfe_product_links
        WHERE seller_id = ? AND supplier_cnpj = ? AND cprod = ?
    """, (seller_id, supplier_cnpj, cprod)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_nfe_link(seller_id: str, supplier_cnpj: str, cprod: str,
                   item_id: str, variation_id: str = ""):
    conn = get_conn()
    now = int(time.time())
    conn.execute("""
        INSERT INTO nfe_product_links (seller_id, supplier_cnpj, cprod, item_id, variation_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(seller_id, supplier_cnpj, cprod)
        DO UPDATE SET item_id = excluded.item_id, variation_id = excluded.variation_id
    """, (seller_id, supplier_cnpj, cprod, item_id, variation_id or "", now))
    conn.commit()
    conn.close()


BONIFICACAO_THRESHOLD_PCT = 0.20  # valor_unit abaixo de 20% da média histórica -> suspeita de bonificação


def get_avg_historical_cost(seller_id: str, item_id: str, variation_id: str) -> float | None:
    """Média dos custos anteriores registrados em cost_history pra esse item — usada
    como baseline pra detectar valor_unit suspeito de bonificação vindo de NF-e.
    Retorna None se não há histórico ainda (primeiro custo do item, nada pra comparar)."""
    conn = get_conn()
    row = conn.execute("""
        SELECT AVG(new_cost) AS avg_cost FROM cost_history
        WHERE seller_id=? AND item_id=? AND variation_id=? AND new_cost IS NOT NULL AND new_cost > 0
    """, (seller_id, item_id, variation_id or "")).fetchone()
    conn.close()
    return float(row["avg_cost"]) if row and row["avg_cost"] is not None else None


def update_cost_from_nfe(seller_id: str, item_id: str, variation_id: str, new_cost: float,
                          nfe_item_id: int | None = None):
    """Atualiza o unit_cost a partir do valor_unit de uma NF-e conciliada (via save_cost,
    que já trata cost_history), preservando tax_rate/ml_fee_rate/shipping_cost/marca/
    cost_source atuais do produto.

    Trava anti-bonificação: se o valor_unit vier abaixo de 20% da média histórica de
    custo já registrada pra esse item, NÃO sobrescreve o custo automaticamente — o
    vínculo produto↔nota continua válido (status permanece 'conciliado'), só o custo
    fica marcado pra revisão manual em vez de mascarar a margem com um valor inflado.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT tax_rate, ml_fee_rate, shipping_cost, marca, cost_source FROM product_costs "
        "WHERE seller_id=? AND item_id=? AND variation_id=?",
        (seller_id, item_id, variation_id or "")
    ).fetchone()
    conn.close()

    defaults = get_seller_defaults(seller_id)
    tax_rate      = row["tax_rate"]      if row else defaults["tax_rate"]
    ml_fee_rate   = row["ml_fee_rate"]   if row else defaults["ml_fee_rate"]
    shipping_cost = row["shipping_cost"] if row else 0.0
    marca         = row["marca"]         if row else ""
    cost_source   = row["cost_source"]   if row else "compra"

    avg_hist = get_avg_historical_cost(seller_id, item_id, variation_id)
    if avg_hist and new_cost < BONIFICACAO_THRESHOLD_PCT * avg_hist:
        if nfe_item_id is not None:
            pct_abaixo = round((1 - new_cost / avg_hist) * 100, 1)
            reason = (f"valor_unit R$ {new_cost:.2f} está {pct_abaixo}% abaixo da média "
                      f"histórica de custo (R$ {avg_hist:.2f}) — possível bonificação")
            conn = get_conn()
            conn.execute(
                "UPDATE nfe_items SET cost_review_needed = 1, cost_review_reason = ? WHERE id = ?",
                (reason, nfe_item_id)
            )
            conn.commit()
            conn.close()
        return

    save_cost(seller_id, item_id, variation_id, new_cost, tax_rate, ml_fee_rate, shipping_cost, marca, cost_source)
    if nfe_item_id is not None:
        conn = get_conn()
        conn.execute(
            "UPDATE nfe_items SET cost_review_needed = 0, cost_review_reason = '' WHERE id = ?",
            (nfe_item_id,)
        )
        conn.commit()
        conn.close()


def get_pending_nfe_items(seller_id: str) -> list:
    """Itens de NF-e ainda não conciliados com um anúncio, para esse seller."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT ni.id, ni.cprod, ni.xprod, ni.ncm, ni.qtd, ni.valor_unit,
               nd.supplier_cnpj, nd.supplier_name, nd.emitted_at
        FROM nfe_items ni
        JOIN nfe_documents nd ON nd.id = ni.nfe_document_id
        WHERE nd.seller_id = ? AND ni.status = 'pendente'
        ORDER BY nd.imported_at DESC, ni.id
    """, (seller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reconcile_nfe_item(nfe_item_id: int, seller_id: str, item_id: str, variation_id: str = ""):
    """Vincula manualmente um item de NF-e a um anúncio: grava o vínculo reutilizável,
    marca o item da nota como conciliado e atualiza o custo do produto."""
    conn = get_conn()
    ni = conn.execute(
        "SELECT ni.*, nd.supplier_cnpj FROM nfe_items ni "
        "JOIN nfe_documents nd ON nd.id = ni.nfe_document_id WHERE ni.id = ?",
        (nfe_item_id,)
    ).fetchone()
    if not ni:
        conn.close()
        raise ValueError(f"Item de NF-e {nfe_item_id} não encontrado.")
    ni = dict(ni)

    conn.execute("""
        UPDATE nfe_items SET item_id = ?, variation_id = ?, status = 'conciliado'
        WHERE id = ?
    """, (item_id, variation_id or "", nfe_item_id))
    conn.commit()
    conn.close()

    save_nfe_link(seller_id, ni["supplier_cnpj"], ni["cprod"], item_id, variation_id)
    update_cost_from_nfe(seller_id, item_id, variation_id, ni["valor_unit"], nfe_item_id=nfe_item_id)


def auto_reconcile_nfe_items(seller_id: str, nfe_document_id: int) -> int:
    """Após importar uma nota, tenta conciliar automaticamente os itens cujo
    (fornecedor, cProd) já tem vínculo salvo de uma nota anterior. Retorna quantos conciliou."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT ni.id, ni.cprod, ni.valor_unit, nd.supplier_cnpj
        FROM nfe_items ni
        JOIN nfe_documents nd ON nd.id = ni.nfe_document_id
        WHERE ni.nfe_document_id = ? AND ni.status = 'pendente'
    """, (nfe_document_id,)).fetchall()
    conn.close()

    matched = 0
    for r in rows:
        link = get_nfe_link(seller_id, r["supplier_cnpj"], r["cprod"])
        if not link:
            continue
        conn2 = get_conn()
        conn2.execute("""
            UPDATE nfe_items SET item_id = ?, variation_id = ?, status = 'conciliado'
            WHERE id = ?
        """, (link["item_id"], link["variation_id"], r["id"]))
        conn2.commit()
        conn2.close()
        update_cost_from_nfe(seller_id, link["item_id"], link["variation_id"], r["valor_unit"], nfe_item_id=r["id"])
        matched += 1
    return matched


# ── Diagnóstico de conta (margem + representatividade + marca controlada) ─────

def init_diagnostico_tables():
    """Cria tabelas de marcas com preço controlado pela indústria e status
    calculado de produto (manter / saída planejada / descontinuar / fora da régua)."""
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS marcas_preco_controlado (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id          TEXT NOT NULL,
            marca_normalizada  TEXT NOT NULL,
            marca_display      TEXT NOT NULL,
            motivo             TEXT DEFAULT '',
            created_at         INTEGER NOT NULL,
            UNIQUE(seller_id, marca_normalizada)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS product_status (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id        TEXT NOT NULL,
            item_id          TEXT NOT NULL,
            variation_id     TEXT NOT NULL DEFAULT '',
            computed_status  TEXT NOT NULL,
            computed_reason  TEXT DEFAULT '',
            margem_pct       REAL,
            share_pct        REAL,
            receita_bruta    REAL,
            override_status  TEXT,
            override_reason  TEXT,
            computed_at      INTEGER NOT NULL,
            override_at      INTEGER,
            override_by      TEXT,
            UNIQUE(seller_id, item_id, variation_id)
        )
    """)

    existing_ps = {row[1] for row in c.execute("PRAGMA table_info(product_status)").fetchall()}
    for col in ("margem_pct", "share_pct", "receita_bruta"):
        if col not in existing_ps:
            c.execute(f"ALTER TABLE product_status ADD COLUMN {col} REAL")

    conn.commit()
    conn.close()


def normalize_marca(marca: str) -> str:
    """Normaliza texto de marca pra matching: trim, lowercase, espaços colapsados.
    Não resolve typos (ex: 'Maxnutri' vs 'Maxmutri') — só variação de espaço/capitalização."""
    return re.sub(r"\s+", " ", (marca or "").strip().lower())


def save_marca_controlada(seller_id: str, marca_display: str, motivo: str = ""):
    conn = get_conn()
    conn.execute("""
        INSERT INTO marcas_preco_controlado (seller_id, marca_normalizada, marca_display, motivo, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(seller_id, marca_normalizada) DO UPDATE SET
            marca_display = excluded.marca_display, motivo = excluded.motivo
    """, (seller_id, normalize_marca(marca_display), marca_display, motivo, int(time.time())))
    conn.commit()
    conn.close()


def remove_marca_controlada(seller_id: str, marca: str):
    conn = get_conn()
    conn.execute(
        "DELETE FROM marcas_preco_controlado WHERE seller_id=? AND marca_normalizada=?",
        (seller_id, normalize_marca(marca))
    )
    conn.commit()
    conn.close()


def get_marcas_controladas(seller_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT marca_normalizada, marca_display, motivo FROM marcas_preco_controlado WHERE seller_id=?",
        (seller_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_product_diagnostics(seller_id: str, diagnostics: list):
    """Persiste o status computado em product_status, preservando override manual
    existente (override_status/override_reason não são tocados aqui).
    diagnostics: lista de {item_id, variation_id, status, motivo, margem_pct, share_pct, receita_bruta}."""
    conn = get_conn()
    now = int(time.time())
    for d in diagnostics:
        vid = d.get("variation_id") or ""
        conn.execute("""
            INSERT INTO product_status
                (seller_id, item_id, variation_id, computed_status, computed_reason,
                 margem_pct, share_pct, receita_bruta, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(seller_id, item_id, variation_id) DO UPDATE SET
                computed_status = excluded.computed_status,
                computed_reason = excluded.computed_reason,
                margem_pct      = excluded.margem_pct,
                share_pct       = excluded.share_pct,
                receita_bruta   = excluded.receita_bruta,
                computed_at     = excluded.computed_at
        """, (seller_id, d["item_id"], vid, d["status"], d["motivo"],
              d.get("margem_pct"), d.get("share_pct"), d.get("receita_bruta"), now))
    conn.commit()
    conn.close()


def get_product_status(seller_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM product_status WHERE seller_id=?", (seller_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product_diagnostics_view(seller_id: str) -> list:
    """product_status persistido, com título (items) e marca (product_costs) pra exibição.
    Não recalcula nada — reflete o último 'Recalcular diagnóstico' rodado."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT ps.*, i.title AS title, pc.marca AS marca
        FROM product_status ps
        LEFT JOIN items i ON i.id = ps.item_id
        LEFT JOIN product_costs pc
            ON pc.item_id = ps.item_id AND pc.seller_id = ps.seller_id AND pc.variation_id = ps.variation_id
        WHERE ps.seller_id = ?
        ORDER BY ps.receita_bruta DESC
    """, (seller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_product_status_override(seller_id: str, item_id: str, variation_id: str,
                                 override_status: str, override_reason: str, override_by: str = ""):
    conn = get_conn()
    now = int(time.time())
    conn.execute("""
        UPDATE product_status
        SET override_status = ?, override_reason = ?, override_at = ?, override_by = ?
        WHERE seller_id = ? AND item_id = ? AND variation_id = ?
    """, (override_status, override_reason, now, override_by, seller_id, item_id, variation_id or ""))
    conn.commit()
    conn.close()


def clear_product_status_override(seller_id: str, item_id: str, variation_id: str):
    conn = get_conn()
    conn.execute("""
        UPDATE product_status
        SET override_status = NULL, override_reason = NULL, override_at = NULL, override_by = NULL
        WHERE seller_id = ? AND item_id = ? AND variation_id = ?
    """, (seller_id, item_id, variation_id or ""))
    conn.commit()
    conn.close()


# ── Promoções ─────────────────────────────────────────────────────────────────

def init_promotions_table():
    """Cria tabela de promoções ativas."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS promotions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id       TEXT NOT NULL,
            promotion_id    TEXT NOT NULL,
            item_id         TEXT NOT NULL,
            promotion_type  TEXT,
            original_price  REAL DEFAULT 0.0,
            promo_price     REAL DEFAULT 0.0,
            discount_pct    REAL DEFAULT 0.0,
            start_date      TEXT,
            finish_date     TEXT,
            collected_at    INTEGER,
            UNIQUE(seller_id, promotion_id, item_id)
        )
    """)
    conn.commit()
    conn.close()


def save_promotions(seller_id: str, promotions: list):
    """
    Salva promoções ativas. Apaga as anteriores e reinsere.
    Cada elemento da lista deve ter:
      promotion_id, item_id, promotion_type,
      original_price, promo_price, start_date, finish_date
    """
    conn = get_conn()
    now  = int(time.time())

    conn.execute("DELETE FROM promotions WHERE seller_id = ?", (seller_id,))

    for p in promotions:
        orig  = float(p.get("original_price") or 0)
        promo = float(p.get("promo_price") or 0)
        disc  = round((orig - promo) / orig * 100, 1) if orig > 0 else 0.0

        conn.execute("""
            INSERT OR IGNORE INTO promotions
                (seller_id, promotion_id, item_id, promotion_type,
                 original_price, promo_price, discount_pct,
                 start_date, finish_date, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            seller_id,
            p.get("promotion_id"),
            p.get("item_id"),
            p.get("promotion_type"),
            orig, promo, disc,
            p.get("start_date"),
            p.get("finish_date"),
            now,
        ))

    # Atualiza flag has_promotion na tabela items
    conn.execute("UPDATE items SET has_promotion = 0 WHERE seller_id = ?", (seller_id,))
    promo_ids = list({p["item_id"] for p in promotions if p.get("item_id")})
    for item_id in promo_ids:
        conn.execute(
            "UPDATE items SET has_promotion = 1 WHERE id = ? AND seller_id = ?",
            (item_id, seller_id),
        )

    conn.commit()
    conn.close()


def get_promotions_summary(seller_id: str) -> dict:
    """Totais de promoções ativas para o dashboard."""
    conn = get_conn()
    row = conn.execute("""
        SELECT
            COUNT(DISTINCT promotion_id) AS total_promos,
            COUNT(DISTINCT item_id)      AS items_count,
            COALESCE(AVG(discount_pct), 0.0) AS avg_discount
        FROM promotions
        WHERE seller_id = ?
    """, (seller_id,)).fetchone()
    conn.close()
    if not row or not row["items_count"]:
        return {"total_promos": 0, "items_count": 0, "avg_discount": 0.0}
    return dict(row)


def get_promotions_for_profitability(seller_id: str) -> list:
    """Promoções ativas com dados de custo para cálculo de rentabilidade."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            p.promotion_id,
            p.item_id,
            p.promotion_type,
            p.original_price,
            p.promo_price,
            p.discount_pct,
            p.start_date,
            p.finish_date,
            i.title,
            pc.unit_cost,
            pc.tax_rate,
            pc.ml_fee_rate,
            pc.shipping_cost
        FROM promotions p
        LEFT JOIN items i
            ON i.id = p.item_id
        LEFT JOIN product_costs pc
            ON pc.item_id = p.item_id AND pc.seller_id = p.seller_id AND pc.variation_id = ''
        WHERE p.seller_id = ?
        ORDER BY p.discount_pct DESC
    """, (seller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Módulo 1 — Histórico de estoque ──────────────────────────────────────────

def init_history_tables():
    """Cria tabelas de histórico de estoque e impacto de rupturas."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_history (
            item_id       TEXT NOT NULL,
            seller_id     TEXT NOT NULL,
            available_qty INTEGER DEFAULT 0,
            date          TEXT NOT NULL,
            PRIMARY KEY (item_id, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rupture_impact (
            item_id                TEXT NOT NULL,
            seller_id              TEXT NOT NULL,
            date                   TEXT NOT NULL,
            estimated_lost_units   REAL DEFAULT 0,
            estimated_lost_revenue REAL DEFAULT 0,
            PRIMARY KEY (item_id, date)
        )
    """)
    conn.commit()
    conn.close()


def save_stock_snapshot(seller_id: str, item_id: str, available_qty: int,
                        date_str: str = None):
    """
    Salva snapshot diário de estoque. Nunca sobrescreve — INSERT OR IGNORE.
    date_str: YYYY-MM-DD, padrão = hoje
    """
    import datetime as _dt
    if date_str is None:
        date_str = _dt.date.today().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO stock_history (item_id, seller_id, available_qty, date)
        VALUES (?, ?, ?, ?)
    """, (item_id, seller_id, available_qty, date_str))
    conn.commit()
    conn.close()


def get_stock_history(seller_id: str, item_id: str, days: int = 90) -> list:
    conn = get_conn()
    rows = conn.execute("""
        SELECT date, available_qty
        FROM stock_history
        WHERE item_id = ? AND seller_id = ?
          AND date >= date('now', '-' || ? || ' days')
        ORDER BY date
    """, (item_id, seller_id, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stock_history_batch(seller_id: str, days: int = 30) -> dict:
    """Retorna histórico de todos os itens do seller como dict keyed por item_id."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT item_id, date, available_qty
        FROM stock_history
        WHERE seller_id = ?
          AND date >= date('now', '-' || ? || ' days')
        ORDER BY item_id, date
    """, (seller_id, days)).fetchall()
    conn.close()
    result = {}
    for r in rows:
        result.setdefault(r["item_id"], []).append(
            {"date": r["date"], "available_qty": r["available_qty"]}
        )
    return result


def get_product_orders_daily(seller_id: str, item_id: str, days: int = 90) -> list:
    """Agrega pedidos por dia para um único produto."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            date(substr(date_created, 1, 10))       AS day,
            COALESCE(SUM(quantity), 0)               AS units,
            COALESCE(SUM(quantity * price), 0)       AS revenue
        FROM orders
        WHERE item_id = ? AND seller_id = ? AND status != 'cancelled'
          AND datetime(substr(date_created, 1, 19))
              >= datetime('now', '-' || ? || ' days')
        GROUP BY day
        ORDER BY day
    """, (item_id, seller_id, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product_full_data(seller_id: str, item_id: str) -> dict:
    """Todos os dados de suporte para a página de produto."""
    conn = get_conn()

    item = conn.execute(
        "SELECT * FROM items WHERE id = ? AND seller_id = ?",
        (item_id, seller_id)
    ).fetchone()

    stock = conn.execute(
        "SELECT * FROM stock_full WHERE item_id = ? AND seller_id = ?",
        (item_id, seller_id)
    ).fetchone()

    costs = conn.execute(
        "SELECT * FROM product_costs WHERE item_id=? AND seller_id=? AND variation_id=''",
        (item_id, seller_id)
    ).fetchone()

    promos = conn.execute(
        "SELECT * FROM promotions WHERE item_id = ? AND seller_id = ?",
        (item_id, seller_id)
    ).fetchall()

    price_hist = conn.execute("""
        SELECT old_price, new_price, changed_at
        FROM price_history
        WHERE item_id = ? AND seller_id = ?
        ORDER BY changed_at DESC LIMIT 10
    """, (item_id, seller_id)).fetchall()

    conn.close()
    return {
        "item":          dict(item) if item else None,
        "stock":         dict(stock) if stock else None,
        "costs":         dict(costs) if costs else None,
        "promos":        [dict(p) for p in promos],
        "price_history": [dict(p) for p in price_hist],
    }


def get_rupture_summary_seller(seller_id: str, days: int = 90) -> dict:
    """
    Resumo de rupturas detectadas no histórico para o dashboard.
    Retorna totais de dias com estoque zero e estimativa de impacto.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            sh.item_id,
            COUNT(CASE WHEN sh.available_qty = 0 THEN 1 END) AS rupture_days,
            COALESCE(i.price, 0) AS price,
            (
                SELECT COALESCE(SUM(o.quantity), 0) / 30.0
                FROM orders o
                WHERE o.item_id = sh.item_id AND o.seller_id = sh.seller_id
                  AND o.status != 'cancelled'
                  AND datetime(substr(o.date_created,1,19))
                      >= datetime('now','-30 days')
            ) AS giro_30d
        FROM stock_history sh
        JOIN items i ON i.id = sh.item_id AND i.seller_id = sh.seller_id
        WHERE sh.seller_id = ?
          AND sh.date >= date('now', '-' || ? || ' days')
        GROUP BY sh.item_id
        HAVING rupture_days > 0
    """, (seller_id, days)).fetchall()
    conn.close()

    total_rupture_days = 0
    total_lost_revenue = 0.0
    items_affected = 0
    for r in rows:
        items_affected += 1
        total_rupture_days += r["rupture_days"]
        total_lost_revenue += r["rupture_days"] * r["giro_30d"] * r["price"]

    return {
        "items_affected":   items_affected,
        "rupture_days":     total_rupture_days,
        "lost_revenue_est": round(total_lost_revenue, 2),
        "has_history":      len(rows) > 0,
    }


# ── Módulo Mercado Pago ───────────────────────────────────────────────────────

def init_mp_tables():
    """Cria tabela de pagamentos MP e executa migrações necessárias."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mp_payments (
            payment_id           TEXT NOT NULL,
            seller_id            TEXT NOT NULL,
            order_id             TEXT DEFAULT '',
            item_id              TEXT DEFAULT '',
            item_title           TEXT DEFAULT '',
            status               TEXT DEFAULT '',
            status_detail        TEXT DEFAULT '',
            transaction_amount   REAL DEFAULT 0,
            net_received_amount  REAL DEFAULT 0,
            ml_fee               REAL DEFAULT 0,
            mp_fee               REAL DEFAULT 0,
            shipping_fee         REAL DEFAULT 0,
            shipping_amount      REAL DEFAULT 0,
            payment_method_id    TEXT DEFAULT '',
            payment_type_id      TEXT DEFAULT '',
            installments         INTEGER DEFAULT 1,
            date_created         TEXT DEFAULT '',
            date_approved        TEXT DEFAULT '',
            money_release_date   TEXT DEFAULT '',
            money_release_status TEXT DEFAULT '',
            collected_at         INTEGER,
            PRIMARY KEY (payment_id, seller_id)
        )
    """)
    # Migrations for columns added after initial deploy
    existing = {r[1] for r in conn.execute("PRAGMA table_info(mp_payments)").fetchall()}
    if "date_created" not in existing:
        conn.execute("ALTER TABLE mp_payments ADD COLUMN date_created TEXT DEFAULT ''")
    if "operation_type" not in existing:
        conn.execute("ALTER TABLE mp_payments ADD COLUMN operation_type TEXT DEFAULT ''")
        # Mark all pre-existing records as regular_payment (they were collected as sales)
        conn.execute(
            "UPDATE mp_payments SET operation_type = 'regular_payment' WHERE operation_type = ''"
        )
    conn.commit()
    conn.close()


def init_mp_other_movements_table():
    """Cria tabela de movimentações MP que não são vendas (compras, transferências, etc.)."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mp_other_movements (
            payment_id         TEXT NOT NULL,
            seller_id          TEXT NOT NULL,
            operation_type     TEXT DEFAULT '',
            status             TEXT DEFAULT '',
            transaction_amount REAL DEFAULT 0,
            net_amount         REAL DEFAULT 0,
            description        TEXT DEFAULT '',
            payment_method_id  TEXT DEFAULT '',
            date_created       TEXT DEFAULT '',
            date_approved      TEXT DEFAULT '',
            payer_id           TEXT DEFAULT '',
            collector_id       TEXT DEFAULT '',
            collected_at       INTEGER,
            PRIMARY KEY (payment_id, seller_id)
        )
    """)
    conn.commit()
    conn.close()


def save_mp_other_movements(seller_id: str, movements: list):
    """Salva movimentações não-venda do MP."""
    if not movements:
        return
    conn = get_conn()
    now = int(time.time())
    for m in movements:
        conn.execute("""
            INSERT OR REPLACE INTO mp_other_movements (
                payment_id, seller_id, operation_type, status,
                transaction_amount, net_amount, description,
                payment_method_id, date_created, date_approved,
                payer_id, collector_id, collected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            m.get("payment_id", ""), seller_id,
            m.get("operation_type", ""), m.get("status", ""),
            m.get("transaction_amount", 0), m.get("net_amount", 0),
            m.get("description", ""), m.get("payment_method_id", ""),
            m.get("date_created", ""), m.get("date_approved", ""),
            m.get("payer_id", ""), m.get("collector_id", ""),
            now,
        ))
    conn.commit()
    conn.close()


def get_mp_other_movements_summary(seller_id: str, days: int = 30) -> dict:
    """Resumo de movimentações não-venda por tipo de operação."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            operation_type,
            COUNT(*) AS count,
            COALESCE(SUM(ABS(transaction_amount)), 0) AS total_amount
        FROM mp_other_movements
        WHERE seller_id = ?
          AND date_created >= date('now', '-' || ? || ' days')
        GROUP BY operation_type
        ORDER BY total_amount DESC
    """, (seller_id, days)).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    total = sum(i["total_amount"] for i in items)
    count = sum(i["count"] for i in items)
    return {"items": items, "total_amount": round(total, 2), "total_count": count}


def init_meli_finance_tables():
    """
    Tabela staging da nova coleta oficial via orders+payments do Mercado Livre.
    Não substitui o fluxo atual; serve para migração e validação paralela.
    """
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meli_order_payments (
            payment_id           TEXT NOT NULL,
            seller_id            TEXT NOT NULL,
            order_id             TEXT DEFAULT '',
            item_id              TEXT DEFAULT '',
            item_title           TEXT DEFAULT '',
            status               TEXT DEFAULT '',
            status_detail        TEXT DEFAULT '',
            transaction_amount   REAL DEFAULT 0,
            net_received_amount  REAL DEFAULT 0,
            ml_fee               REAL DEFAULT 0,
            mp_fee               REAL DEFAULT 0,
            shipping_fee         REAL DEFAULT 0,
            shipping_amount      REAL DEFAULT 0,
            payment_method_id    TEXT DEFAULT '',
            payment_type_id      TEXT DEFAULT '',
            installments         INTEGER DEFAULT 1,
            date_created         TEXT DEFAULT '',
            date_approved        TEXT DEFAULT '',
            money_release_date   TEXT DEFAULT '',
            money_release_status TEXT DEFAULT '',
            source               TEXT DEFAULT 'meli_orders',
            order_status         TEXT DEFAULT '',
            collected_at         INTEGER,
            PRIMARY KEY (payment_id, seller_id)
        )
    """)
    conn.commit()
    conn.close()


_mp_log = _get_logger("database.mp_payments")

# Termos bloqueados como palavras inteiras (sem falso positivo em "earnings", "learning", etc.)
_MP_BLOCKED_TERMS = {
    "shipment", "marketplace_shipment",
    "cashback", "earn", "meli+",
    "google", "anthropic", "posto",
    "tcmp", "subscription", "youtube", "premium",
}


def _is_valid_mp_payment(p: dict) -> bool:
    # 1. order_id deve ser numérico válido
    order_id = str(p.get("order_id", "")).strip()
    if not order_id or not order_id.isdigit():
        return False

    # 2. Somente pagamentos aprovados
    if p.get("status", "") != "approved":
        return False

    # 3. Consistência financeira — todos os valores relevantes devem ser positivos
    if (p.get("net_received_amount") or 0) <= 0:
        return False
    if (p.get("transaction_amount") or 0) <= 0:
        return False

    # 4. Vínculo com venda — item_id e item_title são obrigatórios
    if not str(p.get("item_id", "")).strip():
        return False
    if not str(p.get("item_title", "")).strip():
        return False

    # 5. Filtro de palavras-chave como palavras inteiras (evita "earnings", "learning", etc.)
    raw = " ".join([
        str(p.get("item_title", "")),
        str(p.get("description", "")),
        str(p.get("payment_type_id", "")),
    ]).lower()
    text = f" {raw} "
    if any(f" {kw} " in text for kw in _MP_BLOCKED_TERMS):
        return False

    return True


def purge_invalid_mp_payments(seller_id: str) -> int:
    """
    Remove da tabela mp_payments todos os registros que não passariam em
    _is_valid_mp_payment: dados históricos sujos de coletas anteriores ao filtro.
    Retorna o número de linhas deletadas.
    """
    kw_conditions = "\n          OR ".join(
        f"lower(' ' || item_title || ' ' || payment_type_id || ' ') LIKE '% {kw} %'"
        for kw in _MP_BLOCKED_TERMS
    )
    conn = get_conn()
    cur = conn.execute(f"""
        DELETE FROM mp_payments
        WHERE seller_id = ?
          AND (
            order_id IS NULL
            OR trim(order_id) = ''
            OR CAST(order_id AS INTEGER) <= 0
            OR status != 'approved'
            OR CAST(net_received_amount AS REAL) <= 0
            OR CAST(transaction_amount  AS REAL) <= 0
            OR item_id IS NULL OR trim(item_id) = ''
            OR item_title IS NULL OR trim(item_title) = ''
            OR {kw_conditions}
          )
    """, (seller_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    _mp_log.info(f"purge seller={seller_id}: {deleted} registros inválidos removidos de mp_payments")
    return deleted


def save_mp_payments(seller_id: str, payments: list):
    """Upsert de pagamentos MP — mantém histórico, atualiza status se mudou."""
    # Limpa dados históricos inválidos antes de inserir novos
    purge_invalid_mp_payments(seller_id)

    total = len(payments) if payments else 0
    if not payments:
        _mp_log.info(f"mp_payments seller={seller_id}: nenhum registro recebido")
        return

    valid = [p for p in payments if _is_valid_mp_payment(p)]
    filtered = total - len(valid)
    _mp_log.info(
        f"mp_payments seller={seller_id}: total={total}, valid={len(valid)}, filtered={filtered}"
    )

    if not valid:
        return

    conn = get_conn()
    now  = int(time.time())
    for p in valid:
        conn.execute("""
            INSERT OR REPLACE INTO mp_payments (
                payment_id, seller_id, order_id, item_id, item_title,
                status, status_detail, transaction_amount, net_received_amount,
                ml_fee, mp_fee, shipping_fee, shipping_amount,
                payment_method_id, payment_type_id, installments,
                date_created, date_approved, money_release_date, money_release_status,
                operation_type, collected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            p["payment_id"], seller_id,
            p.get("order_id", ""), p.get("item_id", ""), p.get("item_title", ""),
            p.get("status", ""), p.get("status_detail", ""),
            p.get("transaction_amount", 0), p.get("net_received_amount", 0),
            p.get("ml_fee", 0), p.get("mp_fee", 0),
            p.get("shipping_fee", 0), p.get("shipping_amount", 0),
            p.get("payment_method_id", ""), p.get("payment_type_id", ""),
            p.get("installments", 1),
            p.get("date_created", ""), p.get("date_approved", ""),
            p.get("money_release_date", ""),
            p.get("money_release_status", ""),
            p.get("operation_type", "regular_payment"),
            now,
        ))
    conn.commit()
    conn.close()


def save_meli_order_payments(seller_id: str, payments: list):
    """Upsert dos pagamentos oficiais coletados a partir de orders do ML."""
    if not payments:
        return
    conn = get_conn()
    now = int(time.time())
    for p in payments:
        conn.execute("""
            INSERT OR REPLACE INTO meli_order_payments (
                payment_id, seller_id, order_id, item_id, item_title,
                status, status_detail, transaction_amount, net_received_amount,
                ml_fee, mp_fee, shipping_fee, shipping_amount,
                payment_method_id, payment_type_id, installments,
                date_created, date_approved, money_release_date, money_release_status,
                source, order_status, collected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            p["payment_id"], seller_id,
            p.get("order_id", ""), p.get("item_id", ""), p.get("item_title", ""),
            p.get("status", ""), p.get("status_detail", ""),
            p.get("transaction_amount", 0), p.get("net_received_amount", 0),
            p.get("ml_fee", 0), p.get("mp_fee", 0),
            p.get("shipping_fee", 0), p.get("shipping_amount", 0),
            p.get("payment_method_id", ""), p.get("payment_type_id", ""),
            p.get("installments", 1),
            p.get("date_created", ""), p.get("date_approved", ""),
            p.get("money_release_date", ""), p.get("money_release_status", ""),
            p.get("source", "meli_orders"), p.get("order_status", ""),
            now,
        ))
    conn.commit()
    conn.close()


def get_mp_payments_summary(seller_id: str, days: int = 30) -> dict:
    """
    Totais financeiros do período filtrados por date_created (data da venda).
    Fonte: campo date_created da API do Mercado Pago via range=date_created.
    """
    conn = get_conn()
    row = conn.execute("""
        SELECT
            COALESCE(SUM(transaction_amount),  0) AS receita_bruta,
            COALESCE(SUM(net_received_amount), 0) AS receita_liquida,
            COALESCE(SUM(ml_fee),              0) AS total_ml_fee,
            COALESCE(SUM(mp_fee),              0) AS total_mp_fee,
            COALESCE(SUM(shipping_fee),        0) AS total_shipping_fee,
            COUNT(*)                               AS total_payments,
            COALESCE(SUM(transaction_amount),  0) AS aprovado
        FROM mp_payments
        WHERE seller_id = ?
          AND status = 'approved'
          AND operation_type = 'regular_payment'
          AND CAST(net_received_amount AS REAL) > 0
          AND CAST(transaction_amount  AS REAL) > 0
          AND order_id != ''
          AND item_id   != ''
          AND date_created >= date('now', '-' || ? || ' days')
    """, (seller_id, days)).fetchone()
    conn.close()
    if not row:
        return {}
    d = dict(row)
    d["total_tarifas"] = round(d["total_ml_fee"] + d["total_mp_fee"] + d["total_shipping_fee"], 2)
    return d


def get_mp_daily_net(seller_id: str, days: int = 30) -> list:
    """Receita líquida por dia agrupada por date_created (data da venda)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            substr(date_created, 1, 10)             AS day,
            COALESCE(SUM(net_received_amount), 0)   AS net_amount,
            COALESCE(SUM(transaction_amount),  0)   AS gross_amount,
            COUNT(*)                                 AS count
        FROM mp_payments
        WHERE seller_id = ?
          AND status = 'approved'
          AND operation_type = 'regular_payment'
          AND date_created >= date('now', '-' || ? || ' days')
        GROUP BY day
        ORDER BY day
    """, (seller_id, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_mp_conciliation(seller_id: str, days: int = 30) -> list:
    """
    Conciliação: cruza pedidos ML com pagamentos MP.
    Retorna lista com dados de ambos os lados por order_id.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            mp.payment_id,
            mp.order_id,
            mp.item_id,
            mp.item_title,
            mp.status                AS payment_status,
            mp.status_detail,
            mp.transaction_amount,
            mp.net_received_amount,
            mp.ml_fee,
            mp.mp_fee,
            mp.shipping_fee,
            mp.payment_method_id,
            mp.payment_type_id,
            mp.installments,
            mp.date_approved,
            mp.money_release_date,
            mp.money_release_status,
            o.quantity,
            o.price                  AS order_price,
            o.status                 AS order_status,
            o.date_created           AS order_date
        FROM mp_payments mp
        LEFT JOIN orders o
            ON o.id = mp.order_id AND o.seller_id = mp.seller_id
        WHERE mp.seller_id = ?
          AND mp.status = 'approved'
          AND mp.operation_type = 'regular_payment'
          AND CAST(mp.net_received_amount AS REAL) > 0
          AND CAST(mp.transaction_amount  AS REAL) > 0
          AND mp.order_id != ''
          AND mp.item_id  != ''
          AND mp.date_created >= date('now', '-' || ? || ' days')
        ORDER BY mp.date_created DESC
    """, (seller_id, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_mp_pending_releases(seller_id: str) -> list:
    """Pagamentos aprovados ainda não liberados (a receber)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            payment_id, order_id, item_id, item_title,
            transaction_amount, net_received_amount,
            payment_method_id, installments,
            date_approved, money_release_date
        FROM mp_payments
        WHERE seller_id = ?
          AND status = 'approved'
          AND operation_type = 'regular_payment'
          AND money_release_status != 'released'
          AND money_release_date != ''
        ORDER BY money_release_date ASC
    """, (seller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_mp_fee_audit(seller_id: str, days: int = 30) -> list:
    """
    Auditoria de tarifas: agrupa por item e compara tarifa real vs esperada.
    Usa ml_fee_rate cadastrado em product_costs.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            mp.item_id,
            mp.item_title,
            COUNT(*)                                    AS payments_count,
            COALESCE(SUM(mp.transaction_amount),  0)    AS total_bruto,
            COALESCE(SUM(mp.ml_fee),              0)    AS total_ml_fee_real,
            COALESCE(SUM(mp.shipping_fee),        0)    AS total_ship_fee,
            COALESCE(AVG(mp.ml_fee / NULLIF(mp.transaction_amount,0)), 0) AS avg_ml_fee_rate,
            COALESCE(pc.ml_fee_rate, 0.14)              AS expected_rate
        FROM mp_payments mp
        LEFT JOIN product_costs pc
            ON pc.item_id = mp.item_id AND pc.seller_id = mp.seller_id AND pc.variation_id = ''
        WHERE mp.seller_id = ?
          AND mp.status = 'approved'
          AND mp.operation_type = 'regular_payment'
          AND mp.date_created >= date('now', '-' || ? || ' days')
          AND mp.item_id != ''
        GROUP BY mp.item_id, mp.item_title, pc.ml_fee_rate
        ORDER BY total_bruto DESC
    """, (seller_id, days)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        expected_fee = d["total_bruto"] * d["expected_rate"]
        d["expected_ml_fee"]   = round(expected_fee, 2)
        d["fee_divergence"]    = round(d["total_ml_fee_real"] - expected_fee, 2)
        d["avg_ml_fee_rate"]   = round(d["avg_ml_fee_rate"] * 100, 2)
        d["expected_rate_pct"] = round(d["expected_rate"] * 100, 2)
        result.append(d)
    return result


def get_raw_data_for_analytics(seller_id: str) -> list:
    """
    Dados brutos por item: volumes de venda por janela de tempo e estoque FULL.
    Sem lógica de negócio — apenas acesso a dados.

    Campos retornados:
      id, title, status, is_catalog, price
      stock          — estoque FULL disponível (0 se não rastreado)
      stock_tracked  — 1 se existe entrada em stock_full, 0 caso contrário
      qty_7d         — unidades vendidas nos últimos 7 dias (excl. cancelados)
      qty_15d        — unidades vendidas nos últimos 15 dias
      qty_30d        — unidades vendidas nos últimos 30 dias
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            i.id,
            i.title,
            i.status,
            i.is_catalog,
            i.price,
            COALESCE(i.sold_quantity, 0)                        AS sold_quantity,
            COALESCE(s.available_qty, 0)                        AS stock,
            CASE WHEN s.item_id IS NOT NULL THEN 1 ELSE 0 END   AS stock_tracked,

            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created, 1, 19))
                     >= datetime('now', '-7 days')
                THEN o.quantity ELSE 0 END), 0)                  AS qty_7d,

            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created, 1, 19))
                     >= datetime('now', '-15 days')
                THEN o.quantity ELSE 0 END), 0)                  AS qty_15d,

            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created, 1, 19))
                     >= datetime('now', '-30 days')
                THEN o.quantity ELSE 0 END), 0)                  AS qty_30d,

            MAX(CASE WHEN o.status != 'cancelled'
                     THEN substr(o.date_created, 1, 10) ELSE NULL END) AS last_sale_date

        FROM items i
        LEFT JOIN stock_full s ON s.item_id = i.id
        LEFT JOIN orders o     ON o.item_id  = i.id
        WHERE i.seller_id = ?
        GROUP BY i.id, i.title, i.status, i.is_catalog, i.price,
                 i.sold_quantity, s.available_qty, s.item_id
    """, (seller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_dashboard_data(seller_id: str) -> list:
    """
    Retorna todos os anúncios do seller com métricas calculadas:
      - orders_30d : unidades vendidas nos últimos 30 dias
      - giro       : vendas/dia (orders_30d / 30)
      - stock      : estoque FULL disponível
      - coverage   : dias de cobertura (stock / giro), None se giro=0
      - classification : Crítico / Oportunidade / Estável / Descarte
      - alerts     : lista de strings com alertas ativos
    """
    conn = get_conn()

    rows = conn.execute("""
        SELECT
            i.id,
            i.title,
            i.status,
            i.is_catalog,
            i.sold_quantity,
            i.price,
            COALESCE(s.available_qty, 0) AS stock,
            COALESCE(SUM(
                CASE WHEN o.status != 'cancelled'
                     AND datetime(substr(o.date_created, 1, 19))
                         >= datetime('now', '-30 days')
                THEN o.quantity ELSE 0 END
            ), 0) AS orders_30d
        FROM items i
        LEFT JOIN stock_full s ON s.item_id = i.id
        LEFT JOIN orders o     ON o.item_id  = i.id
        WHERE i.seller_id = ?
        GROUP BY i.id, i.title, i.status, i.is_catalog,
                 i.sold_quantity, i.price, s.available_qty
    """, (seller_id,)).fetchall()

    conn.close()

    items = []
    for r in rows:
        giro     = round(r["orders_30d"] / 30.0, 1)
        stock    = r["stock"]
        status   = r["status"]
        catalog  = bool(r["is_catalog"])

        # Cobertura em dias
        if giro > 0:
            coverage = round(stock / giro, 1)
        elif stock > 0:
            coverage = None          # estoque parado, sem giro — infinito
        else:
            coverage = None

        # Classificação
        if giro == 0:
            classification = "Descarte"
        elif coverage is not None and coverage < 7:
            classification = "Crítico"
        elif status == "paused" or (coverage is not None and coverage < 30):
            classification = "Oportunidade"
        else:
            classification = "Estável"

        # Alertas
        alerts = []
        if catalog:
            alerts.append("catálogo")
        if status == "paused" and not catalog:
            alerts.append("pausado")
        if giro > 0 and stock == 0:
            alerts.append("sem_estoque")

        items.append({
            "id":             r["id"],
            "title":          r["title"],
            "status":         status,
            "is_catalog":     catalog,
            "price":          r["price"],
            "stock":          stock,
            "orders_30d":     r["orders_30d"],
            "giro":           giro,
            "coverage":       coverage,
            "classification": classification,
            "alerts":         alerts,
        })

    # Ordena: Crítico → Oportunidade → Estável → Descarte, depois por giro desc
    priority = {"Crítico": 0, "Oportunidade": 1, "Estável": 2, "Descarte": 3}
    items.sort(key=lambda x: (priority[x["classification"]], -x["giro"]))
    return items


# ── Módulo Análise de Queda de Performance ────────────────────────────────────

def get_performance_comparison(seller_id: str, days: int = 30) -> dict:
    """
    Compara período atual (últimos N dias) com período anterior (N dias anteriores).
    Retorna dados por item para o módulo de Análise de Queda.

    Campos por item:
      item_id, title, price
      qty_current, revenue_current   — período atual
      qty_prev,    revenue_prev      — período anterior
      qty_delta, revenue_delta       — diferença absoluta (atual - anterior)
      revenue_delta_pct              — variação percentual
      stock_current                  — estoque no momento
      has_zero_stock                 — teve ruptura no período atual (stock_history)
      is_paused                      — anúncio pausado no momento
    """
    conn = get_conn()

    rows = conn.execute("""
        SELECT
            i.id          AS item_id,
            i.title,
            i.price,
            i.status,
            COALESCE(s.available_qty, 0) AS stock_current,

            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created,1,19)) >= datetime('now', '-' || ? || ' days')
                THEN o.quantity ELSE 0 END), 0) AS qty_current,

            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created,1,19)) >= datetime('now', '-' || ? || ' days')
                THEN o.quantity * o.price ELSE 0 END), 0) AS revenue_current,

            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created,1,19)) >= datetime('now', '-' || (? * 2) || ' days')
                 AND datetime(substr(o.date_created,1,19)) <  datetime('now', '-' || ? || ' days')
                THEN o.quantity ELSE 0 END), 0) AS qty_prev,

            COALESCE(SUM(CASE
                WHEN o.status != 'cancelled'
                 AND datetime(substr(o.date_created,1,19)) >= datetime('now', '-' || (? * 2) || ' days')
                 AND datetime(substr(o.date_created,1,19)) <  datetime('now', '-' || ? || ' days')
                THEN o.quantity * o.price ELSE 0 END), 0) AS revenue_prev

        FROM items i
        LEFT JOIN stock_full s ON s.item_id = i.id AND s.seller_id = i.seller_id
        LEFT JOIN orders o     ON o.item_id  = i.id AND o.seller_id = i.seller_id
        WHERE i.seller_id = ?
        GROUP BY i.id, i.title, i.price, i.status, s.available_qty
        HAVING qty_current > 0 OR qty_prev > 0
        ORDER BY (revenue_prev - revenue_current) DESC
    """, (days, days, days, days, days, days, seller_id)).fetchall()

    # Detectar rupturas no período atual via stock_history
    rupture_items = set()
    try:
        rrows = conn.execute("""
            SELECT DISTINCT item_id
            FROM stock_history
            WHERE seller_id = ?
              AND date >= date('now', '-' || ? || ' days')
              AND available_qty = 0
        """, (seller_id, days)).fetchall()
        rupture_items = {r["item_id"] for r in rrows}
    except Exception:
        pass

    conn.close()

    items = []
    total_revenue_current = 0.0
    total_revenue_prev    = 0.0

    for r in rows:
        rev_curr = float(r["revenue_current"] or 0)
        rev_prev = float(r["revenue_prev"] or 0)
        qty_curr = int(r["qty_current"] or 0)
        qty_prev = int(r["qty_prev"] or 0)

        total_revenue_current += rev_curr
        total_revenue_prev    += rev_prev

        rev_delta = rev_curr - rev_prev
        rev_delta_pct = None
        if rev_prev > 0:
            rev_delta_pct = round((rev_curr - rev_prev) / rev_prev * 100, 1)

        has_rupture = r["item_id"] in rupture_items
        is_paused   = r["status"] == "paused"
        stock       = int(r["stock_current"] or 0)

        # Classifica a causa da queda
        if qty_curr < qty_prev:
            if is_paused:
                cause = "PAUSADO"
            elif has_rupture:
                cause = "RUPTURA"
            elif stock == 0 and qty_curr < qty_prev:
                cause = "RUPTURA"
            else:
                cause = "QUEDA_PERFORMANCE"
        elif qty_curr == 0 and qty_prev == 0:
            cause = None
        else:
            cause = "CRESCIMENTO" if qty_curr >= qty_prev else "QUEDA_PERFORMANCE"

        items.append({
            "item_id":          r["item_id"],
            "title":            r["title"],
            "price":            float(r["price"] or 0),
            "status":           r["status"],
            "stock_current":    stock,
            "qty_current":      qty_curr,
            "revenue_current":  round(rev_curr, 2),
            "qty_prev":         qty_prev,
            "revenue_prev":     round(rev_prev, 2),
            "qty_delta":        qty_curr - qty_prev,
            "revenue_delta":    round(rev_delta, 2),
            "revenue_delta_pct": rev_delta_pct,
            "has_rupture":      has_rupture,
            "is_paused":        is_paused,
            "cause":            cause,
        })

    total_delta = round(total_revenue_current - total_revenue_prev, 2)

    # Agrupa por causa
    by_cause = {}
    for item in items:
        c = item["cause"]
        if c in ("RUPTURA", "QUEDA_PERFORMANCE", "PAUSADO") and item["revenue_delta"] < 0:
            if c not in by_cause:
                by_cause[c] = {"products": [], "total_impact": 0.0}
            by_cause[c]["products"].append(item)
            by_cause[c]["total_impact"] += item["revenue_delta"]  # negativo

    for c in by_cause:
        by_cause[c]["total_impact"] = round(by_cause[c]["total_impact"], 2)

    return {
        "period_days":            days,
        "items":                  items,
        "total_revenue_current":  round(total_revenue_current, 2),
        "total_revenue_prev":     round(total_revenue_prev, 2),
        "total_delta":            total_delta,
        "by_cause":               by_cause,
    }


# ── Estoque duplo (próprio + FULL) ──────────────────────────────────────────────

def init_stock_own_tables():
    """Cria tabela de estoque próprio e de envios ao FULL (mesmo conceito da Fase 3)."""
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS stock_own (
            item_id        TEXT NOT NULL,
            seller_id      TEXT NOT NULL,
            available_qty  INTEGER NOT NULL DEFAULT 0,
            updated_at     INTEGER,
            PRIMARY KEY (item_id, seller_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS shipments_full (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id       TEXT NOT NULL,
            item_id         TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            shipped_at      TEXT NOT NULL,
            reconciled_qty  INTEGER,
            reconciled_at   INTEGER,
            status          TEXT DEFAULT 'pending_reconciliation',
            created_at      INTEGER
        )
    """)

    conn.commit()
    conn.close()


def get_stock_own(seller_id: str) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT item_id, available_qty FROM stock_own WHERE seller_id = ?", (seller_id,)
    ).fetchall()
    conn.close()
    return {r["item_id"]: r["available_qty"] for r in rows}


def set_stock_own(seller_id: str, item_id: str, available_qty: int):
    """Ajuste manual de estoque próprio — ex: entrada de mercadoria do fornecedor."""
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO stock_own (item_id, seller_id, available_qty, updated_at)
        VALUES (?, ?, ?, ?)
    """, (item_id, seller_id, available_qty, int(time.time())))
    conn.commit()
    conn.close()


def register_full_shipment(seller_id: str, item_id: str, quantity: int, shipped_at: str) -> int:
    """
    Registra um envio de mercadoria pro FULL: debita do estoque próprio e
    grava o evento em shipments_full como pendente de reconciliação contra
    o próximo sync real da API do ML.
    """
    if quantity <= 0:
        raise ValueError("Quantidade do envio deve ser maior que zero.")

    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO stock_own (item_id, seller_id, available_qty, updated_at) VALUES (?, ?, 0, ?)",
        (item_id, seller_id, int(time.time())),
    )
    row = conn.execute(
        "SELECT available_qty FROM stock_own WHERE item_id = ? AND seller_id = ?",
        (item_id, seller_id),
    ).fetchone()
    current = row["available_qty"] if row else 0

    if quantity > current:
        conn.close()
        raise ValueError(
            f"Estoque próprio insuficiente: disponível {current}, envio solicitado {quantity}."
        )

    now = int(time.time())
    conn.execute("""
        UPDATE stock_own SET available_qty = available_qty - ?, updated_at = ?
        WHERE item_id = ? AND seller_id = ?
    """, (quantity, now, item_id, seller_id))

    cur = conn.execute("""
        INSERT INTO shipments_full
            (seller_id, item_id, quantity, shipped_at, status, created_at)
        VALUES (?, ?, ?, ?, 'pending_reconciliation', ?)
    """, (seller_id, item_id, quantity, shipped_at, now))
    shipment_id = cur.lastrowid

    conn.commit()
    conn.close()
    return shipment_id


def get_consolidated_stock(seller_id: str) -> list:
    """Estoque próprio + FULL + total + valor (qtd × custo unitário) por item (LEFT JOIN)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            i.id, i.title,
            COALESCE(so.available_qty, 0) AS estoque_proprio,
            COALESCE(sf.available_qty, 0) AS estoque_full,
            COALESCE(so.available_qty, 0) + COALESCE(sf.available_qty, 0) AS total,
            pc.unit_cost AS unit_cost,
            (COALESCE(so.available_qty, 0) + COALESCE(sf.available_qty, 0)) * COALESCE(pc.unit_cost, 0) AS valor_estoque
        FROM items i
        LEFT JOIN stock_own  so ON so.item_id = i.id AND so.seller_id = i.seller_id
        LEFT JOIN stock_full sf ON sf.item_id = i.id AND sf.seller_id = i.seller_id
        LEFT JOIN product_costs pc ON pc.item_id = i.id AND pc.seller_id = i.seller_id AND pc.variation_id = ''
        WHERE i.seller_id = ?
        ORDER BY i.title
    """, (seller_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_shipments_history(seller_id: str, limit: int = 100) -> list:
    """Histórico de envios ao FULL registrados manualmente, mais recentes primeiro."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT sf.id, sf.item_id, i.title, sf.quantity, sf.shipped_at,
               sf.status, sf.reconciled_qty, sf.reconciled_at, sf.created_at
        FROM shipments_full sf
        LEFT JOIN items i ON i.id = sf.item_id AND i.seller_id = sf.seller_id
        WHERE sf.seller_id = ?
        ORDER BY sf.created_at DESC
        LIMIT ?
    """, (seller_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reconcile_pending_shipments(seller_id: str, tolerance: int = 0) -> int:
    """
    Primeiro corte de reconciliação: para envios pendentes, se o estoque FULL
    atual do item já reflete pelo menos o que foi enviado, marca como reconciliado.
    Não é definitivo — só um sinal pra ajudar a investigar divergência de FULL.
    """
    conn = get_conn()
    pending = conn.execute("""
        SELECT id, item_id, quantity FROM shipments_full
        WHERE seller_id = ? AND status = 'pending_reconciliation'
    """, (seller_id,)).fetchall()

    reconciled = 0
    now = int(time.time())
    for p in pending:
        full_row = conn.execute(
            "SELECT available_qty FROM stock_full WHERE item_id = ? AND seller_id = ?",
            (p["item_id"], seller_id),
        ).fetchone()
        full_qty = full_row["available_qty"] if full_row else 0

        if full_qty + tolerance >= p["quantity"]:
            conn.execute("""
                UPDATE shipments_full
                SET reconciled_qty = ?, reconciled_at = ?, status = 'reconciled'
                WHERE id = ?
            """, (full_qty, now, p["id"]))
            reconciled += 1

    conn.commit()
    conn.close()
    return reconciled
