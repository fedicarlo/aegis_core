# AEGIS — Documento de Contexto Completo
**Gerado em: 15/04/2026**

---

## 1. SOBRE O NEGÓCIO

O operador é consultor oficial Mercado Livre especializado em sellers de suplementos. Gerencia contas de sellers e indústrias fabricantes. É classificado como um dos melhores consultores do Brasil em Mercado Livre.

**Serviços prestados:** anúncios, imagens, copy, tráfego, gestão estratégica de contas.

**Modelo de gestão:** baseado em indução de estoque, performance de algoritmo e interpretação de comportamento da plataforma.

---

## 2. O QUE É O AEGIS

Sistema de gestão operacional estratégica de sellers no Mercado Livre. Não é um painel de anúncios — é um motor de decisão que replica o raciocínio estratégico do operador em escala.

**Objetivo:** transformar gestão manual e reativa em sistema automatizado, preditivo e escalável.

**Stack:** Python 3 / Flask / SQLite / Requests / OpenPyXL

**Diretório:** `/Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core`

**Rodar:** `source venv/bin/activate && python run.py` (porta 8080)

---

## 3. SELLERS GERENCIADOS (16 contas)

| Nome | Seller ID | Nickname | Status |
|------|-----------|----------|--------|
| Maximus | 2124526664 | GREENLABS | ✅ Ativo |
| Querencia | 2465189536 | — | ✅ Ativo |
| Amigão Suplementos | — | — | Pendente |
| Smash | — | — | Pendente |
| Member XXX | — | — | Pendente |
| Foco Fit | — | — | Pendente |
| Profit | — | — | Pendente |
| Ocean Drop | — | — | Pendente |
| Renova Be | — | — | Pendente |
| Iron Meal | — | — | Pendente |
| Max Fem | — | — | Pendente |
| My Whey | — | — | Pendente |
| Gloryful | — | — | Pendente |
| The Good Store | — | — | Pendente |
| Under Labz | — | — | Pendente |
| Strongest | — | — | Pendente |

**Conta piloto para testes:** Maximus / GREENLABS / seller_id: 2124526664

---

## 4. CREDENCIAIS E CONFIGURAÇÃO

**App MELI:**
- CLIENT_ID: 5322198809581181
- Arquivo `.env` na raiz do projeto

**OAuth:**
- O MELI exige HTTPS — usar ngrok para desenvolvimento local
- Comando ngrok: `ngrok http 8080`
- URL ngrok atual: `https://unaging-sawyer-lightfootedly.ngrok-free.dev` (pode mudar ao reiniciar)
- REDIRECT_URI deve terminar em `/callback`
- Atualizar em dois lugares quando ngrok mudar: `.env` e painel MELI Developer
- PKCE está DESATIVADO no app (necessário para o fluxo funcionar)

**Problema conhecido:** `&amp;` no link copiado — usar o botão "Conectar ao Mercado Livre" que usa `&` simples.

---

## 5. ARQUITETURA DO PROJETO

```
aegis_core/
├── run.py                          # Entrypoint — python run.py
├── aegis.db                        # Banco SQLite
├── .env                            # Credenciais
├── app/
│   ├── main.py                     # create_app() — registra blueprints
│   ├── config.py                   # Variáveis, lista de sellers
│   ├── database.py                 # Todas as funções de banco
│   ├── routes/
│   │   ├── auth.py                 # OAuth: /authorize, /callback
│   │   ├── web.py                  # Painel principal, coleta, exchange
│   │   └── promotions.py          # Módulo promoções
│   ├── services/
│   │   ├── meli_auth.py            # OAuth, tokens, refresh automático
│   │   ├── meli_api.py             # Todas as chamadas à API MELI/MP
│   │   ├── collector.py            # Motor de coleta multi-conta
│   │   └── analytics.py           # Motor de decisão e classificação
│   ├── templates/
│   │   ├── index.html              # Painel de contas
│   │   ├── dashboard.html         # Cockpit principal
│   │   ├── exchange.html          # Troca manual de code
│   │   ├── link.html              # Página de autorização
│   │   ├── custos.html            # Cadastro de custos
│   │   ├── apuracao.html          # Apuração financeira
│   │   ├── promocoes.html         # Rentabilidade de promoções
│   │   ├── calendario.html        # Calendário de reposição
│   │   ├── produto.html           # Drill-down por produto
│   │   ├── relatorio.html         # Relatório executivo
│   │   ├── financeiro.html        # Módulo Mercado Pago
│   │   └── simulador.html         # Simulador de precificação
│   └── models/                    # (estrutura reservada)
└── scripts/
    └── diagnose_maximus.py        # Script de diagnóstico
```

