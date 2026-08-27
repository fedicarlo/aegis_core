"""
Ads Data Provider (Etapa 3) — coleta o fluxo de Product Ads e persiste SOURCE_API
campo por campo, mais o snapshot diário de meta/orçamento (SOURCE_CALCULATED).

Não faz nenhuma escrita na API do ML (V1 é read-only).

Regras incorporadas:
  - Dia corrente NÃO é persistido. A API do ML só consolida as métricas do dia
    por volta das 10h GMT-3; a linha de "hoje" vem parcial (impression_share=0,
    acos_benchmark=0, etc.) e distorceria a série. Persistimos só datas
    estritamente anteriores a hoje (fuso GMT-3). Re-coletar um dia já fechado
    corrige o valor via UPSERT (self-healing).
  - Unidade de métrica = ad_group_id. Grupos ad_group_type=ITEM não têm entrada
    em /ad_groups/{id}/ads (a lista vem vazia) — a ponte ad_group_items recebe
    uma linha sintética com item_id = ad_group_external_id.
  - Grupos com campaign_id 0/nulo não têm métrica coletável (o endpoint de
    métrica é por campanha) — são contados em `ad_groups_sem_campanha`.
"""
from datetime import datetime, timedelta, timezone

from app.config import ADS_SYNC_WINDOW_DAYS
from app.database import (
    get_all_accounts,
    replace_ad_group_items,
    snapshot_campaign_targets,
    upsert_ad_group_metrics_daily,
    upsert_ads_ad_group,
    upsert_ads_advertiser,
    upsert_ads_campaign,
    upsert_campaign_metrics_daily,
    upsert_campaign_metrics_detail,
)
from app.services import ads_api
from app.services.meli_auth import get_valid_token
from app.utils.logger import get_logger

log = get_logger("ads_collector")

# Fuso das datas de métrica do ML para sites *LB (MLB = Brasil, GMT-3).
_ML_METRICS_TZ_OFFSET_H = -3


def _today_ml() -> str:
    """Data 'hoje' no fuso das métricas do ML (GMT-3), como 'YYYY-MM-DD'."""
    now = datetime.now(timezone.utc) + timedelta(hours=_ML_METRICS_TZ_OFFSET_H)
    return now.date().isoformat()


def _is_closed_day(date_str: str, today_ml: str) -> bool:
    """True se a data já fechou (estritamente anterior a hoje no fuso do ML)."""
    return bool(date_str) and date_str < today_ml


def _detail_settled(row: dict) -> bool:
    """
    True se os campos de impression share da linha diária já consolidaram.

    Observado ao vivo: as métricas-base do dia D ficam prontas em D+1, mas
    impression_share / acos_benchmark demoram mais um dia. Enquanto não
    consolidam, a linha vem com prints > 0 mas impression_share == 0 E
    acos_benchmark == 0 — assinatura de "ainda não fechou". Nesse caso NÃO
    gravamos a linha de campaign_metrics_detail; o próximo run corrige via
    UPSERT (self-healing). Campanha genuinamente sem tráfego no dia tem
    prints == 0 e cai fora deste guard (não há o que gravar mesmo).
    """
    prints = float(row.get("prints") or 0)
    ish = float(row.get("impression_share") or 0)
    bench = float(row.get("acos_benchmark") or 0)
    return not (prints > 0 and ish == 0.0 and bench == 0.0)


