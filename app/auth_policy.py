"""
Política de acesso do seller assinante (login multiusuário).

Regra central — allowlist POSITIVA de endpoints + trava de seller_id:

  1. Endpoint (blueprint.func) fora de SELLER_ALLOWED  → 403 (default-deny).
     Qualquer rota nova do admin nasce inacessível pro seller até ser
     explicitamente adicionada aqui.
  2. Endpoint com <seller_id> no path → exige igualdade estrita com
     session["seller_id"]. Manipulação de URL (/custos/<outra_conta>) → 403.
  3. O seller_id efetivo vem SEMPRE da sessão. Nenhum handler de escrita do
     seller lê seller_id de request.view_args / form / args pra decidir o que
     gravar — usa current_seller_id().

O admin (session["role"] == "admin") NÃO passa por aqui — acesso irrestrito,
igual ao comportamento de hoje.
"""
from flask import abort, request, session

# Endpoints que um seller logado pode alcançar. Tudo o mais → 403.
SELLER_ALLOWED = frozenset({
    # landing + as ações de escrita do seller
    "seller.painel",
    "seller.confirm_full_shipment",
    "seller.decide_induction",
    "seller.suggest_status",
    "auth.seller_logout",
    # leitura — rotas existentes, todas travadas por seller_id
    "costs.custos",
    "costs.cost_history",
    "costs.ml_fee",
    "costs.diagnostico",
    "costs.apuracao",
    "calendario.calendario",
    "calendario.calendario_export",
    "analise_queda.analise_queda",
    "relatorio.relatorio",
    "promotions.promocoes",
    "produto.produto",
    "produto.concorrentes_json",
    "stock.estoque",
    # Ads — cockpit read-only (nunca experimentos/config)
    "ads.cockpit",
    "ads.campanhas",
    "ads.campanha",
    "ads.ad_group",
})


def current_seller_id():
    """seller_id da sessão do seller logado (str) ou None. Nunca deriva de URL/form."""
    if session.get("role") == "seller":
        return session.get("seller_id")
    return None


def current_seller_user_id():
    """id do seller_users da sessão (int) ou None."""
    if session.get("role") == "seller":
        return session.get("seller_user_id")
    return None


def enforce_seller_policy():
    """Chamado no before_request quando session['role'] == 'seller'.
    Retorna None pra liberar a request; aborta 403 pra barrar."""
    endpoint = request.endpoint
    if endpoint not in SELLER_ALLOWED:
        abort(403)

    view_args = request.view_args or {}
    if "seller_id" in view_args:
        if str(view_args["seller_id"]) != str(session.get("seller_id") or ""):
            abort(403)
    return None
