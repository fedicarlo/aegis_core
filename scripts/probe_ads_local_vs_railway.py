#!/usr/bin/env python3
"""
Probe READ-ONLY do cliente app/services/ads_api.py contra a conta Maximus real.

Exercita as 6 funções do fluxo novo de Product Ads e imprime uma amostra de cada
resposta. Não grava nada, não toca em banco.

Puxa o access_token atual do Maximus do banco de PRODUÇÃO via `railway ssh`
(roda no teu Mac, com as tuas credenciais Railway) — o aegis.db local tem token
expirado e refresh_token já rotacionado pelo scheduler de produção.

Uso:
    cd /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core
    venv/bin/python scripts/probe_ads_local_vs_railway.py [NOME_CONTA]
"""
import json
import subprocess
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from app.services import ads_api

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "Maximus"


def get_prod_token(account):
    code = (
        "from app.database import get_account_by_name;"
        f"a=get_account_by_name({account!r});"
        "print(a['access_token'])"
    )
    out = subprocess.check_output(
        ["railway", "ssh", f'cd /app && .venv/bin/python -c "{code}"'],
        text=True,
    )
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("APP_USR-"):
            return line
    raise SystemExit(f"não consegui extrair o token da saída:\n{out}")


def show(label, obj, depth=3500):
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str)[:depth])


def main():
    print(f"== puxando token de produção (conta {ACCOUNT}) via railway ssh ==")
    token = get_prod_token(ACCOUNT)
    print(f"   token ok, len={len(token)}, prefixo={token[:16]}...")

    dt = date.today().isoformat()
    d30 = (date.today() - timedelta(days=30)).isoformat()
    d7 = (date.today() - timedelta(days=7)).isoformat()

    # ── 1. get_advertisers ────────────────────────────────────────────────
    advertisers = ads_api.get_advertisers(token)
    show("1) get_advertisers(token)", advertisers)
    if not advertisers:
        print("!! sem advertisers — conta sem Product Ads. Parando.")
        return
    adv = advertisers[0]
    adv_id, site = adv["advertiser_id"], adv["site_id"]
    print(f"\n   -> advertiser_id={adv_id}  site_id={site}")

    # ── 2. search_campaigns ──────────────────────────────────────────────
    camp = ads_api.search_campaigns(token, site, adv_id, d30, dt)
    show(f"2) search_campaigns(site={site}, adv={adv_id}, {d30}..{dt})",
         {"total": camp["total"],
          "metrics_summary": camp["metrics_summary"],
          "results[0]": camp["results"][0] if camp["results"] else None,
          "results_count": len(camp["results"])}, depth=4500)
    if not camp["results"]:
        print("!! sem campanhas. Parando.")
        return
    cid = camp["results"][0]["id"]

    # ── 3. get_campaign_detail (período + diário) ───────────────────────
    detail = ads_api.get_campaign_detail(token, site, cid, d30, dt)
    show(f"3a) get_campaign_detail(cid={cid}, {d30}..{dt})  [período]", detail)

    daily = ads_api.get_campaign_detail(token, site, cid, d7, dt, daily=True)
    show(f"3b) get_campaign_detail(cid={cid}, daily=True)  [{len(daily)} linhas]",
         {"n_linhas": len(daily), "primeira": daily[0] if daily else None,
          "ultima": daily[-1] if daily else None})

    # ── 4. search_ad_groups ────────────────────────────────────────────
    groups_all = ads_api.search_ad_groups(token, site, adv_id)
    dist = {}
    for g in groups_all:
        dist[g.get("ad_group_type")] = dist.get(g.get("ad_group_type"), 0) + 1
    show("4a) search_ad_groups(sem filtro)  [distribuição por tipo]",
         {"total": len(groups_all), "por_tipo": dist,
          "amostra": groups_all[:2]})

    groups_camp = ads_api.search_ad_groups(token, site, adv_id, campaign_id=cid)
    show(f"4b) search_ad_groups(campaign_id={cid})",
         {"total": len(groups_camp), "amostra": groups_camp[:2]})

    # ── 5. get_ad_group_ads ───────────────────────────────────────────
    #   pega um grupo CATALOG/FAMILY (tem itens) e um ITEM (vem vazio)
    g_multi = next((g for g in groups_all
                    if g.get("ad_group_type") in ("CATALOG", "FAMILY")), None)
    g_item = next((g for g in groups_all if g.get("ad_group_type") == "ITEM"), None)
    if g_multi:
        ads_in = ads_api.get_ad_group_ads(token, site, g_multi["id"])
        show(f"5a) get_ad_group_ads(ad_group={g_multi['id']}, type={g_multi['ad_group_type']})",
             {"n_itens": len(ads_in), "amostra": ads_in[:2]})
    if g_item:
        ads_item = ads_api.get_ad_group_ads(token, site, g_item["id"])
        show(f"5b) get_ad_group_ads(ad_group={g_item['id']}, type=ITEM)  "
             f"[esperado vazio; external_id={g_item.get('ad_group_external_id')}]",
             {"n_itens": len(ads_item), "resultado": ads_item})

    # ── 6. get_ad_group_metrics ──────────────────────────────────────
    ag_ids = [g["id"] for g in groups_camp[:5]] or [g["id"] for g in groups_all[:5]]
    m_period = ads_api.get_ad_group_metrics(token, site, cid, ag_ids, d30, dt)
    show(f"6a) get_ad_group_metrics(cid={cid}, {len(ag_ids)} ad_groups, {d30}..{dt})",
         {"n_linhas": len(m_period),
          "amostra": m_period[:3],
          "grupos_com_dado": sorted({r['ad_group_id'] for r in m_period})})

    m_daily = ads_api.get_ad_group_metrics(token, site, cid, ag_ids, d7, dt, daily=True)
    show(f"6b) get_ad_group_metrics(daily=True, {d7}..{dt})",
         {"n_linhas": len(m_daily), "amostra": m_daily[:3]})

    # ── erro esperado: rota legada -> AdsRouteRetiredError ───────────
    print(f"\n{'=' * 78}\n7) rota legada (deve levantar AdsRouteRetiredError)\n{'=' * 78}")
    try:
        ads_api._get(
            f"/advertising/advertisers/{adv_id}/product_ads/campaigns",
            token, params={"limit": 1},
        )
        print("   !! NÃO levantou erro (inesperado)")
    except ads_api.AdsRouteRetiredError as e:
        print(f"   OK -> AdsRouteRetiredError: {e}  (rid={e.request_id})")
    except ads_api.AdsApiError as e:
        print(f"   levantou AdsApiError (status={e.status}): {e}")

    print("\n== fim ==")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"railway ssh falhou: {e}", file=sys.stderr)
        sys.exit(1)
