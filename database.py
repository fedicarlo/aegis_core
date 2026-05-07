import sqlite3
import time
from app.config import DB_PATH, ACCOUNTS


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Cria tabelas e popula as contas iniciais."""
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


# ── Leitura ───────────────────────────────────────────────────────────────────

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


# ── Escrita ───────────────────────────────────────────────────────────────────

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
