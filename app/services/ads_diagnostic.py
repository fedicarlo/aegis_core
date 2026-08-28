"""
Ads Diagnostic + Recommendation Engine (Etapa 6).

Motor DETERMINÍSTICO dos casos A-E da spec (§7). Entrada: (scope, target, período).

Regras de arquitetura respeitadas:
  - Gate de amostra ANTES de qualquer diagnóstico: se `sample_sufficiency` diz que
    não dá, é Caso E e PARA (não avalia A/B/C/D).
  - Todo caso separa FATO (medido) de HIPÓTESE (inferido, com nível de confiança).
    Nunca apresenta hipótese como conclusão.
  - Não migra estágio de nada. Se development_rules/consolidation_rules do profile
    estão vazias, devolve estagio.avaliavel = False.
  - Órfão/EMPTY: herda serving=False do Metrics Engine -> status NAO_VEICULANDO,
    sem diagnóstico.
  - Todos os limiares vêm de ads_strategy.diagnostic_rules (profile). Zero número
    de metodologia hardcoded aqui.

recommend() devolve sempre {proxima_acao, porque:{fatos,hipoteses}, caso, confianca}
— nunca uma métrica solta.
"""
from app.services import ads_finance, ads_metrics, ads_strategy
from app.utils.logger import get_logger

log = get_logger("ads_diagnostic")

CONF_ALTA, CONF_MEDIA, CONF_BAIXA = "alta", "media", "baixa"

CASE_NAMES = {
    "A": "Exposição (competitividade / Ad Rank)",
    "B": "Clique (CTR baixo)",
    "C": "Conversão (clique não vira venda)",
    "D": "Econômico (eficiente comercialmente, inadequado economicamente)",
    "E": "Amostra insuficiente",
}


def _pct(v):
    """API traz impression share como fração (0.22). Normaliza p/ % legível."""
    return None if v is None else round(v * 100, 2)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Casos ──────────────────────────────────────────────────────────────────

def _caso_E(ss):
    faltas = ss.get("motivo") or []
    n = ss.get("numeros") or {}
    fatos = [f"Amostra insuficiente no período: {', '.join(faltas)}."]
    if n:
        fatos.append(
            f"sinal do anúncio: {n.get('ads_clicks')} cliques, {n.get('ads_units')} unidade(s) "
            f"atribuída(s), {n.get('ads_orders')} venda(s) atribuída(s), "
            f"{n.get('days_with_prints')} dia(s) com veiculação. "
            f"Vendas reais do(s) item(ns): {n.get('real_units')} un / {n.get('real_orders')} pedido(s) / "
            f"{n.get('unique_buyers')} comprador(es) único(s); maior pedido "
            f"{n.get('max_single_order_qty')} un ({n.get('single_order_dominance_pct')}%)."
        )
    return {
        "caso": "E", "nome": CASE_NAMES["E"], "avaliavel": True,
        "fatos": fatos, "hipoteses": [],
        "acao_sugerida": "Não diagnosticar ainda — aguardar volume mínimo antes de concluir qualquer coisa.",
    }


def _caso_A(m, dr):
    ish = m.get("impression_share")
    if not ish or ish.get("impression_share") is None:
        return {"caso": "A", "nome": CASE_NAMES["A"], "avaliavel": False,
                "motivo_nao_avaliavel": "impression share não existe no nível de ad group — só de campanha",
                "fatos": [], "hipoteses": [], "acao_sugerida": None}

    is_pct = _pct(ish.get("impression_share"))
    lost_rank = _pct(ish.get("lost_impression_share_by_ad_rank"))
    lost_budget = _pct(ish.get("lost_impression_share_by_budget"))
    bench = ish.get("acos_benchmark")
    f = m["funnel"]

    matched = (is_pct is not None and is_pct < dr["impression_share_baixo_pct"]
               and lost_rank is not None and lost_rank > dr["lost_by_ad_rank_alto_pct"])
    if not matched:
        return None

    budget_dominante = (lost_budget or 0) >= (lost_rank or 0)
    fatos = [
        f"Impression share {is_pct}% (limiar {dr['impression_share_baixo_pct']}%): "
        f"perdeu {lost_rank}% das impressões por classificação e {lost_budget}% por orçamento.",
    ]
    if bench is not None and f.get("acos") is not None:
        fatos.append(
            f"ACOS realizado {f['acos']}% vs. benchmark ML {round(bench, 1)}% — "
            + ("há folga vs. o mercado." if f["acos"] < bench else "acima do benchmark do mercado.")
        )
    hip = []
    if not budget_dominante:
        conf = CONF_MEDIA if (bench is not None and f.get("acos") is not None and f["acos"] < bench) else CONF_BAIXA
        hip.append({"texto": "O ROAS-alvo da campanha pode estar restringindo a competitividade do lance "
                             "(Ad Rank), limitando a exposição.", "confianca": conf})
        hip.append({"texto": "Relevância / qualidade do anúncio (SEO da ficha, atributos, imagens) também "
                             "pesa no Ad Rank — pode não ser só o lance.", "confianca": CONF_BAIXA})
        acao = ("Investigar ROAS-alvo, relevância e qualidade do anúncio. NÃO concluir que é o ROAS "
                "sem testar — subir o alvo e medir ganho de share/venda.")
    else:
        hip.append({"texto": "A perda de exposição é predominantemente por ORÇAMENTO, não por "
                             "classificação — a campanha bate o teto de verba.", "confianca": CONF_MEDIA})
        acao = "Avaliar aumento de orçamento diário e medir o ganho de impressões/venda antes de mexer no ROAS-alvo."
    return {"caso": "A", "nome": CASE_NAMES["A"], "avaliavel": True,
            "fatos": fatos, "hipoteses": hip, "acao_sugerida": acao}


