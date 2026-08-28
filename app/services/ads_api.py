"""
Cliente da API de Mercado Ads / Product Ads (fluxo pós-migração para Ad Groups).

Camada fina de I/O: só faz GET, retorna dicts crus, não toca em banco.
A orquestração + persistência fica no Ads Data Provider (Etapa 3).

Fluxo (auditado em 27/08/2026 contra a conta Maximus, ver AEGIS_ADS_INTELLIGENCE_V1.md):

    get_advertisers()                 GET /advertising/advertisers?product_id=PADS      (Api-Version: 1)
    search_campaigns()                GET /advertising/{site}/advertisers/{adv}/product_ads/campaigns/search
    get_campaign_detail()             GET /advertising/{site}/product_ads/campaigns/{cid}
    search_ad_groups()                GET /advertising/{site}/advertisers/{adv}/product_ads/ad_groups/search
    get_ad_group_ads()                GET /advertising/{site}/product_ads/ad_groups/{ag}/ads
    get_ad_group_metrics()            GET /advertising/{site}/product_ads/campaigns/{cid}/ad_groups/metrics

Notas de contrato confirmadas na auditoria:
  - Header correto para tudo exceto get_advertisers: `api-version: 2`.
  - `campaigns/search` e `ad_groups/search` paginam com offset/limit + objeto `paging`.
  - `get_campaign_detail(daily=True)` responde `{"results": [ {..., "date": "..."} ]}`.
  - `get_ad_group_metrics` SEMPRE responde uma lista `[ {"date": ..., "results": [ {"ad_group_id", "metrics", "tags"} ]} ]`
    e EXIGE `filters[ad_group_ids]` quando o range é > 1 dia — por isso a função é
    obrigada a receber ad_group_ids e faz batching interno.
  - Impression share (impression_share / top_impression_share /
    lost_impression_share_by_budget / _by_ad_rank / acos_benchmark) só existe no
    detalhe de campanha — não há equivalente por ad_group.
  - A rota legada `/advertising/advertisers/{id}/product_ads/campaigns` (sem
    {site} e sem /search) foi descontinuada em fev/2026 e responde **404 com corpo
    vazio**. Isso é tratado aqui como AdsRouteRetiredError, NUNCA como "sem dados".
"""
import random
import time

import requests

from app.config import MELI_API_URL
from app.utils.logger import get_logger

log = get_logger("ads_api")

_TIMEOUT = 30
_MAX_RETRIES = 5          # tentativas em 429 / erro de rede transitório
_BACKOFF_BASE = 1.0       # segundos
_BACKOFF_CAP = 30.0
_AD_GROUP_IDS_BATCH = 20  # lote de filters[ad_group_ids] por chamada

AGGREGATION_DAILY = "DAILY"

# Conjuntos de métricas padrão (nomes exatos aceitos pela API, confirmados ao vivo).
CAMPAIGN_METRICS = (
    "clicks,prints,ctr,cost,cpc,acos,cvr,roas,sov,"
    "direct_units_quantity,indirect_units_quantity,units_quantity,"
    "direct_items_quantity,indirect_items_quantity,"
    "organic_units_quantity,organic_items_quantity,"
    "direct_amount,indirect_amount,total_amount"
)
CAMPAIGN_DETAIL_METRICS = CAMPAIGN_METRICS + (
    ",impression_share,top_impression_share,"
    "lost_impression_share_by_budget,lost_impression_share_by_ad_rank,acos_benchmark"
)
AD_GROUP_METRICS = (
    "clicks,prints,ctr,cost,cpc,acos,cvr,roas,sov,"
    "direct_units_quantity,indirect_units_quantity,units_quantity,"
    "direct_items_quantity,indirect_items_quantity,"
    "organic_units_quantity,organic_items_quantity,"
    "direct_amount,indirect_amount,total_amount"
)


# ── Erros ────────────────────────────────────────────────────────────────────

class AdsApiError(Exception):
    """Falha ao falar com a API de Ads. Carrega status, corpo e x-request-id."""

    def __init__(self, message, *, status=None, url=None, body=None, request_id=None):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body
        self.request_id = request_id


class AdsRouteRetiredError(AdsApiError):
    """
    404 com corpo VAZIO numa rota que deveria existir — assinatura de rota
    aposentada/bloqueada no gateway do ML (não é 'sem dados'). Ver nota do módulo.
    """


# ── Helper de request ────────────────────────────────────────────────────────

