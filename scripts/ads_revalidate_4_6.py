#!/usr/bin/env python3
"""
Re-validação das Etapas 4-6 com o backfill corrigido (orders completos).
Objetivo: números absolutos exatos (receita_real, organic_revenue, tacos, lucro)
e confirmar o que acontece com attribution_exceeds_real da campanha 'True'.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta

ROOT = "/Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core"
SCRATCH = ("/private/tmp/claude-501/-Users-felipedicarlo/"
           "61c17962-4515-49cb-bc06-165a556019ed/scratchpad/aegis_revalidate.db")
shutil.copyfile(os.path.join(ROOT, "aegis.db"), SCRATCH)
os.environ["DB_PATH"] = SCRATCH
sys.path.insert(0, ROOT)

from app.database import init_ads_tables, get_account_by_name, get_conn  # noqa: E402
from app.services.ads_collector import collect_ads_account  # noqa: E402
from app.services import ads_metrics, ads_finance, ads_strategy, ads_diagnostic  # noqa: E402
from scripts.backfill_buyer_ids import backfill_account  # noqa: E402

MARGEM_ALVO = 10.0


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

    collect_ads_account(acc, token=tok, window_days=40)
    st = backfill_account(sid, tok, days=220)
    print(f"backfill: {st['linhas_antes']} -> {st['linhas_depois']} linhas "
          f"(+{st['linhas_novas']}), ainda_null={st['linhas_ainda_null']}")
    conn = get_conn()
    rng = conn.execute("SELECT MIN(date(date_created)), MAX(date(date_created)) "
                       "FROM orders WHERE seller_id=?", (sid,)).fetchone()
    cid_true = conn.execute("SELECT campaign_id_ml FROM campaigns WHERE name='True'").fetchone()[0]
    cid_pere = conn.execute("SELECT campaign_id_ml FROM campaigns WHERE name='Perecão'").fetchone()[0]
    conn.close()
    print(f"orders range agora: {tuple(rng)}")

    dt = date.today().isoformat()
    d30 = (date.today() - timedelta(days=30)).isoformat()

    hr("ETAPA 4 — campaign_metrics 'True' (funil + attribution_exceeds_real)")
    cm = ads_metrics.campaign_metrics(sid, cid_true, d30, dt)
    f = cm["funnel"]
    pj({k: f[k] for k in ("prints", "clicks", "cost", "ctr", "cpc", "acos", "roas", "cvr",
                          "ads_units", "ads_revenue", "real_revenue", "real_units", "real_orders",
                          "organic_revenue", "organic_share_pct", "tacos",
                          "attribution_exceeds_real", "days_with_prints")})
    print(f"\n>> attribution_exceeds_real = {f['attribution_exceeds_real']}  "
          f"(ads_revenue {f['ads_revenue']} vs real_revenue {f['real_revenue']})")

    hr("ETAPA 4 — campaign_finance 'True'")
    fin = ads_finance.campaign_finance(sid, cid_true, d30, dt, margem_alvo_pct=MARGEM_ALVO)["finance"]
    pj({k: fin[k] for k in ("receita_real", "receita_com_custo_conhecido", "ads_cost",
                            "ads_revenue_atribuida", "lucro_antes_ads", "lucro_depois_ads",
                            "margem_antes_ads_pct", "margem_depois_ads_pct", "acos_equilibrio_pct",
                            "roas_equilibrio", "roas_minimo_operacional", "custo_incompleto",
                            "n_itens", "n_itens_com_venda_e_custo")})

    hr("ETAPA 4 — ad_group_finance 1538648021")
    agf = ads_finance.ad_group_finance(sid, 1538648021, d30, dt, margem_alvo_pct=MARGEM_ALVO)["finance"]
    pj({k: agf[k] for k in ("receita_real", "ads_cost", "ads_revenue_atribuida", "lucro_antes_ads",
                            "lucro_depois_ads", "margem_antes_ads_pct", "margem_depois_ads_pct",
                            "roas_equilibrio", "roas_minimo_operacional")})
    agm = ads_metrics.ad_group_metrics(sid, 1538648021, d30, dt)["funnel"]
    print(f"   funil: organic_revenue {agm['organic_revenue']} ({agm['organic_share_pct']}%), "
          f"tacos {agm['tacos']}, attribution_exceeds_real {agm['attribution_exceeds_real']}")

    hr("ETAPA 4 — account_finance (Maximus)")
    af = ads_finance.account_finance(sid, d30, dt, margem_alvo_pct=MARGEM_ALVO)
    pj({"n_campanhas": af["n_campanhas"],
        "finance": {k: af["finance"][k] for k in (
            "receita_real", "ads_cost", "ads_revenue_atribuida", "lucro_antes_ads",
            "lucro_depois_ads", "margem_antes_ads_pct", "margem_depois_ads_pct",
            "roas_minimo_operacional", "n_itens", "n_itens_com_venda_e_custo")}})

    hr("ETAPA 5 — profile override flipa a régua (min_ads_orders)")
    ss0 = ads_metrics.sample_sufficiency(sid, "campaign", cid_true, d30, dt)
    ads_strategy.save_strategy_profile(sid, {"minimum_sample_rules": {"min_ads_orders": 9999}})
    ss1 = ads_metrics.sample_sufficiency(sid, "campaign", cid_true, d30, dt)
    ads_strategy.save_strategy_profile(sid, {"minimum_sample_rules": {"min_ads_orders": 5}})
    ss2 = ads_metrics.sample_sufficiency(sid, "campaign", cid_true, d30, dt)
    print(f"  default -> suficiente={ss0['suficiente']}  | override 9999 -> {ss1['suficiente']} "
          f"({ss1['motivo']})  | volta -> {ss2['suficiente']}")
    assert ss0["suficiente"] and not ss1["suficiente"] and ss2["suficiente"]

    hr("ETAPA 6 — diagnóstico (vereditos)")
    for scope, tid, label in (("campaign", cid_true, "'True'"),
                              ("ad_group", 1538648021, "ad_group 1538648021"),
                              ("campaign", cid_pere, "'Perecão'")):
        dg = ads_diagnostic.diagnose(sid, scope, tid, d30, dt)
        casos = [c["caso"] for c in dg["casos"] if c.get("avaliavel") and c["fatos"]]
        print(f"  {label:26s} status={dg['status']}  caso_primario={dg.get('caso_primario')}  casos={casos}")
        rec = ads_diagnostic.recommend(sid, scope, tid, d30, dt)
        print(f"     recommend: caso={rec.get('caso')} conf={rec.get('confianca')} :: {rec.get('proxima_acao')[:90]}")

    print("\n== fim ==")


if __name__ == "__main__":
    main()
