import re
import datetime as dt
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from app.database import (
    get_all_accounts,
    get_product_full_data,
    get_stock_history,
    get_product_orders_daily,
)
from app.services.analytics import (
    compute_all,
    compute_risk_score,
    compute_rupture_impact,
    compute_induction_enhanced,
    compute_margin,
)

produto_bp = Blueprint("produto", __name__)

# ── Keyword extractor ─────────────────────────────────────────────────────────

_STOP = {
    # preposições / artigos / conjunções
    'de','da','do','das','dos','a','o','as','os','um','uma','uns','umas',
    'e','ou','para','com','sem','por','pelo','pela','pelos','pelas',
    'em','no','na','nos','nas','ao','aos','às','se','que','mais','menos',
    'muito','pouco','seu','sua',
    # descritores genéricos
    'liquid','liquido','líquido','premium','original','natural','orgânico',
    'organico','novo','nova','kit','combo','pack','plus','pro','max','ultra',
    'super','extra','especial','novo','nova','puro','pura','100',
    # marcas/palavras que estreitam demais a busca
    'true','source',
    # tamanhos / unidades (backup — regex já pega a maioria)
    'ml','mg','g','kg','mcg','litro','litros','oz','caps','capsulas',
    'cápsulas','comprimidos','comp','un','unid','gotas','sachê','sache',
    'frasco','pote','caixa','envelope',
    # sabores
    'morango','limão','limao','laranja','baunilha','maçã','maca','framboesa',
    'uva','maracuja','maracujá','coco','menta','hortelã','hortela',
    'chocolate','mel','cereja','goiaba','abacaxi','melancia','manga',
    'pessego','pêssego','acerola','caju','abacate','tutti','frutti',
    'frutas','fruta','misto','sabor','sabores','bala','doce',
    # adjetivos não-diferenciadores
    'infantil','kids','adulto','adulta','masculino','feminino',
    'pequeno','médio','grande',
}

_UNIT_RE = re.compile(
    r'^\d+[\.,]?\d*\s*(ml|mg|mcg|µg|g|kg|l|oz|cap|caps|un|gr|cm|mm)s?$', re.I
)
_NUM_RE = re.compile(r'^\d+[\.,]?\d*$')


def _extract_keywords(title: str, n: int = 4) -> str:
    """
    Extrai as N primeiras palavras relevantes do título, descartando:
    - artigos/preposições, sabores, tamanhos, números, marcas genéricas
    """
    # Normalize unicode accents minimally, split on whitespace/ponctuation
    tokens = re.split(r'[\s\-\/|,;]+', title)
    kept = []
    for tok in tokens:
        clean = tok.strip().rstrip('.,;:!?')
        if not clean or len(clean) < 2:
            continue
        lower = clean.lower()
        if lower in _STOP:
            continue
        if _UNIT_RE.match(lower) or _NUM_RE.match(lower):
            continue
        kept.append(clean)
        if len(kept) >= n:
            break
    return ' '.join(kept)


# ── Market analysis ───────────────────────────────────────────────────────────

def _market_analysis(competitors: list, own_price: float) -> dict:
    prices = [c["price"] for c in competitors if c["price"] > 0]
    if not prices:
        return {
            "prices": [], "min_price": None, "avg_price": None,
            "max_price": None, "position": None, "delta_pct": None,
        }
    avg = sum(prices) / len(prices)
    delta_pct = round((own_price - avg) / avg * 100, 1) if avg > 0 else 0
    position  = "acima" if delta_pct > 5 else "abaixo" if delta_pct < -5 else "na média"
    return {
        "prices":    prices,
        "min_price": round(min(prices), 2),
        "avg_price": round(avg, 2),
        "max_price": round(max(prices), 2),
        "position":  position,
        "delta_pct": delta_pct,
    }


def _fetch_competitors(account: dict, title: str, seller_id: str):
    """
    Tenta buscar concorrentes com keyword extraction + fallback progressivo.
    Retorna (competitors, query_used).
    """
    from app.services.meli_api import search_competitors
    from app.services.meli_auth import get_valid_token

    token = get_valid_token(account)

    for n_words in (4, 3, 2):
        query = _extract_keywords(title, n=n_words)
        if not query:
            continue
        results = search_competitors(token, query, seller_id, limit=10)
        if results:
            return results, query

    return [], _extract_keywords(title, n=2) or title[:40]