---

## 6. BANCO DE DADOS (tabelas)

| Tabela | Descrição |
|--------|-----------|
| accounts | Sellers com tokens OAuth |
| items | Anúncios coletados |
| stock_full | Estoque FULL por item |
| stock_history | Histórico diário de estoque |
| orders | Pedidos |
| promotions | Promoções ativas do ML |
| product_costs | Custo NF + alíquota por produto |
| cost_history | Histórico de alterações de custo |
| mp_payments | Pagamentos Mercado Pago |
| rupture_impact | Impacto financeiro de rupturas |

---

## 7. MÓDULOS IMPLEMENTADOS

### 7.1 Fase 1 — Autenticação OAuth
- Painel de contas com status
- Fluxo OAuth com ngrok
- Refresh automático de token
- Rota `/exchange` para troca manual de code
- Rota `/link/{conta}` para enviar ao seller

### 7.2 Fase 2 — Coleta de Dados
- Anúncios com paginação
- Estoque FULL via `inventory_id`
- Pedidos históricos
- Promoções ativas
- Coleta por conta individual ou todas autorizadas

### 7.3 Motor de Analytics (`analytics.py`)
**Métricas por item:**
- giro_7d / giro_15d / giro_30d (unidades/dia)
- giro_potencial (melhor período 90 dias)
- tendência: acelerando / estável / desacelerando / esfriando / neutro
- coverage_days (estoque / giro_7d)
- classification: Crítico / Oportunidade / Estável / Descarte
- reason (motivo textual)
- alerts (chips acionáveis)

**Ciclo de maturidade:**
- 🌱 Emergindo (0-50 vendas acumuladas) — fator 0.5
- 📈 Crescendo (51-200) — fator 0.75
- ⭐ Estrela (201-500) — fator 1.0
- 👑 Dominante (500+) — fator 1.5

**Motor de risco:**
- Score 0-100
- Data estimada de ruptura
- Níveis: ATENÇÃO / URGENTE / CRÍTICO / RUPTURA

**Calculadora de indução:**
```
envio = giro_base × (dias_alvo + ciclo_logístico) × fator_maturidade × fator_tendência × 1.2
ciclo_logístico = 20 dias
dias_alvo = 30 dias
```

### 7.4 Dashboard — Cockpit Principal (`/dashboard?account=X`)
- Painel de alertas (catálogo, promoções, pausados, ruptura)
- KPIs: receita, unidades, pedidos, ticket médio com comparativo
- KPIs de margem: média, negativa, abaixo de 10%, prejuízo acumulado
- Gráfico de vendas diárias 30 dias
- Top produtos por giro
- Tabela com filtro 7d/30d, busca, classificação, maturidade, indução

### 7.5 Módulo de Custos (`/custos/{seller_id}`)
- Cadastro de custo NF por produto
- Custo por variação (opcional)
- Alíquota efetiva congelada por seller
- Margem calculada em tempo real
- Download de planilha template (.xlsx)
- Upload de planilha preenchida
- Filtro por marca
- Histórico de alterações de custo
- Badge de produtos sem custo

