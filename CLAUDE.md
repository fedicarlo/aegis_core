# AEGIS — Contexto do Projeto

Sistema de gestão operacional multi-conta para sellers de Mercado Livre. Python/Flask/SQLite, rodando local no Mac Mini (M2/M4) do Lipe, porta 8080, ngrok pro OAuth (ML exige HTTPS no redirect URI).

**Path:** `/Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core`
**MELI CLIENT_ID:** 5322198809581181
**Contas piloto:** GREENLABS/Maximus (seller_id 2124526664), Querência (seller_id 2465189536)

## Produção e tokens — regra crítica

Além do ambiente local do Lipe, roda uma instância em **produção no Railway**
(projeto `scintillating-insight`, serviço `aegis_core`,
`https://aegiscore-production-8d7b.up.railway.app`, volume `/data`,
`DB_PATH=/data/aegis.db`). Produção tem `AUTO_SYNC_ENABLED=true` e renova os
tokens das contas no seu próprio ciclo (a cada 6h).

**NUNCA rodar coleta, `get_valid_token`, `refresh_token` ou qualquer operação
que use token de conta real a partir de cópia local do banco de produção.**
O `aegis.db` local é um espelho defasado. O ML rotaciona `refresh_token` de uso
único: qualquer refresh disparado localmente **queima o token que produção tem
guardado**, e o próximo auto-sync de produção falha com `invalid_grant` —
obrigando reautorização manual via OAuth no navegador.

Toda operação que precise de token de seller roda **dentro do container de
produção via `railway ssh`**, contra `/data/aegis.db` — nunca no ambiente local.
O ambiente local serve só para desenvolvimento de código e testes com
fixtures/dados fake.

```
railway ssh -- "cd /app && .venv/bin/python -c 'from app.services.ads_collector import collect_all_ads; collect_all_ads()'"
```

(Incidente 2026-08-31: uma coleta de Ads rodada localmente rotacionou os tokens
de Querencia, The Good Store e VIVALEVE; as três precisaram de reautorização
OAuth manual em produção.)

## Estado atual dos módulos

- OAuth multi-conta (dashboard de gestão de contas)
- Coleta de listings, inventário FULL, pedidos
- Analytics engine: giro de venda (janelas 7/15/30 dias), cobertura de estoque em dias, classificação de produto (Crítico/Oportunidade/Estável/Descarte), ciclo de maturidade (Emergindo/Crescendo/Estrela/Dominante por sold_quantity acumulado), detecção de tendência
- Sugestão de indução: `envio = giro_base × (dias_alvo + ciclo_logístico) × fator_maturidade × fator_tendência × 1.2` (ciclo logístico = 20 dias)
- Módulo de custo/margem, rentabilidade de promoções, calendário de reposição, relatórios executivos, simulador de precificação
- Módulo financeiro (Mercado Pago): **REMOVIDO do pipeline por decisão — não desenvolver nem reconectar por enquanto.** A função `get_mp_conciliation` existe no banco mas está desligada de propósito.
- **Login multiusuário de sellers** (`app/auth_policy.py`, `app/routes/seller.py`):
  separado do login de admin único. Tabelas `seller_users` / `seller_suggestions`
  / `induction_decisions`. Isolamento por **allowlist positiva** de endpoints
  (`SELLER_ALLOWED`) + trava de `seller_id` no `before_request`; `session["seller_id"]`
  só é escrito em `routes/auth.py`, nunca lido de URL/form. Seller: leitura da
  própria conta + confirmar envio FULL + aprovar/rejeitar indução (ledger) +
  sugerir status de produto (pendente de aprovação do admin em `/admin/aprovacoes`).
  Credenciais criadas manualmente pelo admin em `/admin/seller-users`. Cobertura:
  `tests/test_seller_isolation.py` varre todo o `url_map`. Pendência anotada:
  rate limit / brute-force no login do seller.

## Bugs abertos (Fase 1 do plano)

