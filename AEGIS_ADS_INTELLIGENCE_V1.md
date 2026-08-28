# AEGIS ADS INTELLIGENCE — Especificação V1

Baseado em auditoria real contra a API do Mercado Livre (Product Ads, fluxo pós-migração para Ad Groups, confirmado em 27/08/2026 contra conta Maximus).

---

## 1. Requisitos Funcionais

O módulo deve responder, para qualquer campanha/anúncio/período:

1. **O que está aconteceu?** — funil completo (impressão → clique → venda → receita → lucro), com série diária.
2. **Onde está o gargalo?** — classificação determinística: exposição, CTR, conversão, ou econômico.
3. **A campanha está economicamente saudável?** — ROAS real vs. ROAS de equilíbrio vs. ROAS mínimo operacional, cruzado com custo/margem real do SKU.
4. **Qual a próxima ação, e por quê?** — recomendação explicável (fatos + hipótese + confiança), nunca uma métrica solta.

V1 é **read-only + recomendação**. Nenhuma ação é executada automaticamente na API do ML.

---

## 2. Requisitos Técnicos

- Unidade de análise primária: **ad_group_id**, não item_id (a API do ML migrou a granularidade pra Ad Groups em 2025-2026; um ad_group pode agrupar múltiplos SKUs em catálogo/família).
- Tabela-ponte `ad_group_items` (1 ad_group → N item_id) alimentada por `/ad_groups/{id}/ads`.
- Snapshot diário de `acos_target`/`roas_target`/`budget`/`status` — a API não versiona histórico de meta, só entrega o valor atual. Sem snapshot, não há "linha do tempo de alterações" (requisito 9 do documento original).
- Coleta 1×/dia, após 13h UTC (a API atualiza métricas do dia anterior por volta das 10h GMT-3 = 13h UTC).
- Paginação de `ad_groups/metrics` em lotes via `filters[ad_group_ids]` (Maximus tem ~180 ad groups — não cabe em uma chamada só).
- Rate limit: a confirmar empiricamente durante a implementação da V1 (não testado em volume ainda).

---

## 3. Fontes de Dados / API — Fluxo confirmado

```
Token válido da conta
    ↓
GET /advertising/advertisers?product_id=PADS
    → advertiser_id + site_id
    ↓
GET /advertising/{site_id}/advertisers/{advertiser_id}/product_ads/campaigns/search
    → campanhas + métricas sumarizadas do período
    ↓
GET /advertising/{site_id}/product_ads/campaigns/{campaign_id}?aggregation_type=DAILY
    → histórico diário (máx. 90 dias, 1 aggregation_type por chamada)
    ↓
GET /advertising/{site_id}/advertisers/{advertiser_id}/product_ads/ad_groups/search
    → ad groups por campanha ou por item_id (filters[item_ids]=...)
    ↓
GET /advertising/{site_id}/product_ads/campaigns/{campaign_id}/ad_groups/metrics
    → métricas por ad_group (granularidade mais próxima de SKU disponível)
```

Rota legada (`/advertising/advertisers/{id}/product_ads/campaigns`, sem `{site_id}` e sem `/search`) foi **descontinuada em fevereiro/2026** — retorna 404 de corpo vazio, não erro descritivo. Causa raiz do bloqueio original da auditoria.

---

## 4. Classificação de Origem do Dado

### SOURCE_API — direto da API, campo por campo

**Advertiser:** advertiser_id, site_id, advertiser_name

**Campanha:** id, name, status, budget, currency_id, strategy, roas_target, channel, date_created, last_updated

**Métricas de campanha (summary + diário):** clicks, prints, ctr, cost, cpc, acos, cvr, roas, sov, direct_units_quantity, indirect_units_quantity, units_quantity, direct_amount, indirect_amount, total_amount

**Só no detalhe de campanha:** impression_share, top_impression_share, lost_impression_share_by_budget, lost_impression_share_by_ad_rank, acos_benchmark

**Ad Group (métricas):** clicks, prints, cost, cpc, ctr, direct_amount, indirect_amount, total_amount, direct_units_quantity, indirect_units_quantity, units_quantity, acos, sov, roas, cvr, tacos

**Pedidos (já coletado):** unidades, nº de pedidos, receita, data — falta `buyer_id` (ver item 8).

### SOURCE_CALCULATED — derivado pelo AEGIS

- Total do período por ad_group (API não entrega somado, só diário)
- Receita orgânica (não vem pronta — calcular via `orders`, subtraindo o atribuído a Ads)
- Vendas/unidades por SKU dentro de grupo CATALOG/FAMILY (rateado via `orders` reais por item_id, já que a API não reparte isso por SKU)
- TACOS/ACOS blended: `cost ÷ receita total (incl. orgânica dos orders)`
- Amostra insuficiente: `units_quantity × direct_items_quantity × compradores únicos × dias-com-dado`
- Sinal de "aquecimento": prints alto + clicks/units ≈ 0 + ad group novo
- Sinal de "deterioração": ACOS↑ + prints/clicks/units↓ simultâneo na série diária
- Gargalo de exposição: `lost_impression_share_by_budget` vs. `_by_ad_rank` vs. `acos` vs. `acos_benchmark`
- Sugestão de migração para campanha principal + regra "+R$1.500 por ponto de ROAS" (regra de negócio sobre métrica de API)
- Histórico de metas/orçamento — só existe se o AEGIS snapshotar a cada coleta (API não versiona)

