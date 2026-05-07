# AEGIS — Tutorial Operacional
**Guia prático para operação do sistema**

---

## INICIAR O SISTEMA

```bash
# 1. Abrir terminal e ir para o projeto
cd /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core

# 2. Ativar ambiente virtual
source venv/bin/activate

# 3. Em outro terminal, subir o ngrok (necessário para OAuth)
ngrok http 8080

# 4. Subir o servidor
python run.py

# 5. Acessar no browser
http://127.0.0.1:8080
```

> ⚠️ Se a porta 8080 estiver ocupada:
> ```bash
> lsof -ti:8080 | xargs kill -9
> python run.py
> ```

---

## ADICIONAR UM NOVO SELLER

### Opção A — Via interface (recomendado)
1. Acessar `http://127.0.0.1:8080`
2. Clicar **"Adicionar Seller"**
3. Digitar o nome e confirmar

### Opção B — Via config.py (manual)
```bash
nano /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core/app/config.py
```
Adicionar o nome na lista `ACCOUNTS`:
```python
ACCOUNTS = [
    "Maximus",
    "Novo Seller",  # ← adicionar aqui
    ...
]
```
Salvar e reiniciar o servidor.

---

## AUTORIZAR UM SELLER

> ⚠️ Pré-requisito: ngrok deve estar rodando e a URL deve estar atualizada no `.env` e no painel MELI Developer.

1. Abrir browser **já logado na conta do seller** (conta correta no MELI)
2. Acessar `http://127.0.0.1:8080`
3. Clicar **Autorizar →** no card do seller
4. Na tela do MELI, clicar **Permitir** imediatamente
5. Aguardar redirect automático — card ficará verde com **● Ativo**

**Verificar se autorizou a conta certa:**
```bash
python -c "
from app.main import create_app
from app.database import get_account_by_name
app = create_app()
with app.app_context():
    a = get_account_by_name('NomeDoConta')
    print('seller_id:', a['seller_id'])
    print('nickname:', a['nickname'])
"
```

**Se der erro 400 (code expirado):** o processo demorou demais. Revogar e autorizar de novo mais rápido.

**Se der "aplicativo não está pronto":** verificar se o ngrok está rodando e se a URL está atualizada.

---

## ENVIAR LINK DE AUTORIZAÇÃO PARA O SELLER

> Usar apenas quando o seller não estiver na mesma rede.
> Requer servidor em produção (Railway) para funcionar externamente.

1. No painel, clicar **🔗 Gerar Link** no card do seller
2. Usar o **botão "Conectar ao Mercado Livre"** — NÃO copiar o link manualmente (causa problema com `&amp;`)
3. Enviar a URL da página para o seller
4. Seller acessa, loga na conta dele e clica Conectar

---

## ATUALIZAR DADOS DE UMA CONTA

1. Acessar `http://127.0.0.1:8080/dashboard?account=NomeDoConta`
2. Clicar **⟳ Atualizar** no menu superior
3. Aguardar — o sistema coleta anúncios, estoque, pedidos e promoções

**Via terminal (mais rápido para debug):**
```bash
python -c "
from app.main import create_app
from app.services.collector import collect_account
from app.database import get_account_by_name
app = create_app()
with app.app_context():
    account = get_account_by_name('Maximus')
    result = collect_account(account)
    print(result)
"
```

---

## CADASTRAR CUSTOS DE PRODUTOS

### Via planilha (recomendado para cadastro inicial)
1. Acessar `/custos/{seller_id}`
2. Clicar **Baixar Planilha Modelo**
3. Preencher colunas `custo_nf` e `aliquota_percent`
4. Fazer upload da planilha preenchida
5. Verificar relatório de erros se houver

### Via interface manual (para ajustes pontuais)
1. Acessar `/custos/{seller_id}`
2. Editar o custo diretamente na linha do produto
3. Clicar **Salvar Tudo** para salvar todas as alterações de uma vez

### Alíquota
- A alíquota fica **congelada** até ser alterada manualmente
- Alterar uma vez e aplicar para todos os produtos do seller
- Representa a alíquota efetiva de imposto (ex: 6%, 8%, 13.33%)

---

## REVOGAR AUTORIZAÇÃO DE UM SELLER

1. No painel `http://127.0.0.1:8080`, clicar **Revogar** no card do seller
2. Confirmar a ação

**Via SQLite (emergência):**
```bash
sqlite3 /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core/aegis.db \
"UPDATE accounts SET seller_id=NULL, nickname=NULL, access_token=NULL, refresh_token=NULL, expires_at=NULL, authorized_at=NULL WHERE name='NomeDoConta';"
```

---

## QUANDO O NGROK MUDAR DE URL

Isso acontece toda vez que o ngrok é reiniciado no plano gratuito.