1. **Estoque FULL zerando incorretamente** em alguns itens — suspeita de paginação incompleta ou race condition na sync
2. **Contagem de pedidos divergente do painel ML** (~gap identificado anteriormente) — suspeita de falta de UPSERT consistente causando duplicata ou perda de registro na sync

MP (Mercado Pago) está fora de escopo — não incluir na investigação de bugs nem em nenhuma fase atual.

## Plano de reestruturação — 5 fases

**Fase 1 — Estancar bugs**
Resolver os dois bugs acima. Sem MP.

**Fase 2 — Fundação estrutural**
- Logging estruturado (rastrear onde/quando uma sync falha)
- Cache (evitar rebater API do ML por dado que não muda a cada request)
- UPSERT consistente em todas as tabelas de sync — provável causa raiz do gap de pedidos

**Fase 3 — Painel administrativo**
- **Cadastro de sellers via UI**: form no dashboard (seller_id, nickname, dispara fluxo OAuth) gravando em `sellers`, sem precisar editar código/`.env` a cada conta nova
- **Upload de NF-e XML**: endpoint de upload + parser do schema padrão NF-e (tags `prod`: cProd, xProd, NCM, qCom, vUnCom). Alimenta custo real de compra do produto (substitui estimativa)
- **Input manual de envios FULL** (data, conteúdo, unidades): formulário gravando em algo como `shipments_full`, cruzado contra o retorno da API ML — dado que falta hoje pro sistema saber o que foi enviado vs. o resultado final (ligado ao bug de FULL zerando)

**Fluxo NF-e → Indução → PDF (detalhamento):**
1. XML da nota alimenta o custo real de compra do produto
2. Motor de indução usa esse custo real (não estimado) pra calcular o valor aproximado do pedido de reposição
3. Ao gerar uma sugestão de indução, o sistema monta o pedido completo: SKU, quantidade sugerida (fórmula de giro/cobertura já existente), valor unitário da última NF, valor total
4. Exporta em PDF pronto pra enviar ao fornecedor — layout livre, sem template externo a seguir (dados do fornecedor, tabela SKU/qtd/valor unit/total, campo de observação)

**Fase 4 — Módulo de Ads (motor de diagnóstico, não dashboard passivo)**
Ingere ACOS/ROAS/impressão/clique/venda por campanha e aplica as regras do método aprendido com o ex-chefe Thiago (consultor certificado de Ads):
- Impressão presa sem clique, SKU sem histórico → sinaliza "campanha em aquecimento, ROAS meta agressiva demais" (trava por classificação)
- ACOS subindo + queda simultânea de impressão/clique/venda → sinaliza "produto deteriorando, revisar SEO/Ad Rank"
- SKU maduro estabilizado dentro da meta → sugere migração pra campanha principal, aplicando a regra de +R$1.500 de verba por ponto de ROAS ganho
- Objetivo: automatizar o diagnóstico semanal que hoje é feito manualmente (ver /areas/mercado-ads-strategy.md para a metodologia completa)

**Fase 5 — Módulo de criação de anúncios (fica pra depois, com atenção dedicada)**
O diferencial do sistema. Não é CRUD — precisa de IA de verdade:
- Mapeamento de volume de busca e clustering de nicho
- Geração de dados prontos pra preencher ficha técnica (SEO calibrado)
- Sugestão de estrutura de imagem + prompt pronto pra IA gerar
- Merece arquitetura própria e planejamento dedicado quando chegar a vez — não misturar com as fases anteriores

## Metodologia de Ads (referência para Fase 4)

Ver contexto completo do método Thiago em conversas anteriores — resumo: verba alta + ACOS baixo ("direcionar o canhão") converte mais rápido que verba baixa + ACOS alto ("abrir o canhão"), desde que o anúncio já tenha SEO calibrado. Regra de escala: +R$1.500 de verba por +1 ponto de ROAS. SKU sem histórico entra em campanha de ramp-up (ROAS ~4x) até a primeira venda, depois migra pra campanha principal.
