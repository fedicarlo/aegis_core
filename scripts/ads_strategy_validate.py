#!/usr/bin/env python3
"""
Validação da Etapa 5 (Strategy Engine) contra Maximus real, em cópia de scratch.

Prova o ponto central: mudar um limiar no ads_strategy_profile muda o comportamento
dos engines (régua de amostra, ROAS mínimo operacional) SEM tocar em código.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta

ROOT = "/Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core"
SCRATCH = ("/private/tmp/claude-501/-Users-felipedicarlo/"
           "61c17962-4515-49cb-bc06-165a556019ed/scratchpad/aegis_ads_strategy_validate.db")
shutil.copyfile(os.path.join(ROOT, "aegis.db"), SCRATCH)
os.environ["DB_PATH"] = SCRATCH
sys.path.insert(0, ROOT)

from app.database import init_ads_tables, get_account_by_name, get_conn  # noqa: E402
from app.services.ads_collector import collect_ads_account  # noqa: E402
from app.services import ads_metrics, ads_finance, ads_strategy  # noqa: E402
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
    print(f"\n{'═' * 78}\n{t}\n{'═' * 78}")


def pj(o):
    print(json.dumps(o, ensure_ascii=False, indent=2, default=str))


def main():
    init_ads_tables()
    tok = prod_token("Maximus")
    acc = get_account_by_name("Maximus")
    sid = acc["seller_id"]

    hr("1) seed_default_profile()")
    written = ads_strategy.seed_default_profile()
    print(f"   grupos materializados no profile global: {written}")
    print(f"   2a chamada (idempotente): {ads_strategy.seed_default_profile()}")

    hr("2) get_strategy_profile(None) — profile global efetivo")
    pj(ads_strategy.get_strategy_profile(None))

    hr("3) override por seller — save_strategy_profile(Maximus, minimum_sample_rules.min_ads_orders=999)")
    ads_strategy.save_strategy_profile(sid, {"minimum_sample_rules": {"min_ads_orders": 999}})
    prof = ads_strategy.get_strategy_profile(sid)
    print("   minimum_sample_rules do seller:")
    pj(prof["minimum_sample_rules"])
    assert prof["minimum_sample_rules"]["min_ads_orders"] == 999
    assert prof["minimum_sample_rules"]["min_clicks"] == 30, "merge apagou chave do default!"
    assert prof["minimum_sample_rules"]["_source"] == "seller"
    print("   OK: override aplicado, demais chaves preservadas (merge), _source=seller")

    hr("4) coleta Ads + backfill buyer_id (p/ ter dado real)")
    collect_ads_account(acc, token=tok, window_days=35)
    st = backfill_account(sid, tok, days=220)
    print(f"   buyer_id: {st['linhas_atualizadas']} linhas, ainda_null={st['linhas_ainda_null']}")

    dt = date.today().isoformat()
    d30 = (date.today() - timedelta(days=30)).isoformat()
    conn = get_conn()
    cid = conn.execute("SELECT campaign_id_ml FROM campaigns WHERE name='True'").fetchone()[0]
    conn.close()

    hr("5) MESMA campanha, MESMO código — veredito muda pelo profile")
    print("   5a) régua com override do seller ativo (min_ads_orders=999):")
    ss = ads_metrics.sample_sufficiency(sid, "campaign", cid, d30, dt)
    pj({k: ss.get(k) for k in ("suficiente", "motivo", "numeros")})
    assert ss["suficiente"] is False and "poucas_vendas_ads" in ss["motivo"]

    print("\n   5b) removo o override (volto pro default global min_ads_orders=5):")
    ads_strategy.save_strategy_profile(sid, {"minimum_sample_rules": {"min_ads_orders": 5}})
    ss2 = ads_metrics.sample_sufficiency(sid, "campaign", cid, d30, dt)
    pj({k: ss2.get(k) for k in ("suficiente", "motivo")})
    assert ss2["suficiente"] is True
    print("   OK: suficiente False -> True só mexendo no profile, zero mudança de código")

    hr("6) profit_targets -> ROAS mínimo operacional (Finance)")
    f_default = ads_finance.campaign_finance(sid, cid, d30, dt)["finance"]
    print(f"   margem_alvo default = {f_default['margem_alvo_pct']}%  "
          f"-> roas_minimo_operacional = {f_default['roas_minimo_operacional']}")
    ads_strategy.save_strategy_profile(sid, {"profit_targets": {"margem_alvo_pct": 5.0}})
    f_5 = ads_finance.campaign_finance(sid, cid, d30, dt)["finance"]
    print(f"   margem_alvo 5% (via profile) -> roas_minimo_operacional = {f_5['roas_minimo_operacional']}")
    assert f_5["margem_alvo_pct"] == 5.0
    assert f_5["roas_minimo_operacional"] != f_default["roas_minimo_operacional"]
    print("   OK: Finance puxou margem_alvo do profile; parâmetro explícito ainda vence se passado")

    ov = ads_finance.campaign_finance(sid, cid, d30, dt, margem_alvo_pct=20.0)["finance"]
    print(f"   parâmetro explícito margem_alvo=20% -> {ov['roas_minimo_operacional']} "
          f"(margem_alvo_inatingivel={ov['margem_alvo_inatingivel']})")

    print("\n== fim — todos os asserts passaram ==")


if __name__ == "__main__":
    main()
