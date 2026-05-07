"""
AEGIS — Validação de dados vs painel real do MercadoLivre.

Uso: python scripts/validate_data.py [seller_id]
Padrão: seller_id = 2124526664

Saída: relatório diagnóstico comparando DB com valores esperados do painel ML.
"""
import sys
import os
import sqlite3
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = "aegis.db"
SELLER_ID = sys.argv[1] if len(sys.argv) > 1 else "2124526664"

# ── Valores de referência do painel ML (atualizar quando mudar) ───────────────
EXPECTED = {
    "orders_30d":   449,
    "units_30d":    480,
    "revenue_30d":  30695.0,
    "avg_ticket":   63.95,
}


def section(title: str):
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print('═' * 60)


def ok(label: str, val, expected=None, tolerance_pct: float = 5.0, fmt=None):
    display = fmt(val) if fmt else val
    if expected is None:
        print(f"  {'✓':<4} {label}: {display}")
        return
    exp_display = fmt(expected) if fmt else expected
    diff = abs(float(val) - float(expected))
    pct  = diff / float(expected) * 100 if float(expected) else 0
    ok_  = pct <= tolerance_pct
    sym  = "✓" if ok_ else "✗"
    print(f"  {sym:<4} {label}: {display} (esperado: {exp_display}, desvio: {pct:.1f}%)")


conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

section(f"1 · VISÃO GERAL — seller_id={SELLER_ID}")

# Cobertura temporal
row = conn.execute(
    "SELECT MIN(date_created), MAX(date_created), COUNT(*) FROM orders WHERE seller_id=?",
    (SELLER_ID,)
).fetchone()
print(f"  {'':4} Pedidos total no banco:   {row[2]}")
print(f"  {'':4} Data mais antiga:         {row[0]}")
print(f"  {'':4} Data mais recente:        {row[1]}")
hoje = date.today().isoformat()
print(f"  {'':4} Hoje (UTC):               {hoje}")

# Dias desde última coleta
max_date = row[1]
if max_date:
    try:
        last_dt = datetime.fromisoformat(max_date[:19].replace("T", " ")).date()
        dias_defasagem = (date.today() - last_dt).days
        sym = "✓" if dias_defasagem <= 1 else "✗"
        print(f"  {sym:<4} Defasagem de coleta:      {dias_defasagem} dia(s)")
    except Exception:
        pass

section("2 · PEDIDOS — últimos 30 dias")

r = conn.execute("""
    SELECT COUNT(DISTINCT id) AS orders,
           COALESCE(SUM(quantity), 0) AS units,
           COALESCE(SUM(quantity * price), 0) AS revenue
    FROM orders
    WHERE seller_id = ?
      AND status != 'cancelled'
      AND datetime(substr(date_created,1,19)) >= datetime('now', '-30 days')
""", (SELLER_ID,)).fetchone()

ok("Pedidos (orders)", r["orders"], EXPECTED["orders_30d"])
ok("Unidades",         r["units"],  EXPECTED["units_30d"])
ok("Receita bruta",    r["revenue"], EXPECTED["revenue_30d"], tolerance_pct=10,
   fmt=lambda v: f"R$ {v:,.2f}")

if r["orders"] > 0:
    ticket = r["revenue"] / r["orders"]
    ok("Ticket médio/pedido", ticket, EXPECTED["avg_ticket"], tolerance_pct=15,
       fmt=lambda v: f"R$ {v:.2f}")

section("3 · STATUS DOS PEDIDOS (últimos 30d)")

rows = conn.execute("""
    SELECT status, COUNT(DISTINCT id) AS cnt
    FROM orders
    WHERE seller_id = ?
      AND datetime(substr(date_created,1,19)) >= datetime('now', '-30 days')
    GROUP BY status ORDER BY cnt DESC
""", (SELLER_ID,)).fetchall()
for r in rows:
    print(f"  {'':4} {r['status']:<25} {r['cnt']}")

section("4 · PEDIDOS DUPLICADOS")

dups = conn.execute("""
    SELECT id, COUNT(*) AS cnt FROM orders
    WHERE seller_id = ?
    GROUP BY id HAVING cnt > 1 LIMIT 10
""", (SELLER_ID,)).fetchall()
if dups:
    print(f"  ✗    {len(dups)} order_id(s) com linhas duplicadas (multi-item OK, verifique):")
    for d in dups[:5]:
        print(f"       {d['id']} — {d['cnt']} linha(s)")
else:
    print(f"  ✓    Nenhum duplicado encontrado")

section("5 · PAGAMENTOS MERCADO PAGO")

r = conn.execute("SELECT COUNT(*) FROM mp_payments WHERE seller_id=?", (SELLER_ID,)).fetchone()
print(f"  {'':4} Total de registros MP:    {r[0]}")

