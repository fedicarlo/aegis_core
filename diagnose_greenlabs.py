#!/usr/bin/env python3
"""
Diagnóstico estratégico completo da conta Mercado Livre GREENLABS
(seller_id 2124526664 / conta "Maximus" no Aegis).

Usa o token já autorizado no aegis.db (via app.services.meli_auth) e
explora endpoints da API do ML além do que já está implementado no
restante do sistema (reputação detalhada, Ads/Product Ads, perguntas
e reclamações abertas), além dos dados já cobertos (anúncios, visitas,
vendas, FULL, promoções).
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from app.config import MELI_API_URL
from app.database import get_account_by_seller_id
from app.services.meli_auth import get_valid_token
from app.services import meli_api as api

SELLER_ID = "2124526664"
NICKNAME = "GREENLABS"

PRIMARY_OUTPUT_PATH = "/diagnose_greenlabs_output.json"
FALLBACK_OUTPUT_PATH = os.path.join(PROJECT_ROOT, "diagnose_greenlabs_output.json")


# ── Helpers ────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def _headers(token: str, extra: dict = None) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if extra:
        h.update(extra)
    return h


def safe_get(url: str, token: str, params: dict = None, extra_headers: dict = None):
    """GET tolerante a falha. Retorna (data_or_None, status_code, error_str)."""
    try:
        res = requests.get(
            url, headers=_headers(token, extra_headers), params=params, timeout=20
        )
        if res.status_code >= 400:
            return None, res.status_code, res.text[:300]
        return res.json(), res.status_code, None
    except Exception as e:
        return None, None, str(e)


# ── 1. Token ─────────────────────────────────────────────────────────────

def load_token():
    account = get_account_by_seller_id(SELLER_ID)
    if not account:
        raise RuntimeError(f"Conta com seller_id={SELLER_ID} não encontrada em aegis.db")
    if not account.get("access_token"):
        raise RuntimeError(
            f"Conta '{account['name']}' não possui access_token salvo (não autorizada)."
        )
    token = get_valid_token(account)  # renova automaticamente se expirado
    return token, account


# ── 2. Seller / Reputação ───────────────────────────────────────────────

def collect_seller_info(token: str) -> dict:
    data, status, err = safe_get(f"{MELI_API_URL}/users/{SELLER_ID}", token)
    return {"data": data, "status": status, "error": err}


# ── 3. Anúncios ──────────────────────────────────────────────────────────

def collect_items(token: str) -> list:
    log("Buscando IDs de todos os anúncios (todos os status)...")
    all_ids = api.get_item_ids(token, SELLER_ID)
    log(f"  {len(all_ids)} anúncios encontrados no total.")

    log("Buscando detalhes completos (batch de 20)...")
    return api.get_items_batch(token, all_ids)


def _count_by_status(items: list) -> dict:
    counts = {}
    for i in items:
        s = i.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return counts


def enrich_items(token: str, items: list, recently_sold_ids: set = frozenset()) -> list:
    """
    Para cada anúncio: campos base + visitas 30/90d, FULL, reviews.
    Enriquece anúncios 'active' e também qualquer anúncio pausado/fechado que
    teve vendas nos últimos 90 dias (sinal estratégico: itens que venderam
    bem e saíram do ar, ex.: ruptura de estoque ou perda de buybox).
    """
    enriched = []
    relevant_count = sum(
        1 for i in items
        if i.get("status") == "active" or i.get("id") in recently_sold_ids
    )
    log(f"Enriquecendo {relevant_count} anúncios (ativos + pausados com vendas recentes) "
        f"com visitas / FULL / reviews...")

    for idx, item in enumerate(items):
        item_id = item.get("id")
        variations = item.get("variations") or []

        entry = {
            "id": item_id,
            "title": item.get("title"),
            "status": item.get("status"),
            "price": item.get("price"),
            "currency_id": item.get("currency_id"),
            "available_quantity": item.get("available_quantity"),
            "sold_quantity": item.get("sold_quantity"),
            "listing_type_id": item.get("listing_type_id"),
            "catalog_listing": bool(item.get("catalog_listing")),
            "permalink": item.get("permalink"),
            "shipping_logistic_type": (item.get("shipping") or {}).get("logistic_type"),
            "free_shipping": (item.get("shipping") or {}).get("free_shipping"),
            "health": item.get("health"),
            "tags": item.get("tags"),
            "variations_count": len(variations),
            "variations": [
                {
                    "id": v.get("id"),
                    "price": v.get("price"),
                    "available_quantity": v.get("available_quantity"),
                    "sold_quantity": v.get("sold_quantity"),
                }
                for v in variations
            ],
        }

        if item.get("status") == "active" or item_id in recently_sold_ids:
            visits_30, _, _ = safe_get(
                f"{MELI_API_URL}/items/{item_id}/visits/time_window",
                token, params={"last": 30, "unit": "day"},
            )
            visits_90, _, _ = safe_get(
                f"{MELI_API_URL}/items/{item_id}/visits/time_window",
                token, params={"last": 90, "unit": "day"},
            )
            entry["visits_30d"] = (visits_30 or {}).get("total_visits", 0)
            entry["visits_90d"] = (visits_90 or {}).get("total_visits", 0)

            full_stock = []
            for inv_id in api.get_inventory_ids_from_item(item):
                stock = api.get_inventory_stock(token, inv_id)
                if stock:
                    full_stock.append({"inventory_id": inv_id, **stock})
            entry["full_stock"] = full_stock

            reviews, _, _ = safe_get(f"{MELI_API_URL}/reviews/item/{item_id}", token)
            if reviews:
                entry["reviews_rating_average"] = reviews.get("rating_average")
                entry["reviews_total"] = reviews.get("total")

        enriched.append(entry)
        if (idx + 1) % 25 == 0:
            log(f"  ...{idx + 1}/{len(items)} processados")

    return enriched


# ── 4. Vendas / Receita (30 e 90 dias) ──────────────────────────────────

def collect_orders_and_revenue(token: str, days: int) -> dict:
    orders = api.get_orders(token, SELLER_ID, days=days)

    revenue_gross = 0.0
    orders_count = 0
    qty_by_item = {}
    orders_trimmed = []

    for o in orders:
        status = o.get("status")
        if status == "cancelled":
            continue
        orders_count += 1
        total_amount = float(o.get("total_amount") or 0)
        revenue_gross += total_amount

        item_lines = []
        for oi in o.get("order_items", []):
            item = oi.get("item", {}) or {}
            iid = item.get("id")
            qty = oi.get("quantity", 0) or 0
            unit_price = oi.get("unit_price")
            qty_by_item[iid] = qty_by_item.get(iid, 0) + qty
            item_lines.append({
                "item_id": iid,
                "title": item.get("title"),
                "quantity": qty,
                "unit_price": unit_price,
            })

        orders_trimmed.append({
            "id": o.get("id"),
            "status": status,
            "date_created": o.get("date_created"),
            "total_amount": total_amount,
            "items": item_lines,
        })

    return {
        "period_days": days,
        "orders_count": orders_count,
        "revenue_gross_total": round(revenue_gross, 2),
        "qty_by_item": qty_by_item,
        "orders": orders_trimmed,
    }


# ── 5. FULL (resumo agregado) ───────────────────────────────────────────

def summarize_full(enriched_items: list) -> dict:
    total_full_units = 0
    items_with_full = 0
    for e in enriched_items:
        fs = e.get("full_stock") or []
        if fs:
            items_with_full += 1
            for loc in fs:
                total_full_units += loc.get("total", 0) or 0
    return {
        "items_with_full_stock": items_with_full,
        "total_full_units": total_full_units,
    }


# ── 6. Ads / Product Ads (Mercado Ads) — endpoint não implementado no sistema
#
# NOTA (2026-08): a rota antiga /advertising/advertisers/{id}/product_ads/campaigns
# foi descontinuada em fev/2026. Fluxo novo (api-version: 2), confirmado ao vivo
# contra a conta Maximus:
#   1. GET /advertising/advertisers?product_id=PADS            -> advertiser_id + site_id
#   2. GET /advertising/{site}/advertisers/{adv}/product_ads/campaigns/search
#          ?date_from&date_to&metrics=...&metrics_summary=true  -> campanhas + métricas
#   3. GET /advertising/{site}/product_ads/campaigns/{id}
#          ?metrics=...(+impression_share...)&aggregation_type=DAILY  -> histórico diário
#   4. GET /advertising/{site}/advertisers/{adv}/product_ads/ad_groups/search
#          [?filters[campaign_id]= | ?filters[item_ids]=]       -> ad groups (produto/família/item)
#   5. GET /advertising/{site}/product_ads/campaigns/{id}/ad_groups/metrics
#          ?filters[ad_group_ids]=...&date_from&date_to&metrics=...  -> métricas por ad_group
# Granularidade mínima de métrica = ad_group_id (NÃO por item_id). Só ad_group_type=ITEM
# é 1:1 com um anúncio; CATALOG/FAMILY agrupam vários item_ids.

_ADS_METRICS = (
    "clicks,prints,ctr,cost,cpc,acos,cvr,roas,sov,"
    "direct_units_quantity,indirect_units_quantity,units_quantity,"
    "direct_amount,indirect_amount,total_amount"
)


def collect_ads(token: str) -> dict:
    from datetime import date, timedelta

    result = {"advertisers": [], "campaigns": [], "ad_groups": [], "errors": []}

    adv_data, status, err = safe_get(
        f"{MELI_API_URL}/advertising/advertisers",
        token,
        params={"product_id": "PADS"},
        extra_headers={"Api-Version": "1"},
    )
    if status != 200 or adv_data is None:
        result["errors"].append({"step": "advertisers", "status": status, "error": err})
        return result

    advertisers = adv_data.get("advertisers", []) if isinstance(adv_data, dict) else []
    result["advertisers"] = advertisers or []

    date_to = date.today().isoformat()
    date_from = (date.today() - timedelta(days=30)).isoformat()

    for adv in result["advertisers"]:
        adv_id = adv.get("advertiser_id")
        site = adv.get("site_id", "MLB")

        camp_data, cstatus, cerr = safe_get(
            f"{MELI_API_URL}/advertising/{site}/advertisers/{adv_id}"
            f"/product_ads/campaigns/search",
            token,
            params={
                "limit": 50, "offset": 0,
                "date_from": date_from, "date_to": date_to,
                "metrics": _ADS_METRICS, "metrics_summary": "true",
            },
            extra_headers={"api-version": "2"},
        )
        if cstatus != 200 or camp_data is None:
            result["errors"].append({
                "step": "campaigns/search", "advertiser_id": adv_id,
                "status": cstatus, "error": cerr,
            })
            continue
        campaigns = camp_data.get("results", []) if isinstance(camp_data, dict) else []
        result["campaigns"].extend(campaigns or [])

        ag_data, agstatus, agerr = safe_get(
            f"{MELI_API_URL}/advertising/{site}/advertisers/{adv_id}"
            f"/product_ads/ad_groups/search",
            token,
            params={"limit": 100, "offset": 0},
            extra_headers={"api-version": "2"},
        )
        if agstatus == 200 and isinstance(ag_data, dict):
            result["ad_groups"].extend(ag_data.get("results", []) or [])
        else:
            result["errors"].append({
                "step": "ad_groups/search", "advertiser_id": adv_id,
                "status": agstatus, "error": agerr,
            })

    return result


# ── 7. Perguntas — endpoint não implementado no sistema ─────────────────

def collect_questions(token: str) -> dict:
    out = {}
    for status in ("UNANSWERED", "ANSWERED"):
        items_out = []
        offset = 0
        limit = 50
        while True:
            data, s, err = safe_get(
                f"{MELI_API_URL}/questions/search",
                token,
                params={"seller_id": SELLER_ID, "status": status, "limit": limit, "offset": offset},
            )
            if s != 200 or not data:
                break
            results = data.get("questions", [])
            items_out.extend(results)
            total = data.get("total", 0)
            offset += limit
            if offset >= total or not results:
                break
        out[status.lower()] = items_out
    out["unanswered_count"] = len(out.get("unanswered", []))
    return out


# ── 8. Reclamações (claims) — endpoint não implementado no sistema ──────

def collect_claims(token: str) -> dict:
    attempts = [
        ("post_purchase_v1_opened", f"{MELI_API_URL}/post-purchase/v1/claims/search",
         {"seller_id": SELLER_ID, "status": "opened"}),
        ("post_purchase_v1_all", f"{MELI_API_URL}/post-purchase/v1/claims/search",
         {"seller_id": SELLER_ID}),
    ]
    tried = []
    for label, url, params in attempts:
        data, status, err = safe_get(url, token, params=params)
        tried.append({"source": label, "status": status, "error": err})
        if status == 200 and data:
            return {"source": label, "data": data, "attempts": tried}
    return {"source": None, "data": None, "attempts": tried,
             "note": "Nenhum endpoint de claims retornou 200 com este token."}


# ── 9. Promoções (já implementado no sistema — reutilizado) ────────────

def collect_promotions(token: str) -> list:
    return api.get_item_promotions(token, SELLER_ID)


# ── Saída ────────────────────────────────────────────────────────────────

def save_output(output: dict) -> str:
    try:
        with open(PRIMARY_OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        return PRIMARY_OUTPUT_PATH
    except (PermissionError, OSError) as e:
        log(f"Sem permissão para gravar em {PRIMARY_OUTPUT_PATH} ({e}); "
            f"salvando na raiz do projeto.")
        with open(FALLBACK_OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        return FALLBACK_OUTPUT_PATH


def print_summary(output: dict, output_path: str) -> None:
    print("\n" + "=" * 72)
    print(f"RESUMO — {output['nickname']} (seller_id {output['seller_id']})")
    print("=" * 72)

    seller_data = (output.get("seller") or {}).get("data") or {}
    rep = seller_data.get("seller_reputation", {}) or {}
    metrics = rep.get("metrics", {}) or {}
    print(f"Nível de reputação:        {rep.get('level_id')}")
    print(f"Power seller status:       {rep.get('power_seller_status')}")
    print(f"Taxa de reclamações:       {(metrics.get('claims') or {}).get('rate')}")
    print(f"Taxa de cancelamentos:     {(metrics.get('cancellations') or {}).get('rate')}")
    print(f"Atraso no envio (rate):    {(metrics.get('delayed_handling_time') or {}).get('rate')}")

    print(f"\nAnúncios totais: {output['items_total_count']}  {output['items_by_status']}")

    s30 = output["sales_30d"]
    s90 = output["sales_90d"]
    print(f"\nReceita bruta 30d: R$ {s30['revenue_gross_total']:.2f}  ({s30['orders_count']} pedidos)")
    print(f"Receita bruta 90d: R$ {s90['revenue_gross_total']:.2f}  ({s90['orders_count']} pedidos)")

    top_items = sorted(
        output["items"], key=lambda i: i.get("units_sold_30d", 0) or 0, reverse=True
    )[:5]
    print("\nTop 5 anúncios por vendas (30d):")
    for it in top_items:
        title = (it.get("title") or "")[:50]
        print(
            f"  - {it['id']} | {title:<50} | vendas30d={it.get('units_sold_30d')} | "
            f"visitas30d={it.get('visits_30d')} | conv={it.get('conversion_30d_pct')}%"
        )

    full = output["full_summary"]
    print(
        f"\nFULL: {full['items_with_full_stock']} anúncios com estoque, "
        f"{full['total_full_units']} unidades totais"
    )

    promos = output.get("promotions") or []
    print(f"\nPromoções ativas: {len(promos)}")

    ads = output.get("ads") or {}
    print(
        f"Ads — anunciantes: {len(ads.get('advertisers') or [])}, "
        f"campanhas: {len(ads.get('campaigns') or [])}, "
        f"ad groups: {len(ads.get('ad_groups') or [])}"
    )
    if ads.get("errors"):
        print(f"  (avisos ads: {ads['errors']})")

    qs = output.get("questions") or {}
    print(f"\nPerguntas em aberto: {qs.get('unanswered_count', 0)}")

    claims = output.get("claims") or {}
    print(f"Reclamações (claims): fonte={claims.get('source')}")
    if not claims.get("source"):
        print(f"  {claims.get('note')}")

    print(f"\nJSON completo salvo em: {output_path}")
    print("=" * 72)


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    log(f"=== Diagnóstico Aegis — {NICKNAME} (seller_id {SELLER_ID}) ===")
    token, account = load_token()
    log(f"Token OK para conta '{account['name']}'.")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seller_id": SELLER_ID,
        "nickname": NICKNAME,
    }

    log("Coletando dados do seller / reputação...")
    output["seller"] = collect_seller_info(token)

    items = collect_items(token)
    output["items_total_count"] = len(items)
    output["items_by_status"] = _count_by_status(items)

    log("Coletando pedidos e receita (30 dias)...")
    sales_30 = collect_orders_and_revenue(token, 30)
    log("Coletando pedidos e receita (90 dias)...")
    sales_90 = collect_orders_and_revenue(token, 90)

    recently_sold_ids = set(sales_90["qty_by_item"].keys())
    enriched_items = enrich_items(token, items, recently_sold_ids)

    for entry in enriched_items:
        iid = entry["id"]
        qty30 = sales_30["qty_by_item"].get(iid, 0)
        qty90 = sales_90["qty_by_item"].get(iid, 0)
        entry["units_sold_30d"] = qty30
        entry["units_sold_90d"] = qty90
        visits30 = entry.get("visits_30d") or 0
        entry["conversion_30d_pct"] = (
            round(qty30 / visits30 * 100, 2) if visits30 else None
        )

    output["items"] = enriched_items
    output["sales_30d"] = sales_30
    output["sales_90d"] = sales_90
    output["full_summary"] = summarize_full(enriched_items)

    log("Coletando promoções ativas...")
    output["promotions"] = collect_promotions(token)

    log("Explorando Ads (Product Ads / Mercado Ads)...")
    output["ads"] = collect_ads(token)

    log("Coletando perguntas em aberto...")
    output["questions"] = collect_questions(token)

    log("Explorando reclamações (claims) abertas...")
    output["claims"] = collect_claims(token)

    output_path = save_output(output)
    log(f"Dados salvos em: {output_path}")
    print_summary(output, output_path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\nERRO ao executar diagnóstico:")
        traceback.print_exc()
        sys.exit(1)
