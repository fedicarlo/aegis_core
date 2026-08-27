#!/usr/bin/env python3
"""
Backfill de orders.buyer_id — re-busca pedidos na API do ML (que trazem buyer.id)
e preenche a coluna orders.buyer_id, sem mexer em mais nada.

Escrita mínima: só `UPDATE orders SET buyer_id = ? WHERE seller_id = ? AND id = ?`.
Não faz INSERT/DELETE, não toca status/quantidade/preço.

Uso:
  # todas as contas autorizadas (roda contra o DB apontado por DB_PATH):
  .venv/bin/python -m scripts.backfill_buyer_ids
  # uma conta, com token explícito (usado pela validação da Etapa 4 em scratch):
  from scripts.backfill_buyer_ids import backfill_account
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import get_all_accounts, get_conn
from app.services import meli_api
from app.services.meli_auth import get_valid_token
from app.utils.logger import get_logger

log = get_logger("backfill_buyer_ids")

DEFAULT_DAYS = 220  # cobre o pedido mais antigo de qualquer conta (Maximus ~fev/2026)


def backfill_account(seller_id, token, *, days=DEFAULT_DAYS):
    """Re-busca pedidos do seller e preenche buyer_id. Retorna estatísticas."""
    orders = meli_api.get_orders(token, seller_id, days=days)

    conn = get_conn()
    rows_updated = 0
    orders_with_buyer = 0
    orders_no_buyer = 0
    for o in orders:
        oid = str(o.get("id"))
        buyer_id = str((o.get("buyer") or {}).get("id") or "") or None
        if not buyer_id:
            orders_no_buyer += 1
            continue
        orders_with_buyer += 1
        cur = conn.execute(
            "UPDATE orders SET buyer_id = ? WHERE seller_id = ? AND id = ?",
            (buyer_id, seller_id, oid),
        )
        rows_updated += cur.rowcount
    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE seller_id = ?", (seller_id,)
    ).fetchone()[0]
    still_null = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE seller_id = ? AND (buyer_id IS NULL OR buyer_id = '')",
        (seller_id,),
    ).fetchone()[0]
    conn.close()

    stats = {
        "seller_id": seller_id,
        "orders_api": len(orders),
        "orders_com_buyer": orders_with_buyer,
        "orders_sem_buyer_na_api": orders_no_buyer,
        "linhas_atualizadas": rows_updated,
        "linhas_total_no_banco": total,
        "linhas_ainda_null": still_null,
    }
    log.info(f"[{seller_id}] backfill buyer_id: {stats}")
    return stats


def main():
    accounts = [a for a in get_all_accounts() if a.get("access_token")]
    print(f"contas autorizadas: {[a['name'] for a in accounts]}")
    all_stats = []
    for a in accounts:
        try:
            token = get_valid_token(a)
            st = backfill_account(a["seller_id"], token)
            st["account"] = a["name"]
            all_stats.append(st)
            print(f"  {a['name']:16s} api={st['orders_api']:5d}  "
                  f"atualizadas={st['linhas_atualizadas']:5d}  "
                  f"ainda_null={st['linhas_ainda_null']:5d}")
        except Exception as e:  # noqa: BLE001
            print(f"  {a['name']:16s} ERRO: {e}")
            all_stats.append({"account": a["name"], "erro": str(e)})
    return all_stats


if __name__ == "__main__":
    main()
