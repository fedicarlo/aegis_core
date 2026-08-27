#!/usr/bin/env python3
"""Validação da Etapa 6 (Diagnostic + Recommendation Engine) — casos A-E contra Maximus."""
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta

ROOT = "/Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core"
SCRATCH = ("/private/tmp/claude-501/-Users-felipedicarlo/"
           "61c17962-4515-49cb-bc06-165a556019ed/scratchpad/aegis_ads_diag_validate.db")
shutil.copyfile(os.path.join(ROOT, "aegis.db"), SCRATCH)
os.environ["DB_PATH"] = SCRATCH
sys.path.insert(0, ROOT)

from app.database import init_ads_tables, get_account_by_name, get_conn  # noqa: E402
from app.services.ads_collector import collect_ads_account  # noqa: E402
from app.services import ads_diagnostic, ads_strategy  # noqa: E402
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


def show(dg, rec):
    print(f"  status={dg['status']}  serving={dg['serving']}  caso_primario={dg.get('caso_primario')}")
    if not dg["serving"]:
        print(f"  not_serving_reason={dg['not_serving_reason']}")
    if dg.get("amostra"):
        print(f"  amostra: suficiente={dg['amostra']['suficiente']} motivo={dg['amostra']['motivo']}")
    for c in dg.get("casos", []):
        tag = "PRIMÁRIO" if c["caso"] == dg.get("caso_primario") else (
            "informativo" if not c.get("avaliavel") else "relacionado")
        print(f"\n  ── Caso {c['caso']} ({c['nome']}) [{tag}]")
        if not c.get("avaliavel"):
            print(f"     não avaliável: {c.get('motivo_nao_avaliavel')}")
            continue
        for x in c["fatos"]:
            print(f"     FATO: {x}")
        for h in c["hipoteses"]:
            print(f"     HIPÓTESE ({h['confianca']}): {h['texto']}")
        print(f"     AÇÃO: {c['acao_sugerida']}")
    print(f"\n  >> recommend(): caso={rec.get('caso')} confianca={rec.get('confianca')}")
    print(f"     próxima ação: {rec.get('proxima_acao')}")
    if rec.get("casos_relacionados"):
        print(f"     casos relacionados: {rec['casos_relacionados']}")
    print(f"  estagio: {dg.get('estagio')}")


def main():
    init_ads_tables()
    ads_strategy.seed_default_profile()
    tok = prod_token("Maximus")
    acc = get_account_by_name("Maximus")
    sid = acc["seller_id"]
    print("coletando Ads + backfill buyer_id…")
    collect_ads_account(acc, token=tok, window_days=35)
    backfill_account(sid, tok, days=220)

    dt = date.today().isoformat()
    d30 = (date.today() - timedelta(days=30)).isoformat()

    conn = get_conn()
    camps = [dict(r) for r in conn.execute(
        "SELECT campaign_id_ml, name, status FROM campaigns ORDER BY name")]
    ag_serv = conn.execute(
        "SELECT m.ad_group_id, SUM(m.total_amount) rev, SUM(m.units_quantity) un "
        "FROM ad_group_metrics_daily m JOIN ad_groups g ON g.ad_group_id_ml=m.ad_group_id "
        "WHERE g.is_scaffold=0 GROUP BY m.ad_group_id HAVING rev>0 ORDER BY rev DESC LIMIT 1"
    ).fetchone()
    ag_low = conn.execute(
        "SELECT m.ad_group_id, SUM(m.prints) pr, SUM(m.units_quantity) un, SUM(m.clicks) cl "
        "FROM ad_group_metrics_daily m JOIN ad_groups g ON g.ad_group_id_ml=m.ad_group_id "
        "WHERE g.is_scaffold=0 GROUP BY m.ad_group_id "
        "HAVING pr>500 AND un<8 AND cl<60 ORDER BY pr DESC LIMIT 1"
    ).fetchone()
    ag_scaffold = conn.execute(
        "SELECT ad_group_id_ml FROM ad_groups WHERE seller_id=? AND is_scaffold=1 LIMIT 1", (sid,)
    ).fetchone()
    conn.close()

    for c in camps:
        hr(f"CAMPANHA '{c['name']}' ({c['campaign_id_ml']}, status_ml={c['status']})")
        dg = ads_diagnostic.diagnose(sid, "campaign", c["campaign_id_ml"], d30, dt)
        rec = ads_diagnostic.recommend(sid, "campaign", c["campaign_id_ml"], d30, dt)
        show(dg, rec)

    if ag_serv:
        agid = ag_serv["ad_group_id"]
        hr(f"AD GROUP {agid} (veicula — rev R${ag_serv['rev']:.0f}, {ag_serv['un']} un) — esperado Caso D")
        show(ads_diagnostic.diagnose(sid, "ad_group", agid, d30, dt),
             ads_diagnostic.recommend(sid, "ad_group", agid, d30, dt))

    if ag_low:
        agid = ag_low["ad_group_id"]
        hr(f"AD GROUP {agid} (baixo volume — {ag_low['pr']} prints, {ag_low['cl']} cliques, "
           f"{ag_low['un']} un) — esperado Caso E")
        show(ads_diagnostic.diagnose(sid, "ad_group", agid, d30, dt),
             ads_diagnostic.recommend(sid, "ad_group", agid, d30, dt))

    if ag_scaffold:
        agid = ag_scaffold["ad_group_id_ml"]
        hr(f"AD GROUP {agid} (scaffold) — esperado NAO_VEICULANDO, sem diagnóstico")
        dg = ads_diagnostic.diagnose(sid, "ad_group", agid, d30, dt)
        rec = ads_diagnostic.recommend(sid, "ad_group", agid, d30, dt)
        show(dg, rec)
        assert dg["status"] == "NAO_VEICULANDO" and not dg["casos"]
        assert "não está veiculando" in rec["proxima_acao"]

    print("\n== fim ==")


if __name__ == "__main__":
    main()