### SOURCE_MANUAL — não existe na API, input desde a V1

- Palavras-chave / termos de busca / negativação (Product Ads do ML não tem API de keyword)
- Breakdown de posicionamento além do hint `top_impression_share`/`acos_top_search_target`
- Parâmetros de julgamento do método (Strategy Profile): ROAS-alvo de ramp-up, dias_alvo, qual campanha é "a principal", se o anúncio tem SEO calibrado (sim/não)
- Brand Ads / Display (produto separado, não habilitado nas contas atuais)
- Repartição fina de impressão/clique/investimento por SKU dentro de grupo CATALOG/FAMILY (ML não expõe)

---

## 5. Modelo de Dados (desenho inicial)

```
advertisers
  id, seller_id, advertiser_id, site_id, advertiser_name

campaigns
  id, advertiser_id, campaign_id_ml, name, status, budget,
  currency_id, strategy, channel, date_created_ml

campaign_target_snapshots        -- histórico de meta (SOURCE_CALCULATED)
  id, campaign_id, acos_target, roas_target, budget, status, snapshot_at

campaign_metrics_daily
  id, campaign_id, date, clicks, prints, ctr, cost, cpc, acos, cvr,
  roas, sov, direct_units_quantity, indirect_units_quantity,
  units_quantity, direct_amount, indirect_amount, total_amount

campaign_metrics_detail          -- só no nível "detalhe de campanha"
  id, campaign_id, date, impression_share, top_impression_share,
  lost_impression_share_by_budget, lost_impression_share_by_ad_rank,
  acos_benchmark

ad_groups
  id, campaign_id, ad_group_id_ml, ad_group_type, ad_group_external_id

ad_group_items                   -- tabela-ponte, 1 ad_group → N item_id
  id, ad_group_id, item_id, seller_id

ad_group_metrics_daily
  id, ad_group_id, date, clicks, prints, cost, cpc, ctr,
  direct_amount, indirect_amount, total_amount,
  direct_units_quantity, indirect_units_quantity, units_quantity,
  acos, sov, roas, cvr, tacos

ads_strategy_profile              -- camada configurável, NÃO hardcode metodologia
  id, seller_id (ou global), name, development_rules (JSON),
  consolidation_rules (JSON), risk_limits (JSON),
  profit_targets (JSON), minimum_sample_rules (JSON)

ads_manual_inputs                 -- SOURCE_MANUAL
  id, campaign_id ou ad_group_id, field_name, value, set_by, set_at

ads_events                        -- linha do tempo de alterações (req. 9)
  id, campaign_id ou ad_group_id, changed_at, field, old_value,
  new_value, author, motivo, hipotese

ads_experiments                   -- sistema de experimentos (req. 10)
  id, campaign_id ou ad_group_id, hipotese, intervencao,
  janela_inicio, janela_fim, resultado, conclusao, created_at

ads_alerts
  id, campaign_id ou ad_group_id, tipo, severidade, evidencia (JSON),
  acao_sugerida, created_at, resolved_at
```

`orders` (já existente) ganha `buyer_id` — necessário pro cálculo de compradores únicos na régua de amostra insuficiente.

---

## 6. Cálculos

**Funil:** Impressões → CTR → Cliques → Taxa de conversão → Vendas atribuídas → Receita → Lucro

**Lucro antes de Ads (por SKU):**
`Preço − custo da mercadoria − comissão ML − tarifa/frete − imposto − promoção = lucro antes Ads`
(reaproveita `product_costs` e o pipeline de custo já existente no AEGIS)

**Lucro depois de Ads:** `lucro antes Ads − custo de Ads atribuído por venda`

**ACOS de equilíbrio:** % de faturamento que o Ads pode consumir até lucro = 0

**ROAS de equilíbrio:** ROAS correspondente a esse limite (não é meta, é o piso absoluto)

**ROAS mínimo operacional:** dado uma margem-alvo (ex: 10%, podendo ser da operação consolidada, não por SKU), quanto de Ads o produto/campanha suporta mantendo essa margem — suporta análise em 3 níveis: SKU → campanha → conta.

**TACOS/ACOS blended:** `investimento total ÷ (receita atribuída Ads + receita orgânica via orders)`

---

## 7. Regras de Diagnóstico (motor determinístico, V1)

