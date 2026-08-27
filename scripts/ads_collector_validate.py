#!/usr/bin/env python3
"""
Validação da Etapa 3 (Ads Data Provider) contra a conta Maximus real.

NÃO toca no aegis.db de produção nem no local: trabalha numa CÓPIA de scratch.
Puxa o access_token atual do Maximus de produção via `railway ssh` e passa
explícito pro collector (o token local está expirado).

Checa:
  1. collect_ads_account() roda e persiste (contadores)
  2. dia corrente NÃO é persistido (max(date) < hoje GMT-3)
  3. idempotência: 2a execução não infla as contagens
  4. snapshot + ads_events: adultera um snapshot e re-coleta -> gera evento
  5. ponte ad_group_items: grupo CATALOG/FAMILY com N itens, grupo ITEM com 1 sintético

Uso:
    cd /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core
    venv/bin/python scripts/ads_collector_validate.py
"""
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(
    "/private/tmp/claude-501/-Users-felipedicarlo/61c17962-4515-49cb-bc06-165a556019ed/scratchpad",
    "aegis_ads_validate.db",
)

# DB_PATH tem que estar setado ANTES de importar app.config
shutil.copyfile(os.path.join(ROOT, "aegis.db"), SCRATCH)
os.environ["DB_PATH"] = SCRATCH
sys.path.insert(0, ROOT)

from app.database import (  # noqa: E402
    get_conn, get_account_by_name, init_ads_tables, ads_row_counts,
)
from app.services.ads_collector import collect_ads_account, _today_ml  # noqa: E402


def prod_token(account="Maximus"):
    code = (
        "from app.database import get_account_by_name;"
        f"a=get_account_by_name({account!r});print(a['access_token'])"
    )
    out = subprocess.check_output(
        ["railway", "ssh", f'cd /app && .venv/bin/python -c "{code}"'], text=True)
    for line in out.splitlines():
        if line.strip().startswith("APP_USR-"):
            return line.strip()
    raise SystemExit(f"token não encontrado na saída:\n{out}")


def q(sql, *args):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


def hr(t):
    print(f"\n{'─' * 78}\n{t}\n{'─' * 78}")