def _get(path, token, params=None, *, version="2"):
    """
    GET autenticado contra a API de Ads.

    - Retorna o JSON já parseado (dict ou list).
    - 429 / erro de rede transitório: backoff exponencial com jitter, até _MAX_RETRIES.
    - 404 com corpo vazio -> AdsRouteRetiredError.
    - Qualquer outro >= 400 -> AdsApiError.
    - 200 com results vazio / lista vazia -> retorna normalmente (NÃO é erro).
    """
    url = f"{MELI_API_URL}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "api-version": str(version),
        "Content-Type": "application/json",
    }

    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
            wait = _backoff(attempt)
            log.warning(f"[{path}] erro de rede ({exc}); retry em {wait:.1f}s "
                        f"(tentativa {attempt + 1}/{_MAX_RETRIES})")
            time.sleep(wait)
            continue

        rid = res.headers.get("x-request-id")

        if res.status_code == 429:
            wait = _retry_after(res) or _backoff(attempt)
            log.warning(f"[{path}] 429 rate limit; retry em {wait:.1f}s "
                        f"(tentativa {attempt + 1}/{_MAX_RETRIES}) rid={rid}")
            time.sleep(wait)
            continue

        if res.status_code == 404 and not res.text.strip():
            raise AdsRouteRetiredError(
                f"404 corpo vazio em {path} — rota provavelmente descontinuada "
                f"no gateway do ML (não é 'sem dados').",
                status=404, url=url, body="", request_id=rid,
            )

        if res.status_code >= 400:
            raise AdsApiError(
                f"{res.status_code} em {path}: {res.text[:300]}",
                status=res.status_code, url=url, body=res.text[:1000], request_id=rid,
            )

        try:
            return res.json()
        except ValueError as exc:
            raise AdsApiError(
                f"resposta 200 não-JSON em {path}: {res.text[:200]}",
                status=res.status_code, url=url, body=res.text[:1000], request_id=rid,
            ) from exc

    raise AdsApiError(
        f"falha após {_MAX_RETRIES} tentativas em {path}"
        + (f" (último erro: {last_exc})" if last_exc else ""),
        url=url,
    )


def _backoff(attempt):
    return min(_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, _BACKOFF_BASE), _BACKOFF_CAP)


def _retry_after(res):
    raw = res.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), _BACKOFF_CAP)
    except (TypeError, ValueError):
        return None


def _paginate(path, token, params, *, version="2", page_size=50, hard_cap=5000):
    """
    Itera offset/limit num endpoint que devolve {"paging": {...}, "results": [...]}.
    Retorna (todos_os_results, primeira_pagina_completa).
    """
    params = dict(params or {})
    params["limit"] = page_size
    offset = 0
    out = []
    first_page = None

    while offset < hard_cap:
        params["offset"] = offset
        page = _get(path, token, params, version=version)
        if first_page is None:
            first_page = page

        results = page.get("results") if isinstance(page, dict) else page
        results = results or []
        out.extend(results)

        total = 0
        if isinstance(page, dict):
            total = (page.get("paging") or {}).get("total", 0) or 0
        offset += page_size
        if offset >= total or not results:
            break

    return out, (first_page or {})


# ── 1. Advertisers ──────────────────────────────────────────────────────────

def get_advertisers(token, product_id="PADS"):
    """
    GET /advertising/advertisers?product_id=PADS   (Api-Version: 1)

    Retorna lista de advertisers: [{advertiser_id, site_id, advertiser_name, account_name}].
    Lista vazia = a conta não tem o produto habilitado (não é erro).
    """
    body = _get("/advertising/advertisers", token,
                params={"product_id": product_id}, version="1")
    return (body or {}).get("advertisers", []) or []


# ── 2. Campanhas (lista + métricas do período) ──────────────────────────────

def search_campaigns(token, site_id, advertiser_id, date_from, date_to,
                     *, metrics=CAMPAIGN_METRICS, metrics_summary=True):
    """
    GET /advertising/{site}/advertisers/{adv}/product_ads/campaigns/search

    Pagina todas as campanhas. Retorna:
        {"results": [ {campo de campanha..., "metrics": {...}} ],
         "metrics_summary": {...} | None,
         "total": N}
    metrics_summary é o agregado de TODAS as campanhas (vem já pronto na resposta).
    """
    path = (f"/advertising/{site_id}/advertisers/{advertiser_id}"
            f"/product_ads/campaigns/search")
    params = {"date_from": date_from, "date_to": date_to, "metrics": metrics}
    if metrics_summary:
        params["metrics_summary"] = "true"

    results, first_page = _paginate(path, token, params)
    return {
        "results": results,
        "metrics_summary": first_page.get("metrics_summary"),
        "total": (first_page.get("paging") or {}).get("total", len(results)),
    }


# ── 3. Detalhe de campanha (+ impression share, + série diária) ─────────────

