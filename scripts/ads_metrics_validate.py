#!/usr/bin/env python3
"""
Validação da Etapa 4 (Metrics + Finance Engine) contra a conta Maximus real.

Trabalha numa CÓPIA de scratch do banco. Puxa o token de produção via railway ssh,
coleta Maximus (Etapa 3) e roda os engines por cima:
  - campaign_metrics / campaign_finance da campanha "True"
  - ad_group_metrics / ad_group_finance de um ad group que veicula
  - o MESMO num ad group scaffold/órfão -> tem que vir serving=False, funnel=None
  - sample_sufficiency nos dois
  - sku_profit de um item
  - account_finance

Os números de Ads (impressão/clique/investimento/ROAS/ACOS/impression share) são
pra você conferir contra o painel do Mercado Ads.

Uso:
    cd /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core
    venv/bin/python scripts/ads_metrics_validate.py
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(
    "/private/tmp/claude-501/-Users-felipedicarlo/61c17962-4515-49cb-bc06-165a556019ed/scratchpad",
    "aegis_ads_metrics_validate.db",
)
shutil.copyfile(os.path.join(ROOT, "aegis.db"), SCRATCH)
os.environ["DB_PATH"] = SCRATCH
sys.path.insert(0, ROOT)

from app.database import (  # noqa: E402
    init_ads_tables, get_account_by_name, get_conn,
)
from app.services.ads_collector import collect_ads_account  # noqa: E402
from app.services import ads_metrics, ads_finance  # noqa: E402

MARGEM_ALVO = 10.0  # placeholder — Etapa 5 puxa de ads_strategy_profile


def prod_token(account="Maximus"):
    code = ("from app.database import get_account_by_name;"
            f"a=get_account_by_name({account!r});print(a['access_token'])")
    out = subprocess.check_output(
        ["railway", "ssh", f'cd /app && .venv/bin/python -c "{code}"'], text=True)
    for line in out.splitlines():
        if line.strip().startswith("APP_USR-"):
            return line.strip()
    raise SystemExit(f"token não encontrado:\n{out}")


def hr(t):
    print(f"\n{'═' * 80}\n{t}\n{'═' * 80}")


def pj(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main():
    init_ads_tables()
    token = prod_token("Maximus")
    acc = get_account_by_name("Maximus")
    print(f"token ok (len={len(token)}) — coletando Maximus (Etapa 3)…")
    collect_ads_account(acc, token=token, window_days=35)
    sid = acc["seller_id"]

    dt = date.today().isoformat()
    d30 = (date.today() - timedelta(days=30)).isoformat()

    conn = get_conn()
    cid_true = conn.execute("SELECT campaign_id_ml FROM campaigns WHERE name='True'").fetchone()[0]
    # ad group que veicula (tem total_amount no período)
    ag_serving = conn.execute(
        "SELECT ad_group_id, SUM(total_amount) rev FROM ad_group_metrics_daily "
        "WHERE campaign_id=? GROUP BY ad_group_id HAVING rev>0 ORDER BY rev DESC LIMIT 1",
        (cid_true,)).fetchone()
    ag_serving_id = ag_serving[0] if ag_serving else None
    # ad group scaffold (órfão ou EMPTY)
    ag_scaffold = conn.execute(
        "SELECT ad_group_id_ml FROM ad_groups WHERE seller_id=? AND is_scaffold=1 LIMIT 1",
        (sid,)).fetchone()
    ag_scaffold_id = ag_scaffold[0] if ag_scaffold else None
    n_scaffold = conn.execute(
        "SELECT COUNT(*) FROM ad_groups WHERE seller_id=? AND is_scaffold=1", (sid,)).fetchone()[0]
    conn.close()

    print(f"campanha 'True' = {cid_true} | ad_group veiculando = {ag_serving_id} | "
          f"ad_group scaffold = {ag_scaffold_id} ({n_scaffold} scaffold no total)")

    hr(f"1) campaign_metrics — 'True' ({d30}..{dt})")
    cm = ads_metrics.campaign_metrics(sid, cid_true, d30, dt)
    pj({k: cm[k] for k in ("serving", "status", "n_items", "window")})
    print("\nfunnel:")
    pj(cm["funnel"])
    print("\nimpression_share (média ponderada por prints):")
    pj(cm["impression_share"])
    print(f"\ndaily_series: {len(cm['daily_series'])} dias; últimos 2:")
    pj(cm["daily_series"][-2:])

    hr(f"2) campaign_finance — 'True' (margem_alvo={MARGEM_ALVO}%)")
    cf = ads_finance.campaign_finance(sid, cid_true, d30, dt, margem_alvo_pct=MARGEM_ALVO)
    fin = cf["finance"]
    pj({k: fin[k] for k in (
        "receita_real", "receita_com_custo_conhecido", "ads_cost", "ads_revenue_atribuida",
        "lucro_antes_ads", "lucro_depois_ads", "margem_antes_ads_pct", "margem_depois_ads_pct",
        "acos_equilibrio_pct", "roas_equilibrio", "roas_minimo_operacional",
        "margem_alvo_inatingivel", "custo_incompleto", "n_itens",
        "n_itens_com_venda_e_custo")})
    print("\nby_sku (top 3 por lucro):")
    top = sorted(fin["by_sku"].items(), key=lambda kv: kv[1]["lucro_antes_ads"], reverse=True)[:3]
    pj(dict(top))

    hr(f"3) sample_sufficiency — campanha 'True'")
    pj(ads_metrics.sample_sufficiency(sid, "campaign", cid_true, d30, dt))

    if ag_serving_id:
        hr(f"4) ad_group_metrics — {ag_serving_id} (veicula)")
        am = ads_metrics.ad_group_metrics(sid, ag_serving_id, d30, dt)
        pj({k: am[k] for k in ("serving", "status", "ad_group_type", "sku_level",
                               "is_scaffold", "n_items", "item_ids", "tags")})
        print("\nfunnel:")
        pj(am["funnel"])
        hr(f"5) ad_group_finance — {ag_serving_id}")
        agf = ads_finance.ad_group_finance(sid, ag_serving_id, d30, dt, margem_alvo_pct=MARGEM_ALVO)
        pj(agf["finance"])
        hr(f"6) sample_sufficiency — ad_group {ag_serving_id}")
        pj(ads_metrics.sample_sufficiency(sid, "ad_group", ag_serving_id, d30, dt))

    if ag_scaffold_id:
        hr(f"7) ad_group SCAFFOLD {ag_scaffold_id} — NÃO pode vir como 'performance zero'")
        am = ads_metrics.ad_group_metrics(sid, ag_scaffold_id, d30, dt)
        pj({k: am[k] for k in ("serving", "status", "not_serving_reason", "is_scaffold",
                               "funnel")})
        agf = ads_finance.ad_group_finance(sid, ag_scaffold_id, d30, dt, margem_alvo_pct=MARGEM_ALVO)
        pj({k: agf[k] for k in ("serving", "status", "not_serving_reason", "finance")})
        ss = ads_metrics.sample_sufficiency(sid, "ad_group", ag_scaffold_id, d30, dt)
        pj({k: ss.get(k) for k in ("suficiente", "motivo", "not_serving_reason", "numeros")})
        assert am["funnel"] is None and agf["finance"] is None, "SCAFFOLD retornou números!"
        assert ss["motivo"] == ["nao_veiculando"], "sample_sufficiency scaffold errado!"
        print("\n   OK: scaffold -> serving=False, funnel=None, finance=None, motivo=['nao_veiculando']")

    hr("8) sku_profit — um item do ad group que veicula")
    if ag_serving_id:
        conn = get_conn()
        it = conn.execute("SELECT item_id FROM ad_group_items WHERE ad_group_id=? LIMIT 1",
                          (ag_serving_id,)).fetchone()
        conn.close()
        if it:
            pj(ads_finance.sku_profit(sid, it[0], d30, dt))

    hr("9) account_finance — Maximus (roll-up)")
    af = ads_finance.account_finance(sid, d30, dt, margem_alvo_pct=MARGEM_ALVO)
    pj({"n_campanhas": af["n_campanhas"], "campanhas": af["campanhas"],
        "finance": af["finance"]})

    print("\n== fim ==")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"railway ssh falhou: {e}", file=sys.stderr)
        sys.exit(1)