def main():
    print(f"scratch DB: {SCRATCH}")
    init_ads_tables()  # aplica migration date_created_ml/tags na cópia

    token = prod_token("Maximus")
    print(f"token de produção puxado (len={len(token)})")
    acc = get_account_by_name("Maximus")
    print(f"conta: {acc['name']} seller_id={acc['seller_id']}  hoje(GMT-3)={_today_ml()}")

    hr("1) collect_ads_account — 1a execução")
    r1 = collect_ads_account(acc, token=token, window_days=35)
    for k, v in r1.items():
        if k != "errors":
            print(f"   {k:24s}: {v}")
    if r1["errors"]:
        print("   ERROS:")
        for e in r1["errors"]:
            print(f"     - {e}")

    hr("contagem por tabela (seller Maximus)")
    counts1 = ads_row_counts(acc["seller_id"])
    for k, v in counts1.items():
        print(f"   {k:28s}: {v}")

    hr("2) dia corrente NÃO persistido")
    for tbl in ("campaign_metrics_daily", "campaign_metrics_detail", "ad_group_metrics_daily"):
        row = q(f"SELECT MIN(date) mn, MAX(date) mx, COUNT(*) n FROM {tbl}")[0]
        ok = (row["mx"] or "9999") < _today_ml()
        print(f"   {tbl:24s}: {row['n']} linhas, range {row['mn']}..{row['mx']}  "
              f"-> max < hoje? {'OK' if ok else 'FALHOU'}")

    hr("amostra: campanha")
    for c in q("SELECT campaign_id_ml, name, status, strategy, budget, automatic_budget, "
               "acos_target, roas_target, currency_id FROM campaigns"):
        print(f"   {c}")

    hr("amostra: campaign_metrics_daily (campanha 'True', últimos 3 dias)")
    cid = q("SELECT campaign_id_ml FROM campaigns WHERE name='True'")[0]["campaign_id_ml"]
    for m in q("SELECT date, clicks, prints, cost, units_quantity, total_amount, acos, roas "
               "FROM campaign_metrics_daily WHERE campaign_id=? ORDER BY date DESC LIMIT 3", cid):
        print(f"   {m}")

    hr("amostra: campaign_metrics_detail (impression share, últimos 3 dias)")
    for m in q("SELECT date, impression_share, top_impression_share, "
               "lost_impression_share_by_budget, lost_impression_share_by_ad_rank, acos_benchmark "
               "FROM campaign_metrics_detail WHERE campaign_id=? ORDER BY date DESC LIMIT 3", cid):
        print(f"   {m}")

    hr("amostra: ad_groups (distribuição + date_created_ml + tags)")
    for row in q("SELECT ad_group_type, COUNT(*) n, COUNT(date_created_ml) com_data, "
                 "SUM(CASE WHEN tags NOT IN ('[]','') AND tags IS NOT NULL THEN 1 ELSE 0 END) com_tags "
                 "FROM ad_groups GROUP BY ad_group_type"):
        print(f"   {row}")
    for row in q("SELECT ad_group_id_ml, ad_group_type, ad_group_external_id, title, "
                 "date_created_ml, tags FROM ad_groups WHERE tags NOT IN ('[]','') LIMIT 3"):
        print(f"   {row}")

    hr("amostra: ad_group_items (ponte 1→N)")
    for row in q("SELECT g.ad_group_type, COUNT(DISTINCT i.item_id) itens "
                 "FROM ad_group_items i JOIN ad_groups g ON g.ad_group_id_ml=i.ad_group_id "
                 "GROUP BY g.ad_group_type"):
        print(f"   {row}")
    print("   exemplo grupo CATALOG/FAMILY com múltiplos itens:")
    multi = q("SELECT i.ad_group_id, COUNT(*) n FROM ad_group_items i "
              "JOIN ad_groups g ON g.ad_group_id_ml=i.ad_group_id "
              "WHERE g.ad_group_type IN ('CATALOG','FAMILY') GROUP BY i.ad_group_id "
              "HAVING n>1 LIMIT 1")
    if multi:
        agid = multi[0]["ad_group_id"]
        for row in q("SELECT item_id, family_id, user_product_id, current_level, buy_box_winner, "
                     "listing_type_id FROM ad_group_items WHERE ad_group_id=?", agid):
            print(f"     {row}")
    print("   exemplo grupo ITEM (1 item sintético = external_id):")
    for row in q("SELECT i.ad_group_id, i.item_id, g.ad_group_external_id "
                 "FROM ad_group_items i JOIN ad_groups g ON g.ad_group_id_ml=i.ad_group_id "
                 "WHERE g.ad_group_type='ITEM' LIMIT 2"):
        print(f"     {row}")

    hr("amostra: ad_group_metrics_daily")
    for m in q("SELECT ad_group_id, date, clicks, prints, cost, units_quantity, total_amount, acos "
               "FROM ad_group_metrics_daily WHERE total_amount > 0 ORDER BY date DESC LIMIT 3"):
        print(f"   {m}")
    print(f"   tacos (deve ser tudo NULL nesta etapa): "
          f"{q('SELECT COUNT(*) n, COUNT(tacos) com_tacos FROM ad_group_metrics_daily')[0]}")

    hr("3) idempotência — 2a execução")
    r2 = collect_ads_account(acc, token=token, window_days=35)
    counts2 = ads_row_counts(acc["seller_id"])
    diffs = {k: (counts1[k], counts2[k]) for k in counts1 if counts1[k] != counts2[k]}
    print(f"   tabelas que mudaram de contagem: {diffs or 'NENHUMA (idempotente OK)'}")

    hr("4) snapshot + ads_events — adultera snapshot e re-coleta")
    snap = q("SELECT id, campaign_id, budget FROM campaign_target_snapshots "
             "ORDER BY snapshot_at DESC LIMIT 1")
    if snap:
        s = snap[0]
        conn = get_conn()
        conn.execute("UPDATE campaign_target_snapshots SET budget = budget + 1234 WHERE id=?", (s["id"],))
        conn.commit()
        conn.close()
        print(f"   snapshot id={s['id']} campaign={s['campaign_id']}: budget {s['budget']} -> {s['budget']+1234} (adulterado)")
        events_before = q("SELECT COUNT(*) n FROM ads_events")[0]["n"]
        collect_ads_account(acc, token=token, window_days=35)
        ev = q("SELECT scope, target_id, field, old_value, new_value, author, source "
               "FROM ads_events ORDER BY changed_at DESC LIMIT 5")
        print(f"   ads_events antes={events_before}, depois={q('SELECT COUNT(*) n FROM ads_events')[0]['n']}")
        for e in ev:
            print(f"     {e}")
        newsnap = q("SELECT COUNT(*) n FROM campaign_target_snapshots WHERE campaign_id=?", s["campaign_id"])[0]["n"]
        print(f"   snapshots da campanha {s['campaign_id']}: {newsnap} (deve ter +1)")

    hr("resumo de erros das 3 execuções")
    allerr = r1["errors"] + r2["errors"]
    print(f"   {len(allerr)} erro(s)" + ("" if not allerr else ":"))
    for e in allerr:
        print(f"     - {e}")

    print("\n== fim ==")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"railway ssh falhou: {e}", file=sys.stderr)
        sys.exit(1)