def _caso_B(m, dr):
    f = m["funnel"]
    ctr = _f(f.get("ctr"))
    if ctr is None or ctr >= dr["ctr_floor_pct"]:
        return None
    return {
        "caso": "B", "nome": CASE_NAMES["B"], "avaliavel": True,
        "fatos": [
            f"CTR {ctr}% (piso configurado {dr['ctr_floor_pct']}%), "
            f"com {f.get('prints')} impressões e {f.get('clicks')} cliques no período.",
        ],
        "hipoteses": [
            {"texto": "Imagem principal, preço vs. concorrência, promoção, título ou prova social "
                      "(reputação/avaliações) podem estar desalinhados — a impressão não converte em clique.",
             "confianca": CONF_BAIXA},
        ],
        "acao_sugerida": "Investigar capa, preço relativo, promoção ativa, título e prova social do anúncio.",
    }


def _caso_C(m, dr):
    f = m["funnel"]
    ctr = _f(f.get("ctr"))
    clicks = _f(f.get("clicks")) or 0
    cvr = _f(f.get("cvr"))
    if ctr is None or ctr < dr["ctr_floor_pct"]:
        return None  # o gargalo é antes (clique), não conversão
    if clicks < dr["min_clicks_para_conversao"]:
        return None  # cliques de menos pra afirmar algo sobre conversão
    if cvr is None or cvr >= dr["cvr_floor_pct"]:
        return None
    return {
        "caso": "C", "nome": CASE_NAMES["C"], "avaliavel": True,
        "fatos": [
            f"{int(clicks)} cliques e {f.get('ads_units')} venda(s) atribuída(s) no período — "
            f"CVR {cvr}% (piso {dr['cvr_floor_pct']}%). CTR {ctr}% está acima do piso, "
            f"então o problema é do clique pra frente.",
        ],
        "hipoteses": [
            {"texto": "Página/ficha do anúncio, oferta, preço final, prazo ou custo de entrega, ou "
                      "avaliações podem estar travando a conversão depois do clique.",
             "confianca": CONF_BAIXA},
        ],
        "acao_sugerida": "Investigar página do anúncio, ficha técnica, preço final, logística (prazo/frete) e avaliações.",
    }