1. Ver a nova URL no terminal do ngrok:
   ```
   Forwarding → https://NOVA-URL.ngrok-free.dev
   ```

2. Atualizar o `.env`:
   ```bash
   nano /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core/.env
   ```
   Trocar `MELI_REDIRECT_URI=https://URL-ANTIGA.ngrok-free.dev/callback`
   por `MELI_REDIRECT_URI=https://NOVA-URL.ngrok-free.dev/callback`

3. Atualizar no painel MELI Developer:
   - Acessar `https://developers.mercadolivre.com.br/devcenter`
   - Entrar no app
   - Campo **URIs de redirect** → trocar pela nova URL com `/callback`
   - Salvar

4. Reiniciar o servidor Flask

> 💡 Para evitar esse problema permanentemente: fazer deploy no Railway — URL fixa para sempre.

---

## VERIFICAR SAÚDE DO SISTEMA

```bash
# Verificar contas autorizadas
python -c "
from app.main import create_app
from app.database import get_all_accounts
app = create_app()
with app.app_context():
    accounts = get_all_accounts()
    for a in accounts:
        status = '✅' if a['access_token'] else '⏳'
        print(f'{status} {a[\"name\"]:20} | {a[\"seller_id\"] or \"pendente\"}')"

# Verificar banco de dados
sqlite3 aegis.db "SELECT name, COUNT(*) FROM sqlite_master WHERE type='table' GROUP BY name;"

# Contar registros por tabela
sqlite3 aegis.db "
SELECT 'items' as tabela, COUNT(*) as registros FROM items
UNION ALL SELECT 'orders', COUNT(*) FROM orders
UNION ALL SELECT 'stock_full', COUNT(*) FROM stock_full
UNION ALL SELECT 'mp_payments', COUNT(*) FROM mp_payments;"
```

---

## DEPLOY EM PRODUÇÃO (Railway)

Para que sellers externos possam autorizar sem ngrok:

```bash
# 1. Instalar Railway CLI
npm install -g @railway/cli

# 2. Login
railway login

# 3. Na pasta do projeto
cd /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core
railway init

# 4. Deploy
railway up

# 5. Pegar a URL pública gerada
railway domain
```

Após o deploy:
- Atualizar `MELI_REDIRECT_URI` nas variáveis de ambiente do Railway
- Atualizar no painel MELI Developer
- URL será permanente — nunca mais precisa trocar

---

## CONTINUAR DESENVOLVIMENTO COM CLAUDE

### No Claude Code (terminal)
```bash
cd /Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core
claude
```

### No chat (claude.ai)
Ao iniciar nova conversa, colar o conteúdo do arquivo `AEGIS_CONTEXTO.md` no início da mensagem para o Claude ter contexto completo.

### Retomar sessão do Claude Code
```bash
claude --resume ID_DA_SESSAO
```
O ID aparece no terminal quando você sai: `Resume this session with: claude --resume XXXX`

---

## ROTAS DISPONÍVEIS

| Rota | Descrição |
|------|-----------|
| `/` | Painel de contas |
| `/dashboard?account=X` | Cockpit do seller |
| `/authorize/X` | Iniciar OAuth |
| `/callback` | Receber código OAuth |
| `/exchange` | Trocar code manualmente |
| `/link/X` | Gerar link para seller |
| `/collect/X` | Coletar dados da conta |
| `/custos/{seller_id}` | Cadastro de custos |
| `/custos/export/{seller_id}` | Baixar planilha modelo |
| `/custos/import/{seller_id}` | Upload de planilha |
| `/apuracao/{seller_id}` | Apuração financeira |
| `/promocoes/{seller_id}` | Rentabilidade de promoções |
| `/calendario/{seller_id}` | Calendário de reposição |
| `/produto/{seller_id}/{item_id}` | Detalhe do produto |
| `/relatorio/{seller_id}` | Relatório executivo |
| `/financeiro/{seller_id}` | Módulo Mercado Pago |
| `/simulador/{seller_id}` | Simulador de precificação |

---

## SOLUÇÃO DE PROBLEMAS COMUNS

| Problema | Causa | Solução |
|----------|-------|---------|
| Porta 8080 ocupada | Processo anterior não encerrou | `lsof -ti:8080 \| xargs kill -9` |
| Erro 400 no OAuth | Code expirado ou redirect_uri errado | Verificar `.env` e tentar mais rápido |
| "App não está pronto" | ngrok mudou URL | Atualizar `.env` e MELI Developer |
| Estoque FULL zerado | inventory_id não mapeado | Rodar `python -m scripts.diagnose_maximus` |
| Seller errado autenticado | Estava logado em outra conta | Revogar e autorizar no browser correto |
| `&amp;` no link | Bug HTML no link copiado | Usar o botão, não copiar o link |
| Sessão Claude Code perdida | Saiu acidentalmente | `claude --resume ID_SESSAO` |