### 7.6 Módulo de Promoções (`/promocoes/{seller_id}`)
- Lista de promoções ativas criadas pelo ML
- Comparativo: margem original vs margem com promoção
- Alerta de margem negativa em promoção
- Integrado no painel de alertas do dashboard

### 7.7 Calendário de Reposição (`/calendario/{seller_id}`)
- Produtos agrupados por urgência: HOJE / 5d / 10d / 20d / 30d
- Score de risco, estoque, giro, envio sugerido, prazo máximo

### 7.8 Página de Produto (`/produto/{seller_id}/{item_id}`)
- Cabeçalho com badges
- Linha do tempo de vendas 90 dias
- Métricas de performance
- Estoque e calculadora de reposição
- Financeiro por produto
- Análise de concorrentes (em ajuste)

### 7.9 Módulo Financeiro MP (`/financeiro/{seller_id}`)
- Saldo disponível e a liberar
- Receita bruta / tarifas / líquido
- Gráfico de recebimentos diários
- Conciliação ML × MP
- Previsão de recebimentos
- **Atenção:** valores ainda em calibração — receita pode estar superestimada

### 7.10 Outros módulos
- Apuração por período (`/apuracao/{seller_id}`)
- Relatório executivo (`/relatorio/{seller_id}`)
- Simulador de precificação (`/simulador/{seller_id}`)

---

## 8. PENDÊNCIAS EM ABERTO

### Bugs conhecidos
1. **Estoque FULL zerado** para alguns itens — endpoint retorna 0 mesmo com estoque real. Investigar por `inventory_id` das variações.
2. **Receita financeira superestimada** — MP `/v1/payments/search` trazendo valores ~4.5x acima do painel ML. Filtrar por `marketplace=mercadolibre` e agrupar por `order_id`.
3. **Saldo MP indisponível** — endpoints de saldo retornam 404/403 com token OAuth do ML.
4. **Análise de concorrentes** retornando vazio — query muito específica, precisa extração de palavras-chave.
5. **Comparativo % absurdo** nos KPIs — período anterior com poucos dados no banco, normaliza com tempo.

### Funcionalidades pendentes
1. Melhorias estruturais do relatório técnico (10 pontos) — finance_pipeline, finance_insights, logging, cache, UPSERT
2. CLAUDE.md na raiz do projeto
3. Adicionar sellers via interface (sem editar config.py)
4. Botão "Salvar tudo" na página de custos
5. Monitor em tempo real (a cada 30 min) — catálogo, promoção, pausa
6. Histórico de estoque diário (snapshot automático)
7. Impacto financeiro de rupturas
8. Deploy em produção (Railway recomendado) para OAuth sem ngrok

---

## 9. FLUXO DE AUTORIZAÇÃO DE NOVA CONTA

1. ngrok deve estar rodando: `ngrok http 8080`
2. URL do ngrok e `.env` devem estar sincronizados
3. Abrir browser **já logado na conta do seller**
4. Acessar `http://127.0.0.1:8080`
5. Clicar **Autorizar** na conta desejada
6. MELI abre → clicar **Permitir** imediatamente (code expira em minutos)
7. Callback chega automaticamente → token salvo
8. Verificar seller_id no card (deve ser o correto)

---

## 10. ENDPOINTS MELI UTILIZADOS

| Endpoint | Uso |
|----------|-----|
| `GET /users/me` | Info da conta |
| `GET /users/{id}/items/search` | IDs dos anúncios |
| `GET /items?ids=` | Detalhes em batch |
| `GET /inventories/{id}/stock/fulfillment` | Estoque FULL |
| `GET /orders/search` | Pedidos |
| `GET /seller-promotions/users/{id}/promotions` | Promoções |
| `GET /sites/MLB/search` | Busca de produtos/concorrentes |
| `POST /oauth/token` | Troca code por token |
| `GET /v1/payments/search` | Pagamentos MP |
| `GET /v1/users/{id}/mercadopago_account/balance` | Saldo MP (404 com token ML) |