def _caso_D(m, fin):
    """
    Duas lentes (Finance Engine as rotula separadas — não misturar):
      BLENDED: margem depois de Ads sobre a receita REAL total (inclui orgânica).
               atinge_meta_blended = "a operação fecha na meta?"
      AD:      o Ads se paga sozinho na meta? roas_realizado vs roas_minimo_operacional.
    Caso D dispara se: economicamente viável (roas >= equilíbrio) E
      (não fecha no blended) OU (fecha no blended mas o Ads não se paga sozinho —
       o orgânico está subsidiando) OU (meta inatingível nem sem Ads).
    """
    f = m["funnel"]
    roas = _f(fin.get("roas_realizado")) or _f(f.get("roas"))
    be = _f(fin.get("roas_equilibrio"))
    rmo = _f(fin.get("roas_minimo_operacional"))
    margem_alvo = fin.get("margem_alvo_pct")
    margem_depois = fin.get("margem_depois_ads_pct")
    atinge_blended = fin.get("atinge_meta_blended")
    ads_ok = fin.get("ads_auto_suficiente_na_meta")
    inatingivel = fin.get("margem_alvo_inatingivel")
    tacos = fin.get("tacos_pct")
    tacos_max = fin.get("tacos_maximo_operacional_pct")

    if roas is None:
        return None
    if be is not None and roas < be:
        return None  # perdendo dinheiro na venda atribuída — não é "só" econômico, é pior (vai pro Alert)

    dispara = bool(inatingivel) or (atinge_blended is False) or (ads_ok is False)
    if not dispara:
        return None

    fatos = [
        f"Margem antes de Ads {fin.get('margem_antes_ads_pct')}%; margem DEPOIS de Ads "
        f"(sobre a receita total, com orgânica) {margem_depois}% vs. meta {margem_alvo}%.",
        f"TACOS {tacos}% (máximo p/ a meta: {tacos_max}%). "
        f"ROAS realizado {roas} — equilíbrio {be}, mínimo operacional p/ o Ads se pagar "
        f"sozinho na meta = {rmo}.",
    ]
    if fin.get("custo_incompleto"):
        fatos.append(f"ATENÇÃO: {len(fin.get('itens_sem_custo') or [])} SKU(s) com venda mas sem custo "
                     f"cadastrado — a margem calculada ignora esses itens.")

    hip = []
    if inatingivel:
        hip.append({"texto": "A margem-alvo não é alcançável nem sem Ads — o problema é a estrutura de "
                             "custo/preço do SKU, não a campanha.", "confianca": CONF_MEDIA})
        acao = ("Rever a meta de margem OU a estrutura de custo/preço do produto antes de mexer na "
                "campanha — Ads não resolve margem que já não fecha sem Ads.")
    elif atinge_blended is False:
        hip.append({"texto": "O ROAS-alvo pode estar calibrado acima do que a margem real do produto "
                             "sustenta — a operação não fecha na meta nem somando a venda orgânica.",
                    "confianca": CONF_MEDIA})
        acao = ("Revisar o ROAS-alvo à luz da margem real; ou assumir a margem menor de propósito, "
                "como custo de aquisição, se a exposição for estratégica.")
    else:  # fecha no blended, mas o Ads não se paga sozinho
        hip.append({"texto": "A operação fecha na meta NO TOTAL, mas as vendas atribuídas ao Ads não se "
                             "pagam nessa margem — o orgânico está subsidiando o Ads. Se o objetivo é "
                             "o Ads auto-suficiente, o ROAS-alvo está agressivo demais.",
                    "confianca": CONF_MEDIA})
        acao = ("Decidir a intenção do Ads: se é pra se pagar sozinho, subir o ROAS-alvo / rever "
                "SKU; se é pra ganhar exposição e o total fecha, pode estar OK — é decisão estratégica.")
    return {"caso": "D", "nome": CASE_NAMES["D"], "avaliavel": True,
            "fatos": fatos, "hipoteses": hip, "acao_sugerida": acao}


# ── Orquestração ───────────────────────────────────────────────────────────