def get_campaign_detail(token, site_id, campaign_id, date_from, date_to,
                        *, metrics=CAMPAIGN_DETAIL_METRICS, daily=False):
    """
    GET /advertising/{site}/product_ads/campaigns/{cid}

    daily=False -> dict da campanha com `metrics` do período (inclui impression share).
    daily=True  -> lista de linhas diárias [{..., "date": "YYYY-MM-DD"}], ordenada por data.
    """
    path = f"/advertising/{site_id}/product_ads/campaigns/{campaign_id}"
    params = {"date_from": date_from, "date_to": date_to, "metrics": metrics}
    if daily:
        params["aggregation_type"] = AGGREGATION_DAILY

    body = _get(path, token, params)

    if daily:
        rows = body.get("results", []) if isinstance(body, dict) else (body or [])
        return sorted(rows, key=lambda r: r.get("date") or "")
    return body


# ── 4. Ad groups (busca por campanha ou por item) ──────────────────────────

def search_ad_groups(token, site_id, advertiser_id, *, campaign_id=None,
                     item_ids=None):
    """
    GET /advertising/{site}/advertisers/{adv}/product_ads/ad_groups/search

    campaign_id : filtra por campanha (filters[campaign_id]).
    item_ids    : str "MLB1,MLB2" ou lista — filtra por item (filters[item_ids], PLURAL;
                  a chave singular filters[item_id] é ignorada pela API).
    Sem filtro: pagina TODOS os ad groups do advertiser.
    Retorna lista de ad groups (cada um com id, ad_group_type, ad_group_external_id,
    campaign_id, title, status, domain_id, catalog_listing, ...).
    """
    path = (f"/advertising/{site_id}/advertisers/{advertiser_id}"
            f"/product_ads/ad_groups/search")
    params = {}
    if campaign_id is not None:
        params["filters[campaign_id]"] = campaign_id
    if item_ids:
        params["filters[item_ids]"] = _csv(item_ids)

    results, _ = _paginate(path, token, params)
    return results


# ── 5. Itens (item_id) que compõem um ad group ────────────────────────────

def get_ad_group_ads(token, site_id, ad_group_id):
    """
    GET /advertising/{site}/product_ads/ad_groups/{ag}/ads

    Retorna os anúncios (item_id + atributos) dentro do ad group. SEM métrica
    (a API devolve `metrics: {}` aqui). Para grupos ad_group_type=ITEM a lista
    vem VAZIA — o próprio ad_group_external_id já é o item_id (isso é válido,
    retorna []).
    """
    path = f"/advertising/{site_id}/product_ads/ad_groups/{ad_group_id}/ads"
    results, _ = _paginate(path, token, {})
    return results


# ── 6. Métricas por ad group (grão mínimo de métrica que existe) ──────────

def get_ad_group_metrics(token, site_id, campaign_id, ad_group_ids,
                         date_from, date_to, *, metrics=AD_GROUP_METRICS,
                         daily=False):
    """
    GET /advertising/{site}/product_ads/campaigns/{cid}/ad_groups/metrics

    ad_group_ids : lista (ou csv) de ad_group_id do ML. OBRIGATÓRIO — a API exige
                   filters[ad_group_ids] sempre que o range é > 1 dia. Batched em
                   lotes de _AD_GROUP_IDS_BATCH.
    daily        : True adiciona aggregation_type=DAILY.

    Retorna lista achatada, uma linha por (data, ad_group_id):
        [{"date": "YYYY-MM-DD", "ad_group_id": 123, "tags": [...], "metrics": {...}}]
    """
    ids = [str(x) for x in (ad_group_ids.split(",") if isinstance(ad_group_ids, str)
                            else ad_group_ids) if str(x).strip()]
    if not ids:
        raise ValueError("get_ad_group_metrics exige ad_group_ids (a API não "
                         "aceita esse endpoint sem filters[ad_group_ids] em range > 1 dia)")

    path = (f"/advertising/{site_id}/product_ads/campaigns/{campaign_id}"
            f"/ad_groups/metrics")
    out = []
    for chunk in _chunks(ids, _AD_GROUP_IDS_BATCH):
        params = {
            "date_from": date_from, "date_to": date_to, "metrics": metrics,
            "filters[ad_group_ids]": ",".join(chunk),
        }
        if daily:
            params["aggregation_type"] = AGGREGATION_DAILY

        body = _get(path, token, params)
        for day in (body or []):
            date = day.get("date")
            for row in day.get("results", []) or []:
                out.append({
                    "date": date,
                    "ad_group_id": row.get("ad_group_id"),
                    "tags": row.get("tags", []),
                    "metrics": row.get("metrics", {}) or {},
                })
    return out


# ── util ────────────────────────────────────────────────────────────────────

def _csv(value):
    return value if isinstance(value, str) else ",".join(str(v) for v in value)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
