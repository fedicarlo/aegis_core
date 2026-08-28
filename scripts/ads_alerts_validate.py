#!/usr/bin/env python3
"""Validação da Etapa 7 (Alert Engine + Eventos/Experimentos) contra Maximus."""
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta

ROOT = "/Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core"
SCRATCH = ("/private/tmp/claude-501/-Users-felipedicarlo/"
           "61c17962-4515-49cb-bc06-165a556019ed/scratchpad/aegis_ads_alerts_validate.db")
shutil.copyfile(os.path.join(ROOT, "aegis.db"), SCRATCH)
os.environ["DB_PATH"] = SCRATCH
sys.path.insert(0, ROOT)

from app.database import (  # noqa: E402
    init_ads_tables, get_account_by_name, get_conn, get_ads_alerts,
)
from app.services.ads_collector import collect_ads_account  # noqa: E402
from app.services import ads_alerts, ads_experiments, ads_strategy  # noqa: E402
from scripts.backfill_buyer_ids import backfill_account  # noqa: E402


def prod_token(account="Maximus"):
    code = ("from app.database import get_account_by_name;"
            f"a=get_account_by_name({account!r});print(a['access_token'])")
    out = subprocess.check_output(
        ["railway", "ssh", f'cd /app && .venv/bin/python -c "{code}"'], text=True)
    for line in out.splitlines():
        if line.strip().startswith("APP_USR-"):
            return line.strip()
    raise SystemExit("token não encontrado")


def hr(t):
    print(f"\n{'═' * 82}\n{t}\n{'═' * 82}")


def pj(o):
    print(json.dumps(o, ensure_ascii=False, indent=2, default=str))