def collect_ads_account(account: dict, *, window_days: int = None,
                        token: str = None, refresh_items: bool = True) -> dict:
    """Coleta Ads de uma conta. Retorna resumo com contadores e erros."""
    window_days = window_days or ADS_SYNC_WINDOW_DAYS
    seller_id = account["seller_id"]
    name = account.get("name", seller_id)
    token = token or get_valid_token(account)

    today_ml = _today_ml()
    date_to = today_ml
    date_from = (datetime.fromisoformat(today_ml).date()
                 - timedelta(days=window_days)).isoformat()

    r = {
        "account": name, "seller_id": seller_id,
        "window": f"{date_from}..{date_to}",
        "advertisers": 0, "campaigns": 0, "ad_groups": 0, "ad_groups_orfaos": 0,
        "ad_group_items": 0,
        "campaign_metric_days": 0, "campaign_detail_days": 0,
        "campaign_detail_skipped_unsettled": 0,
        "ad_group_metric_days": 0,
        "snapshots_written": 0, "events_written": 0,
        "skipped_current_day": 0,
        "errors": [],
    }

    try:
        advertisers = ads_api.get_advertisers(token)
    except ads_api.AdsApiError as e:
        r["errors"].append(f"advertisers: {e}")
        log.error(f"[{name}] advertisers falhou: {e}")
        return r

    for adv in advertisers:
        adv_id = adv.get("advertiser_id")
        site = adv.get("site_id") or "MLB"
        upsert_ads_advertiser(seller_id, adv)
        r["advertisers"] += 1

        # ── Campanhas + snapshot de meta + série diária ──────────────────
        try:
            camp_res = ads_api.search_campaigns(token, site, adv_id, date_from, date_to)
        except ads_api.AdsApiError as e:
            r["errors"].append(f"campaigns/search adv={adv_id}: {e}")
            log.error(f"[{name}] campaigns/search adv={adv_id} falhou: {e}")
            continue

        campaigns = camp_res["results"]
        for camp in campaigns:
            cid = camp.get("id")

            # detail não-diário só p/ pegar currency_id (não vem no /search)
            try:
                cdetail = ads_api.get_campaign_detail(
                    token, site, cid, date_from, date_to, daily=False)
            except ads_api.AdsApiError as e:
                cdetail = {}
                r["errors"].append(f"campaign_detail(currency) cid={cid}: {e}")

            camp_row = dict(camp)
            camp_row["currency_id"] = (cdetail or {}).get("currency_id")
            upsert_ads_campaign(seller_id, adv_id, camp_row)
            r["campaigns"] += 1

            snap = snapshot_campaign_targets(seller_id, cid, camp)
            if snap["snapshot_written"]:
                r["snapshots_written"] += 1
            r["events_written"] += len(snap["events"])

            # ── Série diária + impression share ─────────────────────────
            try:
                daily = ads_api.get_campaign_detail(
                    token, site, cid, date_from, date_to, daily=True)
            except ads_api.AdsApiError as e:
                r["errors"].append(f"campaign_detail(daily) cid={cid}: {e}")
                log.error(f"[{name}] campaign_detail(daily) cid={cid} falhou: {e}")
                continue

            for row in daily:
                d = row.get("date")
                if not _is_closed_day(d, today_ml):
                    r["skipped_current_day"] += 1
                    continue
                upsert_campaign_metrics_daily(seller_id, cid, d, row)
                r["campaign_metric_days"] += 1
                if _detail_settled(row):
                    upsert_campaign_metrics_detail(seller_id, cid, d, row)
                    r["campaign_detail_days"] += 1
                else:
                    r["campaign_detail_skipped_unsettled"] += 1

        # ── Ad groups: associação confiável = search por campanha ───────
        # O search sem filtro devolve campaign_id 0 pra muitos grupos; o filtro
        # filters[campaign_id] dá a associação real. Grupos que não aparecem em
        # nenhuma campanha (órfãos: pausados / sem veiculação) são guardados mas
        # não têm métrica coletável (o endpoint de métrica é por campanha).
        groups_by_cid = {}
        seen_ids = set()
        for camp in campaigns:
            cid = camp.get("id")
            try:
                cg = ads_api.search_ad_groups(token, site, adv_id, campaign_id=cid)
            except ads_api.AdsApiError as e:
                r["errors"].append(f"ad_groups/search cid={cid}: {e}")
                log.error(f"[{name}] ad_groups/search cid={cid} falhou: {e}")
                continue
            for g in cg:
                g["campaign_id"] = cid  # associação confiável
                seen_ids.add(g["id"])
            groups_by_cid[cid] = cg

        try:
            unfiltered = ads_api.search_ad_groups(token, site, adv_id)
        except ads_api.AdsApiError as e:
            unfiltered = []
            r["errors"].append(f"ad_groups/search adv={adv_id}: {e}")
        orphans = [g for g in unfiltered if g["id"] not in seen_ids]
        r["ad_groups_orfaos"] += len(orphans)

        # ── Métricas por ad_group (batch por campanha) ─────────────────
        tags_by_ag = {}
        for cid, glist in groups_by_cid.items():
            if not glist:
                continue
            ag_ids = [g["id"] for g in glist]
            try:
                rows = ads_api.get_ad_group_metrics(
                    token, site, cid, ag_ids, date_from, date_to, daily=True)
            except ads_api.AdsApiError as e:
                r["errors"].append(f"ad_group_metrics cid={cid}: {e}")
                log.error(f"[{name}] ad_group_metrics cid={cid} falhou: {e}")
                continue
            for row in rows:
                if row.get("tags"):
                    tags_by_ag[row["ad_group_id"]] = row["tags"]
                d = row.get("date")
                if not _is_closed_day(d, today_ml):
                    r["skipped_current_day"] += 1
                    continue
                upsert_ad_group_metrics_daily(
                    seller_id, cid, row["ad_group_id"], d, row["metrics"])
                r["ad_group_metric_days"] += 1

        # ── Upsert dos ad groups (campanha + órfãos), tags mescladas ────
        all_groups = [g for glist in groups_by_cid.values() for g in glist] + orphans
        for g in all_groups:
            tags = tags_by_ag.get(g["id"]) or g.get("tags") or []
            upsert_ads_ad_group(seller_id, g, tags=tags)
            r["ad_groups"] += 1

        # ── Ponte ad_group → item_id ────────────────────────────────────
        if refresh_items:
            for g in all_groups:
                agid = g["id"]
                gtype = g.get("ad_group_type")
                ext = g.get("ad_group_external_id")
                try:
                    if gtype == "ITEM":
                        rows = [{"item_id": str(ext)}] if ext else []
                    else:
                        rows = ads_api.get_ad_group_ads(token, site, agid)
                except ads_api.AdsApiError as e:
                    r["errors"].append(f"ad_group_ads ag={agid}: {e}")
                    log.error(f"[{name}] ad_group_ads ag={agid} falhou: {e}")
                    continue
                replace_ad_group_items(seller_id, agid, rows)
                r["ad_group_items"] += len(rows)

    log.info(
        f"[{name}] ads sync ok — {r['advertisers']} advertiser(s), "
        f"{r['campaigns']} campanha(s), {r['ad_groups']} ad group(s), "
        f"{r['campaign_metric_days']} dia(s) de campanha, "
        f"{r['ad_group_metric_days']} dia(s) de ad group, "
        f"{r['skipped_current_day']} linha(s) do dia corrente puladas, "
        f"{r['snapshots_written']} snapshot(s), {len(r['errors'])} erro(s)"
    )
    return r


def collect_all_ads(*, window_days: int = None) -> list:
    """Roda a coleta de Ads em todas as contas autorizadas."""
    accounts = [a for a in get_all_accounts() if a.get("access_token")]
    if not accounts:
        log.info("nenhuma conta autorizada para coleta de Ads")
        return []

    results = []
    for account in accounts:
        try:
            results.append(collect_ads_account(account, window_days=window_days))
        except Exception as e:  # noqa: BLE001 — uma conta não pode derrubar as outras
            log.error(f"[{account.get('name')}] coleta de Ads falhou: {e}")
            results.append({"account": account.get("name"),
                            "seller_id": account.get("seller_id"),
                            "errors": [str(e)]})
    return results