| Caso | Sinal | Gargalo provável | Ação |
|---|---|---|---|
| A — Exposição | Poucas impressões + perda alta por classificação | Competitividade/exposição | Investigar ROAS objetivo, relevância, qualidade (nunca concluir "é ROAS" automaticamente — é hipótese, não fato) |
| B — CTR | Muitas impressões + CTR muito baixo | Impressão → clique | Investigar capa, preço, promoção, título, prova social |
| C — Conversão | CTR saudável + muitos cliques + poucas/zero vendas | Clique → venda | Investigar página, oferta, preço, logística |
| D — Econômico | Vendas + ROAS aparentemente bom + lucro abaixo da meta | Comercialmente eficiente, economicamente inadequado | Revisar ROAS-alvo à luz da margem real |
| E — Amostra insuficiente | 2-3 cliques e zero vendas | — | NÃO diagnosticar ainda. Aguardar volume mínimo. |

**Regra de arquitetura:** todo output do motor distingue **FATO** ("95% das oportunidades perdidas por classificação") de **HIPÓTESE** ("ROAS objetivo pode estar restringindo competitividade") — nunca apresentar hipótese como conclusão.

**Regra de amostra:** o sistema nunca conclui "consolidado" ou "convertendo bem" olhando só `units_quantity`. Sempre cruza com nº de pedidos, compradores únicos, e outliers de quantidade (ex: 16 unidades de 1 pedido só ≠ 16 decisões de compra independentes).

---

## 8. Estados e Fluxos

**Estágio do produto/ad_group (interno do AEGIS, não existe no ML):**
`NOVO → DESENVOLVIMENTO → CONSOLIDADO → ESCALA`, com desvio possível para `RECUPERAÇÃO`.

Migração de estágio nunca é automática por volume de venda isolado — passa pelo motor de diagnóstico (amostra suficiente + sinais consistentes na série, não só um pico).

**Classificação de campanha (interna do AEGIS):**
`Desenvolvimento | Consolidação | Escala | Recuperação | Teste`

**Status geral do cockpit:**
`SAUDÁVEL | ATENÇÃO | CRÍTICO | APRENDIZADO/DADOS INSUFICIENTES` — nunca calculado a partir de uma métrica isolada.

---

## 9. Telas / Componentes

1. **Cockpit** (visão geral): investimento hoje/7d/30d, receita atribuída, receita total, vendas atribuídas vs. sem Ads, impressões, cliques, CTR, CPC, ROAS objetivo vs. realizado, ACOS, TACOS, lucro antes/depois de Ads, margem depois de Ads, status geral.

2. **Lista de Campanhas**: tabela com todos os campos do item 3 do documento original + classificação interna (Desenvolvimento/Consolidação/etc).

3. **Detalhe de Campanha**: funil visual, impression share breakdown (ganho/perdido por orçamento/perdido por classificação), série diária, linha do tempo de eventos.

4. **Análise Individual por SKU/Ad Group**: funil completo + camada financeira (lucro antes/depois, ROAS de equilíbrio, ROAS mínimo operacional) + estágio do produto.

5. **Experimentos**: lista de hipótese → intervenção → janela → resultado → conclusão, com comparação Before/After.

6. **Configuração de Strategy Profile**: onde o usuário edita `development_rules`, `consolidation_rules`, `risk_limits`, `profit_targets`, `minimum_sample_rules` sem tocar código.

7. **Filtros de tempo**: Hoje, Ontem, 7d, 14d, 30d, intervalo customizado, desde criação da campanha, **desde última alteração** (crítico — evita misturar resultado pré/pós intervenção).

---

## 10. Alertas (V1)

Cada alerta mostra: **o que aconteceu + evidência + severidade + ação sugerida.**

Lista inicial: CPC disparou, investimento acelerou acima do histórico, N cliques sem venda, ROAS abaixo do break-even, ACOS acima do limite, campanha batendo teto de orçamento, perda por orçamento aumentou, CTR despencou, conversão caiu, produto antes consolidado parou de vender, venda excepcional distorcendo ROAS (outlier).

---

## Roadmap

**V1 (esta especificação):** Data Provider + Metrics Engine + Finance Engine + Diagnostic Engine (regras determinísticas) + Strategy Engine (profiles configuráveis) + Alert Engine + camada de eventos/experimentos (registro manual) + UI read-only. **Nenhuma escrita na API do ML.**

**V2:** Aprovação humana → execução via API (mudar ROAS-alvo, orçamento, pausar campanha, mover SKU — mediante confirmação explícita do usuário).

**V3:** Automação supervisionada (sistema aplica mudanças de baixo risco sozinho, dentro de limites pré-aprovados; V1+V2 precisam ter gerado histórico suficiente pra validar confiança do motor antes disso).

---

## Arquitetura (camadas, sem lógica no frontend)

```
Ads Data Provider     → coleta ML/API/manual, grava SOURCE_* por campo
Ads Metrics Engine    → CTR, CPC, ROAS, ACOS, TACOS, CVR
Ads Finance Engine    → custo, margem, break-even, lucro (integra com product_costs)
Ads Diagnostic Engine → motor de regras determinístico (casos A-E)
Ads Strategy Engine   → aplica ads_strategy_profile (nunca regra hardcoded)
Ads Experiment Engine → hipótese → intervenção → resultado → conclusão
Ads Alert Engine      → detecção de anomalia
Ads Recommendation Engine → próxima ação + motivos + confiança
Ads UI                → cockpit, campanhas, SKU/ad group, experimentos
```
