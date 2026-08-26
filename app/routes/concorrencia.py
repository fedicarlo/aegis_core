from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from app.database import (
    create_produto_referencia, get_produtos_referencia, get_produto_referencia,
    get_produto_referencia_for_item, link_item_to_produto_referencia,
    unlink_item_from_produto_referencia, get_items_for_produto_referencia,
    search_items_cross_account, create_concorrente, get_concorrentes_for_produto,
    update_concorrente_preco, confirmar_ciencia_concorrente, get_concorrente_price_history,
    get_marcas_monitoraveis, set_marca_monitora_pma, get_all_accounts,
)
from app.services.meli_api import extract_mlb_id, get_public_item_info
from app.services.meli_auth import get_valid_token
from app.utils.logger import get_logger

log = get_logger("routes.concorrencia")
concorrencia_bp = Blueprint("concorrencia", __name__)


# ── Listagem / criação de produto-referência ──────────────────────────────────

@concorrencia_bp.route("/concorrencia")
def lista():
    search = request.args.get("q", "").strip()
    produtos = get_produtos_referencia(search)
    marcas = get_marcas_monitoraveis()
    return render_template(
        "concorrencia_lista.html",
        produtos=produtos, search=search, marcas=marcas,
    )


@concorrencia_bp.route("/concorrencia/novo", methods=["POST"])
def novo():
    nome = request.form.get("nome", "").strip()
    marca = request.form.get("marca", "").strip()
    if not nome:
        flash("Nome do produto-referência é obrigatório.", "error")
        return redirect(url_for("concorrencia.lista"))

    pr_id = create_produto_referencia(nome, marca)
    flash(f"Produto-referência '{nome}' criado.", "success")
    return redirect(url_for("concorrencia.detalhe", produto_referencia_id=pr_id))


# ── Detalhe do produto-referência ─────────────────────────────────────────────

@concorrencia_bp.route("/concorrencia/<int:produto_referencia_id>")
def detalhe(produto_referencia_id):
    produto = get_produto_referencia(produto_referencia_id)
    if not produto:
        flash("Produto-referência não encontrado.", "error")
        return redirect(url_for("concorrencia.lista"))

    itens = get_items_for_produto_referencia(produto_referencia_id)
    concorrentes = get_concorrentes_for_produto(produto_referencia_id)

    return render_template(
        "concorrencia_detalhe.html",
        produto=produto, itens=itens, concorrentes=concorrentes,
    )


@concorrencia_bp.route("/concorrencia/<int:produto_referencia_id>/buscar-itens")
def buscar_itens(produto_referencia_id):
    q = request.args.get("q", "")
    results = search_items_cross_account(q)
    for r in results:
        existing = get_produto_referencia_for_item(r["seller_id"], r["item_id"])
        r["ja_vinculado"] = existing["nome"] if existing else None
    return jsonify(results)


@concorrencia_bp.route("/concorrencia/<int:produto_referencia_id>/vincular", methods=["POST"])
def vincular(produto_referencia_id):
    seller_id = request.form.get("seller_id", "").strip()
    item_id = request.form.get("item_id", "").strip()

    if not seller_id or not item_id:
        flash("Selecione um item pra vincular.", "error")
        return redirect(url_for("concorrencia.detalhe", produto_referencia_id=produto_referencia_id))

    existing = get_produto_referencia_for_item(seller_id, item_id)
    if existing and existing["id"] != produto_referencia_id:
        flash(f"Esse item já está vinculado ao produto-referência '{existing['nome']}'.", "error")
        return redirect(url_for("concorrencia.detalhe", produto_referencia_id=produto_referencia_id))

    link_item_to_produto_referencia(produto_referencia_id, seller_id, item_id)
    flash("Item vinculado.", "success")
    return redirect(url_for("concorrencia.detalhe", produto_referencia_id=produto_referencia_id))


@concorrencia_bp.route("/concorrencia/<int:produto_referencia_id>/desvincular", methods=["POST"])
def desvincular(produto_referencia_id):
    seller_id = request.form.get("seller_id", "").strip()
    item_id = request.form.get("item_id", "").strip()
    unlink_item_from_produto_referencia(produto_referencia_id, seller_id, item_id)
    flash("Item desvinculado.", "success")
    return redirect(url_for("concorrencia.detalhe", produto_referencia_id=produto_referencia_id))


# ── Concorrentes ───────────────────────────────────────────────────────────────

@concorrencia_bp.route("/concorrencia/<int:produto_referencia_id>/concorrente", methods=["POST"])
def add_concorrente(produto_referencia_id):
    url = request.form.get("url", "").strip()
    nome_seller = request.form.get("nome_seller", "").strip()
    preco = request.form.get("preco_atual", "").strip()

    if not url:
        flash("URL do concorrente é obrigatória.", "error")
        return redirect(url_for("concorrencia.detalhe", produto_referencia_id=produto_referencia_id))

    create_concorrente(produto_referencia_id, url, nome_seller, preco or None)
    flash("Concorrente adicionado.", "success")
    return redirect(url_for("concorrencia.detalhe", produto_referencia_id=produto_referencia_id))


@concorrencia_bp.route("/concorrencia/concorrente/<int:concorrente_id>/preco", methods=["POST"])
def atualizar_preco(concorrente_id):
    data = request.get_json(silent=True) or {}
    try:
        novo_preco = float(data.get("preco"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Preço inválido."), 400

    update_concorrente_preco(concorrente_id, novo_preco)
    return jsonify(ok=True)


@concorrencia_bp.route("/concorrencia/concorrente/<int:concorrente_id>/ciencia", methods=["POST"])
def ciencia(concorrente_id):
    confirmar_ciencia_concorrente(concorrente_id)
    return jsonify(ok=True)


@concorrencia_bp.route("/concorrencia/concorrente/<int:concorrente_id>/historico")
def historico(concorrente_id):
    return jsonify(get_concorrente_price_history(concorrente_id))


@concorrencia_bp.route("/concorrencia/extrair-mlb")
def extrair_mlb():
    """Best-effort: tenta preencher nome/preço a partir do link colado. Nunca
    trava o fluxo — se não conseguir, o formulário manual continua disponível.

    A API do ML exige token mesmo pra ler item de outro seller (não tem
    endpoint público anônimo de verdade) — usa o token de qualquer uma das
    nossas contas autorizadas, já que é só leitura de dado de catálogo."""
    url = request.args.get("url", "")
    item_id = extract_mlb_id(url)
    if not item_id:
        return jsonify(ok=False)

    account = next((a for a in get_all_accounts() if a.get("access_token")), None)
    if not account:
        return jsonify(ok=False)

    try:
        token = get_valid_token(account)
    except Exception:
        return jsonify(ok=False)

    info = get_public_item_info(item_id, token)
    if not info or not info.get("title"):
        return jsonify(ok=False)

    return jsonify(ok=True, **info)


# ── Marcas monitoradas (PMA) ───────────────────────────────────────────────────

@concorrencia_bp.route("/concorrencia/marcas/<marca_normalizada>/toggle", methods=["POST"])
def toggle_marca(marca_normalizada):
    data = request.get_json(silent=True) or {}
    value = bool(data.get("value"))
    set_marca_monitora_pma(marca_normalizada, value)
    return jsonify(ok=True, value=value)
