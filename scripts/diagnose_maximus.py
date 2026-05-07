"""
Diagnóstico da conta Maximus — AEGIS
Roda a partir da raiz do projeto:
    python -m scripts.diagnose_maximus

Investiga:
  1. Status e tags de todos os anúncios (pausados × ativos × catálogo)
  2. Campos de fulfillment no item (onde fica o inventory_id)
  3. Resposta bruta do endpoint /items/{id}/fulfillment_stock (atual)
  4. Resposta bruta do endpoint /inventories/{inv_id}/stock/fulfillment (correto)
"""

import sys
import os
import json
import requests

# Garante que o root do projeto está no path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import get_account_by_name, init_db
from app.services.meli_auth import get_valid_token
from app.services.meli_api import _get, _headers, get_item_ids, get_items_batch
from app.config import MELI_API_URL

SEP  = "─" * 72
SEP2 = "═" * 72

# ── Helpers de exibição ───────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{SEP2}")
    print(f"  {title}")
    print(SEP2)


def subsection(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def dump(label: str, data):
    print(f"\n[{label}]")
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ── 1. Token ──────────────────────────────────────────────────────────────────

section("1. TOKEN DA CONTA MAXIMUS")

init_db()
account = get_account_by_name("Maximus")

if not account:
    print("ERRO: conta 'Maximus' não encontrada no banco.")
    sys.exit(1)

if not account.get("access_token"):
    print("ERRO: conta 'Maximus' não está autorizada (sem token).")
    sys.exit(1)

token = get_valid_token(account)
print(f"seller_id : {account['seller_id']}")
print(f"nickname  : {account['nickname']}")
print(f"token ok  : {'sim' if token else 'nao'}")


# ── 2. Contagem de anúncios por status ────────────────────────────────────────

section("2. CONTAGEM DE ANÚNCIOS POR STATUS")

seller_id = account["seller_id"]

for status in ("active", "paused", "closed", "under_review"):
    try:
        ids = get_item_ids(token, seller_id, status=status)
        print(f"  {status:15s}: {len(ids)} anúncios")
    except Exception as e:
        print(f"  {status:15s}: ERRO — {e}")


# ── 3. Detalhes dos primeiros 5 anúncios ─────────────────────────────────────

section("3. DETALHES — PRIMEIROS 5 ANÚNCIOS (paused + active)")

all_ids = get_item_ids(token, seller_id, status="active") + \
          get_item_ids(token, seller_id, status="paused")

sample_ids  = all_ids[:5]
sample_items = get_items_batch(token, sample_ids) if sample_ids else []

for item in sample_items:
    subsection(f"{item['id']} — {item.get('title', '')[:60]}")
    print(f"  status              : {item.get('status')}")
    print(f"  price               : {item.get('price')}")
    print(f"  available_quantity  : {item.get('available_quantity')}")
    print(f"  sold_quantity       : {item.get('sold_quantity')}")
    print(f"  catalog_product_id  : {item.get('catalog_product_id')}")
    print(f"  tags                : {item.get('tags', [])}")

    shipping = item.get("shipping", {})
    print(f"  shipping.logistic_type : {shipping.get('logistic_type')}")
    print(f"  shipping.mode          : {shipping.get('mode')}")

    # inventory_id pode estar na raiz ou nas variações
    print(f"  inventory_id (raiz) : {item.get('inventory_id')}")

    variations = item.get("variations", [])
    if variations:
        for i, v in enumerate(variations[:3]):
            print(f"  variations[{i}].inventory_id : {v.get('inventory_id')}")
    else:
        print(f"  variations          : (sem variações)")


# ── 4. Teste do endpoint atual: /items/{id}/fulfillment_stock ─────────────────

section("4. ENDPOINT ATUAL — /items/{id}/fulfillment_stock")

for item in sample_items[:3]:
    item_id = item["id"]
    logistic = item.get("shipping", {}).get("logistic_type", "?")
    subsection(f"{item_id}  (logistic_type={logistic})")

    url = f"{MELI_API_URL}/items/{item_id}/fulfillment_stock"
    try:
        res = requests.get(url, headers=_headers(token), timeout=15)
        print(f"  HTTP {res.status_code}")
        if res.status_code == 200:
            dump("resposta", res.json())
        else:
            print(f"  corpo: {res.text[:500]}")
    except Exception as e:
        print(f"  ERRO: {e}")


# ── 5. Teste do endpoint correto: /inventories/{inv_id}/stock/fulfillment ──────

section("5. ENDPOINT INVENTÁRIO — /inventories/{inv_id}/stock/fulfillment")

for item in sample_items[:3]:
    item_id = item["id"]

    # Coleta todos os inventory_ids disponíveis no item
    inv_ids = set()
    if item.get("inventory_id"):
        inv_ids.add(item["inventory_id"])
    for v in item.get("variations", []):
        if v.get("inventory_id"):
            inv_ids.add(v["inventory_id"])

    subsection(f"{item_id}  — inventory_ids encontrados: {inv_ids or 'nenhum'}")

    if not inv_ids:
        print("  Pulando — sem inventory_id no item.")
        continue

    for inv_id in inv_ids:
        url = f"{MELI_API_URL}/inventories/{inv_id}/stock/fulfillment"
        print(f"\n  GET {url}")
        try:
            res = requests.get(url, headers=_headers(token), timeout=15)
            print(f"  HTTP {res.status_code}")
            if res.status_code == 200:
                dump("resposta", res.json())
            else:
                print(f"  corpo: {res.text[:500]}")
        except Exception as e:
            print(f"  ERRO: {e}")


# ── 6. Campos brutos de fulfillment do primeiro item FULL ─────────────────────

section("6. JSON COMPLETO DE FULFILLMENT — PRIMEIRO ITEM COM logistic_type=fulfillment")

full_item = next(
    (i for i in sample_items if i.get("shipping", {}).get("logistic_type") == "fulfillment"),
    None
)

if full_item:
    # Exibe apenas os campos relacionados a estoque/fulfillment
    keys_of_interest = [
        "id", "status", "tags", "available_quantity", "inventory_id",
        "shipping", "variations", "catalog_product_id",
    ]
    filtered = {k: full_item[k] for k in keys_of_interest if k in full_item}
    dump("campos relevantes", filtered)
else:
    print("  Nenhum item com logistic_type=fulfillment nos primeiros 5 itens.")
    print("  Expandindo para todos os anúncios...")

    all_items = get_items_batch(token, all_ids)
    full_item = next(
        (i for i in all_items if i.get("shipping", {}).get("logistic_type") == "fulfillment"),
        None
    )
    if full_item:
        keys_of_interest = [
            "id", "status", "tags", "available_quantity", "inventory_id",
            "shipping", "variations", "catalog_product_id",
        ]
        filtered = {k: full_item[k] for k in keys_of_interest if k in full_item}
        dump("campos relevantes", filtered)
    else:
        print("  Nenhum item com FULL encontrado na conta.")


# ── 7. Resumo ─────────────────────────────────────────────────────────────────

section("7. RESUMO DE TAGS")

all_items_full = get_items_batch(token, all_ids)

tag_counter: dict = {}
status_counter: dict = {}

for item in all_items_full:
    st = item.get("status", "unknown")
    status_counter[st] = status_counter.get(st, 0) + 1
    for tag in item.get("tags", []):
        tag_counter[tag] = tag_counter.get(tag, 0) + 1

print("\n  Status:")
for st, count in sorted(status_counter.items(), key=lambda x: -x[1]):
    print(f"    {st:25s}: {count}")

print("\n  Tags mais frequentes:")
for tag, count in sorted(tag_counter.items(), key=lambda x: -x[1])[:20]:
    print(f"    {tag:40s}: {count}")

print(f"\n{SEP2}")
print("  Diagnóstico concluído.")
print(SEP2)