# ── Main page ─────────────────────────────────────────────────────────────────

@produto_bp.route("/produto/<seller_id>/<item_id>")
def produto(seller_id: str, item_id: str):
    accounts   = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]

    account = next((a for a in authorized if str(a["seller_id"]) == str(seller_id)), None)
    if not account:
        flash("Conta não encontrada ou não autorizada.", "error")
        return redirect(url_for("web.index"))

    all_items = compute_all(seller_id)
    item_flat = next((i for i in all_items if i["id"] == item_id), None)
    if not item_flat:
        flash(f"Produto {item_id} não encontrado.", "error")
        return redirect(url_for("web.dashboard") + f"?account={account['name']}")

    full_data     = get_product_full_data(seller_id, item_id)
    stock_history = get_stock_history(seller_id, item_id, days=90)
    orders_daily  = get_product_orders_daily(seller_id, item_id, days=90)

    risk      = compute_risk_score(item_flat, stock_history)
    impact    = compute_rupture_impact(seller_id, item_id, days=90)
    induction = compute_induction_enhanced(item_flat, stock_history)

    margin_item = None
    try:
        md = compute_margin(seller_id, days=30)
        margin_item = next(
            (r for r in md.get("breakdown", []) if r["item_id"] == item_id), None
        )
    except Exception:
        pass

    today  = dt.date.today()
    dates  = [(today - dt.timedelta(days=89 - i)).isoformat() for i in range(90)]
    orders_map = {r["day"]:  r["units"]        for r in orders_daily}
    stock_map  = {r["date"]: r["available_qty"] for r in stock_history}
    giro_7d    = item_flat.get("giro_7d", 0)

    chart_labels   = dates
    chart_orders   = [orders_map.get(d, 0) for d in dates]
    chart_stock    = [stock_map.get(d)      for d in dates]
    chart_ruptures = [1 if (stock_map.get(d) == 0 and giro_7d > 0) else 0 for d in dates]

    # Competitors — non-fatal
    competitors       = []
    market            = {}
    competitors_query = ""
    try:
        competitors, competitors_query = _fetch_competitors(
            account, item_flat["title"], seller_id
        )
        market = _market_analysis(competitors, item_flat["price"] or 0)
    except Exception:
        pass

    return render_template(
        "produto.html",
        account             = account,
        authorized          = authorized,
        item                = item_flat,
        full                = full_data,
        risk                = risk,
        impact              = impact,
        induction           = induction,
        margin_item         = margin_item,
        seller_id           = seller_id,
        chart_labels        = chart_labels,
        chart_orders        = chart_orders,
        chart_stock         = chart_stock,
        chart_ruptures      = chart_ruptures,
        competitors         = competitors,
        competitors_query   = competitors_query,
        market              = market,
    )


# ── JSON endpoint for re-search ───────────────────────────────────────────────

@produto_bp.route("/produto/<seller_id>/<item_id>/concorrentes")
def concorrentes_json(seller_id: str, item_id: str):
    accounts   = get_all_accounts()
    authorized = [a for a in accounts if a.get("access_token")]
    account    = next((a for a in authorized if str(a["seller_id"]) == str(seller_id)), None)

    if not account:
        return jsonify({"ok": False, "error": "Conta não encontrada"}), 404

    # Accept query override from ?q=
    all_items = compute_all(seller_id)
    item_flat = next((i for i in all_items if i["id"] == item_id), None)
    if not item_flat:
        return jsonify({"ok": False, "error": "Produto não encontrado"}), 404

    custom_q = request.args.get("q", "").strip()

    try:
        from app.services.meli_api import search_competitors
        from app.services.meli_auth import get_valid_token

        token = get_valid_token(account)

        if custom_q:
            competitors   = search_competitors(token, custom_q, seller_id, limit=10)
            query_used    = custom_q
            fallback_used = False
        else:
            competitors, query_used = _fetch_competitors(
                account, item_flat["title"], seller_id
            )
            fallback_used = (query_used != _extract_keywords(item_flat["title"], n=4))

        market = _market_analysis(competitors, item_flat["price"] or 0)

        return jsonify({
            "ok":           True,
            "query":        query_used,
            "fallback":     fallback_used,
            "own_price":    item_flat["price"],
            "market":       market,
            "competitors":  competitors,
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