def main():
    init_ads_tables()
    ads_strategy.seed_default_profile()
    tok = prod_token("Maximus")
    acc = get_account_by_name("Maximus")
    sid = acc["seller_id"]
    print("coletando Ads + backfill…")
    collect_ads_account(acc, token=tok, window_days=40)
    backfill_account(sid, tok, days=220)

    conn = get_conn()
    camps = [dict(r) for r in conn.execute(
        "SELECT campaign_id_ml, name FROM campaigns ORDER BY name")]
    ag_serv = conn.execute(
        "SELECT m.ad_group_id, SUM(m.units_quantity) un FROM ad_group_metrics_daily m "
        "JOIN ad_groups g ON g.ad_group_id_ml=m.ad_group_id WHERE g.is_scaffold=0 "
        "GROUP BY m.ad_group_id HAVING un>=10 ORDER BY un DESC LIMIT 1").fetchone()
    conn.close()

    hr("1) ALERTAS NATURAIS — 4 campanhas da Maximus")
    for c in camps:
        r = ads_alerts.run_alerts_for_target(sid, "campaign", c["campaign_id_ml"])
        tipos = [a["tipo"] for a in r["alerts"]]
        print(f"  {c['name']:22s} recente={r['recent']['from']}..{r['recent']['to']}  "
              f"serving={r['serving_recent']}  alertas={tipos or '(nenhum)'}")
        for a in r["alerts"]:
            print(f"      [{a['severidade']}] {a['tipo']}: {json.dumps(a['evidencia'], ensure_ascii=False)}")

    hr("2) CENÁRIO SINTÉTICO — 'parou_de_vender' (zera janela recente de um ad group)")
    agid = ag_serv["ad_group_id"]
    rl = ads_strategy.risk_limits(sid)
    today = date.today()
    r_from = (today - timedelta(days=rl["alert_recent_days"])).isoformat()
    conn = get_conn()
    before = conn.execute("SELECT SUM(units_quantity) FROM ad_group_metrics_daily WHERE ad_group_id=?",
                          (agid,)).fetchone()[0]
    conn.execute("UPDATE ad_group_metrics_daily SET units_quantity=0, direct_units_quantity=0, "
                 "indirect_units_quantity=0, total_amount=0, direct_amount=0, indirect_amount=0, "
                 "direct_items_quantity=0, indirect_items_quantity=0 "
                 "WHERE ad_group_id=? AND date>=?", (agid, r_from))
    conn.commit()
    conn.close()
    r = ads_alerts.run_alerts_for_target(sid, "ad_group", agid)
    print(f"  ad group {agid} (vendia {before} un no total) — alertas: {[a['tipo'] for a in r['alerts']]}")
    for a in r["alerts"]:
        print(f"      [{a['severidade']}] {a['tipo']}: {a['evidencia']}  ->  {a['acao_sugerida']}")
    assert any(a["tipo"] == "parou_de_vender" for a in r["alerts"])

    hr("3) CENÁRIO SINTÉTICO — 'cpc_disparou' (infla CPC/custo da janela recente de 'True')")
    cid = next(c["campaign_id_ml"] for c in camps if c["name"] == "True")
    conn = get_conn()
    conn.execute("UPDATE campaign_metrics_daily SET cpc = cpc*5, cost = cost*5 "
                 "WHERE campaign_id=? AND date>=?", (cid, r_from))
    conn.commit()
    conn.close()
    r = ads_alerts.run_alerts_for_target(sid, "campaign", cid)
    print(f"  campanha 'True' — alertas: {[a['tipo'] for a in r['alerts']]}")
    for a in r["alerts"]:
        print(f"      [{a['severidade']}] {a['tipo']}: {json.dumps(a['evidencia'], ensure_ascii=False)}")
    assert any(a["tipo"] == "cpc_disparou" for a in r["alerts"])

    hr("4) DEDUP — roda de novo, não pode duplicar")
    n1 = len(get_ads_alerts(sid, only_open=True))
    ads_alerts.run_alerts_for_target(sid, "campaign", cid)
    ads_alerts.run_alerts_for_target(sid, "ad_group", agid)
    n2 = len(get_ads_alerts(sid, only_open=True))
    print(f"  alertas abertos antes={n1}, depois de re-rodar={n2}  -> {'OK (dedup)' if n1 == n2 else 'DUPLICOU!'}")
    assert n1 == n2

    hr("5) EVENTOS — registro manual + timeline (sistema + manual)")
    ads_experiments.record_manual_event(
        sid, "campaign", cid, "roas_target", old_value=14, new_value=12,
        author="lipe", motivo="margem depois de Ads abaixo da meta",
        hipotese="baixar o alvo ganha volume sem estourar ACOS")
    tl = ads_experiments.timeline(sid, "campaign", cid, limit=10)
    for e in tl:
        print(f"  {e['changed_at_iso'][:19]}  [{e['source']}] {e['field']}: "
              f"{e['old_value']} -> {e['new_value']}  "
              f"{'(' + e['author'] + ': ' + (e['motivo'] or '') + ')' if e['author'] else ''}")

    hr("6) EXPERIMENTO — create + evaluate (Before/After simétrico)")
    xid = ads_experiments.create(
        sid, "campaign", cid,
        hipotese="baixar ROAS-alvo de 14 p/ 12 aumenta volume mantendo margem",
        intervencao="roas_target 14 -> 12",
        janela_inicio=(today - timedelta(days=12)).isoformat())
    ev = ads_experiments.evaluate(xid, persist=True)
    print(f"  experimento #{xid}: {ev['hipotese']}")
    print(f"  Before {ev['before_window']}  |  After {ev['after_window']}")
    print(f"  ressalva de amostra: {ev['ressalva_amostra'] or 'nenhuma'}")
    for k in ("prints", "clicks", "cost", "cpc", "acos", "roas", "cvr", "ads_units",
              "ads_revenue", "real_revenue", "tacos"):
        c = ev["comparacao"][k]
        print(f"    {k:14s} antes={c['antes']}  depois={c['depois']}  "
              f"Δ={c['delta']}  ({c['delta_pct']}%)")
    saved = ads_experiments.get(xid)
    assert saved["resultado"] and saved["status"] in ("aberto", "concluido")
    print(f"  resultado persistido em ads_experiments.resultado (status={saved['status']})")

    print("\n== fim — asserts OK ==")


if __name__ == "__main__":
    main()