mp_sales = conn.execute("""
    SELECT COUNT(*), COALESCE(SUM(transaction_amount),0), COALESCE(SUM(net_received_amount),0)
    FROM mp_payments
    WHERE seller_id=?
      AND status='approved'
      AND operation_type='regular_payment'
      AND order_id != '' AND item_id != ''
      AND CAST(net_received_amount AS REAL) > 0
      AND date_created >= date('now', '-30 days')
""", (SELLER_ID,)).fetchone()
print(f"  {'':4} Pagamentos de venda (30d): {mp_sales[0]}")
print(f"  {'':4} Receita bruta MP (30d):   R$ {mp_sales[1]:,.2f}")
print(f"  {'':4} Receita líquida MP (30d): R$ {mp_sales[2]:,.2f}")

# Check operation_type column
has_op = conn.execute("SELECT name FROM pragma_table_info('mp_payments') WHERE name='operation_type'").fetchone()
sym = "✓" if has_op else "✗"
print(f"  {sym:<4} Coluna operation_type existe: {'sim' if has_op else 'NAO — rode init_mp_tables()'}")

section("6 · COMPARAÇÃO DE PERÍODOS (analise-queda)")

current = conn.execute("""
    SELECT COALESCE(SUM(quantity * price), 0), COALESCE(COUNT(DISTINCT id), 0)
    FROM orders
    WHERE seller_id=? AND status != 'cancelled'
      AND datetime(substr(date_created,1,19)) >= datetime('now', '-30 days')
""", (SELLER_ID,)).fetchone()

previous = conn.execute("""
    SELECT COALESCE(SUM(quantity * price), 0), COALESCE(COUNT(DISTINCT id), 0)
    FROM orders
    WHERE seller_id=? AND status != 'cancelled'
      AND datetime(substr(date_created,1,19)) >= datetime('now', '-60 days')
      AND datetime(substr(date_created,1,19)) <  datetime('now', '-30 days')
""", (SELLER_ID,)).fetchone()

print(f"  {'':4} Período atual  (30d):     R$ {current[0]:,.2f}  ({current[1]} pedidos)")
print(f"  {'':4} Período anterior (30-60d): R$ {previous[0]:,.2f}  ({previous[1]} pedidos)")

if previous[0] > 0:
    delta_pct = (current[0] - previous[0]) / previous[0] * 100
    direction = "QUEDA" if delta_pct < 0 else "ALTA"
    sym = "✓" if delta_pct < 0 else "!"
    print(f"  {sym:<4} Variação: {delta_pct:+.1f}% ({direction})")
    print(f"  {'':4} Esperado: QUEDA de ~50.4%")
    if previous[1] < 50:
        print(f"  ✗    ATENÇÃO: período anterior tem poucos pedidos ({previous[1]}) —")
        print(f"  {'':4}           re-coleta com days=60 é necessária para comparação válida")
else:
    print(f"  ✗    Período anterior sem dados — re-coleta com days=60 necessária")

section("7 · ITENS NO CATÁLOGO")

r = conn.execute("SELECT COUNT(*) FROM items WHERE seller_id=?", (SELLER_ID,)).fetchone()
print(f"  {'':4} Itens no banco:           {r[0]}")

r2 = conn.execute("SELECT status, COUNT(*) FROM items WHERE seller_id=? GROUP BY status", (SELLER_ID,)).fetchall()
for row in r2:
    print(f"  {'':4}   {row[0]:<12} {row[1]}")

section("8 · RESUMO DE AÇÕES NECESSÁRIAS")

actions = []
# Check data freshness
if max_date:
    try:
        last_dt = datetime.fromisoformat(max_date[:19].replace("T", " ")).date()
        if (date.today() - last_dt).days > 1:
            actions.append(f"Re-coletar dados: /collect/Maximus (última coleta há {(date.today() - last_dt).days}d)")
    except Exception:
        pass

# Check orders count
orders_30d = conn.execute(
    "SELECT COUNT(DISTINCT id) FROM orders WHERE seller_id=? AND status != 'cancelled' AND datetime(substr(date_created,1,19)) >= datetime('now', '-30 days')",
    (SELLER_ID,)
).fetchone()[0]
if orders_30d < EXPECTED["orders_30d"] * 0.95:
    pct_found = orders_30d / EXPECTED["orders_30d"] * 100
    actions.append(f"Pedidos insuficientes: {orders_30d}/{EXPECTED['orders_30d']} ({pct_found:.0f}%) — re-coletar")

# Check previous period
if previous[1] < 50:
    actions.append("Período anterior (30-60d) insuficiente — re-coletar com days=60 para comparação")

# Check MP payments
if mp_sales[0] < 100:
    actions.append("Poucos pagamentos MP — executar coleta em /financeiro/SELLER_ID/collect")

if actions:
    for a in actions:
        print(f"  ✗    {a}")
else:
    print(f"  ✓    Dados parecem consistentes")

conn.close()
print()