def diagnose(seller_id, scope, target_id, date_from, date_to, *, since_last_change=False):
    if scope == "campaign":
        m = ads_metrics.campaign_metrics(seller_id, target_id, date_from, date_to,
                                         since_last_change=since_last_change, include_series=False)
        fin_fn = ads_finance.campaign_finance
    elif scope == "ad_group":
        m = ads_metrics.ad_group_metrics(seller_id, target_id, date_from, date_to,
                                         since_last_change=since_last_change, include_series=False)
        fin_fn = ads_finance.ad_group_finance
    else:
        raise ValueError(f"scope inválido: {scope}")

    if not m.get("found"):
        return {"found": False, "scope": scope, "target_id": target_id}

    out = {
        "found": True, "scope": scope, "target_id": target_id,
        "window": m["window"], "serving": m["serving"], "status": m["status"],
        "not_serving_reason": m.get("not_serving_reason"),
        "caso_primario": None, "casos": [], "fatos_gerais": [],
        "estagio": _estagio_stub(seller_id),
    }

    if not m["serving"]:
        out["status"] = "NAO_VEICULANDO"
        return out

    ss = ads_metrics.sample_sufficiency(seller_id, scope, target_id, date_from, date_to,
                                        since_last_change=since_last_change)
    out["amostra"] = {"suficiente": ss.get("suficiente"), "motivo": ss.get("motivo"),
                      "numeros": ss.get("numeros")}

    # Gate: sem amostra -> Caso E, PARA.
    if not ss.get("suficiente"):
        e = _caso_E(ss)
        out["caso_primario"] = "E"
        out["casos"] = [e]
        out["status"] = "APRENDIZADO"
        return out

    dr = ads_strategy.diagnostic_rules(seller_id)
    fin_full = fin_fn(seller_id, target_id, date_from, date_to,
                      since_last_change=since_last_change)
    fin = fin_full.get("finance") or {}

    if m["funnel"].get("attribution_exceeds_real"):
        out["fatos_gerais"].append(
            "Receita atribuída pelo ML > receita real dos itens do alvo (venda assistida de outros "
            "produtos) — cruzamentos financeiros deste alvo têm essa ressalva."
        )

    a = _caso_A(m, dr)
    b = _caso_B(m, dr)
    c = _caso_C(m, dr)
    d = _caso_D(m, fin) if fin else None

    matched = [x for x in (a, b, c, d) if x and x.get("avaliavel") and (x["fatos"] or x["caso"] == "E")]
    # A pode vir avaliavel=False (ad group) — registra como informativo
    if a and not a.get("avaliavel"):
        out["casos"].append(a)

    out["casos"].extend(matched)

    funnel_case = next((x["caso"] for x in (a, b, c) if x and x.get("avaliavel") and x["fatos"]), None)
    d_matched = d is not None

    if funnel_case and d_matched:
        # exposição (A) + economia -> ambos apontam pro ROAS-alvo, lidera D.
        # clique/conversão (B/C) são problemas de criativo/página, independentes -> lidera o funil.
        primario = "D" if funnel_case == "A" else funnel_case
    elif funnel_case:
        primario = funnel_case
    elif d_matched:
        primario = "D"
    else:
        primario = None

    out["caso_primario"] = primario
    out["status"] = "COM_GARGALO" if primario else "SAUDAVEL"
    return out


def _estagio_stub(seller_id):
    dev = ads_strategy.development_rules(seller_id)
    con = ads_strategy.consolidation_rules(seller_id)
    configurado = any(v is not None for v in dev.values()) or any(v is not None for v in con.values())
    return {
        "avaliavel": configurado,
        "motivo": None if configurado else
        "development_rules/consolidation_rules não configuradas no Strategy Profile — "
        "transição de estágio não é sugerida automaticamente (spec §8).",
    }


# ── Recommendation ─────────────────────────────────────────────────────────

def recommend(seller_id, scope, target_id, date_from, date_to, *, since_last_change=False):
    dg = diagnose(seller_id, scope, target_id, date_from, date_to,
                  since_last_change=since_last_change)
    if not dg.get("found"):
        return {"found": False, "scope": scope, "target_id": target_id}

    base = {"found": True, "scope": scope, "target_id": target_id,
            "window": dg["window"], "status": dg["status"], "caso": dg.get("caso_primario")}

    if not dg["serving"]:
        return {**base,
                "proxima_acao": f"Nenhuma — o alvo não está veiculando ({dg['not_serving_reason']}).",
                "porque": {"fatos": [f"serving=False, motivo={dg['not_serving_reason']}"], "hipoteses": []},
                "confianca": CONF_ALTA}

    if dg["caso_primario"] == "E":
        e = dg["casos"][0]
        return {**base, "proxima_acao": e["acao_sugerida"],
                "porque": {"fatos": e["fatos"], "hipoteses": []},
                "confianca": CONF_ALTA}  # alta confiança de que a amostra é insuficiente

    if not dg["caso_primario"]:
        return {**base,
                "proxima_acao": "Manter — nenhum gargalo detectado no período. Acompanhar margem depois de Ads.",
                "porque": {"fatos": dg["fatos_gerais"] or ["Funil e economia dentro dos limiares configurados."],
                           "hipoteses": []},
                "confianca": CONF_MEDIA}

    caso = next(x for x in dg["casos"] if x["caso"] == dg["caso_primario"])
    # confiança da recomendação = a da hipótese principal do caso (as hipóteses são
    # escritas em ordem de prioridade; as demais são ressalvas, não travam a ação).
    conf = caso["hipoteses"][0]["confianca"] if caso["hipoteses"] else CONF_MEDIA
    outros = [x["caso"] for x in dg["casos"] if x["caso"] != dg["caso_primario"] and x.get("avaliavel")]
    return {
        **base,
        "proxima_acao": caso["acao_sugerida"],
        "porque": {"fatos": caso["fatos"] + dg["fatos_gerais"], "hipoteses": caso["hipoteses"]},
        "casos_relacionados": outros,
        "confianca": conf,
    }
