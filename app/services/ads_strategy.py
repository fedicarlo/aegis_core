"""
Ads Strategy Engine (Etapa 5) — camada CONFIGURÁVEL.

Toda regra numérica de estratégia/metodologia vive em `ads_strategy_profile`
(JSON no banco), NUNCA hardcoded no código dos engines. Este módulo:

  - carrega o profile (override por seller sobre o profile global `seller_id = ''`)
  - completa buracos com DEFAULTS neutros/conservadores (documentados como "ajustar")
  - expõe accessors tipados que Metrics / Finance / Diagnostic / Alert consomem

Os DEFAULTS abaixo são o ÚNICO lugar com números, e são deliberadamente neutros
— não codificam o método do Thiago nem nenhuma outra metodologia. Ajuste-os por
profile (global ou por conta) sem tocar em código.

Grupos de regras:
  minimum_sample_rules  — quando o dado é suficiente pra diagnosticar (gate da Etapa 6)
  profit_targets        — margem-alvo p/ ROAS mínimo operacional (Finance, Etapa 4)
  risk_limits           — limiares de anomalia (Alert Engine, Etapa 7)
  development_rules      — NOVO -> DESENVOLVIMENTO (estágio do grupo, Etapa 6/8)
  consolidation_rules   — DESENVOLVIMENTO -> CONSOLIDADO -> ESCALA
"""
import json

from app.database import get_conn
from app.utils.logger import get_logger

log = get_logger("ads_strategy")

GLOBAL = ""  # seller_id do profile global default
DEFAULT_NAME = "default"

# ── DEFAULTS NEUTROS — ajuste por profile, não aqui ──────────────────────────
DEFAULTS = {
    "minimum_sample_rules": {
        "min_units": 10,                     # unidades vendidas reais no período
        "min_orders": 5,                     # pedidos distintos reais
        "min_days_with_prints": 7,           # dias com veiculação
        "single_order_dominance_pct": 50.0,  # 1 pedido concentrando > X% das unidades = outlier
    },
    "profit_targets": {
        "margem_alvo_pct": 10.0,             # margem depois de Ads que a operação quer manter
    },
    "risk_limits": {
        "cpc_spike_factor": 2.0,             # CPC do dia > fator × mediana recente => alerta
        "spend_pace_factor": 2.0,            # investimento do dia > fator × média recente => alerta
        "clicks_sem_venda": 15,              # N cliques sem venda no período => alerta
        "max_acos_pct": None,                # teto de ACOS (None = desligado)
        "budget_cap_share_pct": 90.0,        # % de dias batendo teto de orçamento => alerta
        "lost_by_budget_alert_pct": 20.0,    # perda de impressão por orçamento acima disso => alerta
        "ctr_drop_factor": 0.5,              # CTR recente < fator × CTR anterior => alerta
        "cvr_drop_factor": 0.5,              # idem conversão
    },
    "development_rules": {
        "min_dias_veiculando": None,         # None = transição de estágio não é sugerida automaticamente
        "min_unidades": None,
        "roas_alvo_rampup": None,            # ROAS-alvo enquanto NOVO (metodologia — preencher por profile)
    },
    "consolidation_rules": {
        "min_dias_estavel": None,
        "acos_dentro_da_meta_dias": None,
        "escala_incremento_verba": None,     # regra de escala (ex: +R$X por ponto de ROAS) — por profile
        "escala_por_ponto_roas": None,
    },
}

_GROUPS = tuple(DEFAULTS.keys())


def _parse(txt):
    try:
        v = json.loads(txt) if txt else {}
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _row(conn, seller_id):
    return conn.execute(
        "SELECT * FROM ads_strategy_profile WHERE seller_id = ? AND name = ? AND is_active = 1",
        (seller_id, DEFAULT_NAME),
    ).fetchone()


def get_strategy_profile(seller_id=None):
    """
    Profile efetivo: DEFAULTS <- profile global <- override do seller.
    Retorna {group: {...}} pra todos os grupos, sempre completo.
    Inclui `_source` por grupo: 'default' | 'global' | 'seller'.
    """
    conn = get_conn()
    g = _row(conn, GLOBAL)
    s = _row(conn, seller_id) if seller_id else None
    conn.close()

    out = {}
    for grp in _GROUPS:
        merged = dict(DEFAULTS[grp])
        source = "default"
        if g and g[grp]:
            gv = _parse(g[grp])
            if gv:
                merged.update(gv)
                source = "global"
        if s and s[grp]:
            sv = _parse(s[grp])
            if sv:
                merged.update(sv)
                source = "seller"
        merged["_source"] = source
        out[grp] = merged
    out["_seller_id"] = seller_id
    return out


def _group(seller_id, grp):
    d = dict(get_strategy_profile(seller_id)[grp])
    d.pop("_source", None)
    return d


def minimum_sample_rules(seller_id=None):
    return _group(seller_id, "minimum_sample_rules")


def profit_targets(seller_id=None):
    return _group(seller_id, "profit_targets")


def margem_alvo_pct(seller_id=None):
    return profit_targets(seller_id).get("margem_alvo_pct")


def risk_limits(seller_id=None):
    return _group(seller_id, "risk_limits")


def development_rules(seller_id=None):
    return _group(seller_id, "development_rules")


def consolidation_rules(seller_id=None):
    return _group(seller_id, "consolidation_rules")


# ── Escrita ────────────────────────────────────────────────────────────────

def save_strategy_profile(seller_id, groups, *, name=DEFAULT_NAME):
    """
    groups: dict parcial {group_name: {chave: valor}}. Faz MERGE sobre o que já
    está salvo naquele profile (não substitui o grupo inteiro).
    seller_id='' edita o profile global.
    """
    import time
    invalid = set(groups) - set(_GROUPS)
    if invalid:
        raise ValueError(f"grupos inválidos: {invalid}. Válidos: {_GROUPS}")

    conn = get_conn()
    now = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO ads_strategy_profile (seller_id, name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)", (seller_id, name, now, now),
    )
    row = conn.execute(
        "SELECT * FROM ads_strategy_profile WHERE seller_id = ? AND name = ?",
        (seller_id, name),
    ).fetchone()

    for grp, patch in groups.items():
        current = _parse(row[grp])
        current.update(patch)
        conn.execute(
            f"UPDATE ads_strategy_profile SET {grp} = ?, updated_at = ? "
            f"WHERE seller_id = ? AND name = ?",
            (json.dumps(current, ensure_ascii=False), now, seller_id, name),
        )
    conn.commit()
    conn.close()
    log.info(f"strategy_profile atualizado seller_id={seller_id!r} grupos={list(groups)}")


def seed_default_profile():
    """
    Garante que o profile global 'default' exista e tenha os DEFAULTS materializados
    em JSON (facilita edição pela UI da Etapa 8). Idempotente — só escreve grupos
    ainda vazios ('{}' ou NULL). Não sobrescreve ajuste já feito.
    """
    conn = get_conn()
    row = _row(conn, GLOBAL)
    conn.close()
    to_write = {}
    for grp in _GROUPS:
        if not row or not _parse(row[grp]):
            to_write[grp] = DEFAULTS[grp]
    if to_write:
        save_strategy_profile(GLOBAL, to_write)
        log.info(f"seed do profile global 'default': {list(to_write)}")
    return list(to_write)
