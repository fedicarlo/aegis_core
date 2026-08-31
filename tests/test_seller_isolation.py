"""
Isolamento total entre sellers (Parte 6 do desenho do login multiusuário).

Varre TODO o url_map com uma sessão de seller e verifica:
  1. allowlist positiva  — endpoint fora de SELLER_ALLOWED nunca devolve 200
  2. trava de seller_id   — rota /<coisa>/<seller_id> da própria conta != 403;
                            de outra conta == 403; seller_id inexistente == 403
  3. seller_id forjado    — form/query/body com seller_id de outra conta é ignorado;
                            o servidor grava sempre com session["seller_id"]
  4. suggested_qty         — a indução é recalculada no servidor, não vem do form
  5. swap de cookie        — a sessão é auto-contida; trocar o cookie troca a
                            identidade por inteiro (sem resíduo)
  6. admin sem regressão   — sessão de admin continua alcançando tudo
  7. grep estático         — session["seller_id"] só é atribuído em routes/auth.py
"""
import re
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]

# Espelha main.PUBLIC_ENDPOINTS — endpoints que o before_request libera antes da policy.
PUBLIC_ENDPOINTS = {"auth.authorize", "auth.callback", "auth.login", "static"}

SELLER_A = "111"   # conta do seller logado nos testes
SELLER_B = "222"   # conta alheia
BOGUS    = "999999"

# Valores de argumento de rota que não são seller_id.
_ARG_FILL = {
    "account_name": "Maximus",
    "item_id": "MLBA",
    "variation_id": "",
    "marca_normalizada": "x",
}


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def app(db, monkeypatch):
    import app.services.scheduler as scheduler
    monkeypatch.setattr(scheduler, "start_scheduler", lambda *a, **k: None)

    from app.main import create_app
    application = create_app()          # roda todos os init_*, inclusive seller auth
    application.config.update(TESTING=True)

    conn = db.get_conn()
    conn.execute("UPDATE accounts SET seller_id=?, access_token='tok', nickname='A' WHERE name='Maximus'",  (SELLER_A,))
    conn.execute("UPDATE accounts SET seller_id=?, access_token='tok', nickname='B' WHERE name='Querencia'", (SELLER_B,))
    conn.execute("INSERT OR REPLACE INTO items (id, seller_id, title, price, status) VALUES ('MLBA', ?, 'Item A', 100, 'active')", (SELLER_A,))
    conn.execute("INSERT OR REPLACE INTO items (id, seller_id, title, price, status) VALUES ('MLBB', ?, 'Item B', 100, 'active')", (SELLER_B,))
    conn.commit()
    conn.close()

    import app.database as database
    database.create_seller_user(SELLER_A, "a@x.com", "senha12345", "Resp A", "1")
    database.create_seller_user(SELLER_B, "b@x.com", "senha12345", "Resp B", "2")
    return application


def _login_admin(app):
    from app import config
    c = app.test_client()
    r = c.post("/login", data={"modo": "admin", "password": config.ADMIN_PASSWORD})
    assert r.status_code == 302, "login de admin falhou — checar ADMIN_PASSWORD do .env"
    return c


def _login_seller(app, email):
    c = app.test_client()
    r = c.post("/login", data={"modo": "seller", "email": email, "password": "senha12345"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/painel")
    return c


@pytest.fixture
def admin_client(app):
    return _login_admin(app)


@pytest.fixture
def seller_a(app):
    return _login_seller(app, "a@x.com")


@pytest.fixture
def seller_b(app):
    return _login_seller(app, "b@x.com")


# ── Helpers de url_map ─────────────────────────────────────────────────────

def _iter_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        methods = rule.methods - {"HEAD", "OPTIONS"}
        if not methods:
            continue
        yield rule, methods


def _build(app, rule, seller_value):
    args = {}
    for a in rule.arguments:
        if a == "seller_id":
            args[a] = seller_value
        else:
            args[a] = _ARG_FILL.get(a, 1)
    return app.url_map.bind("localhost").build(rule.endpoint, args)


def _request(client, method, url):
    fn = client.get if method == "GET" else client.post
    return fn(url)


# ── 1 + 2 — allowlist + trava de seller_id, varrendo o url_map ─────────────

def test_allowlist_and_seller_id_lock_across_url_map(app, seller_a):
    from app.auth_policy import SELLER_ALLOWED

    leaks = []          # endpoint fora da allowlist que devolveu 200
    own_403 = []        # própria conta barrada indevidamente
    other_ok = []       # conta alheia NÃO barrada
    bogus_ok = []       # seller_id inexistente NÃO barrado

    for rule, methods in _iter_rules(app):
        ep = rule.endpoint
        method = "POST" if "POST" in methods else "GET"

        if ep in PUBLIC_ENDPOINTS:
            continue

        if ep not in SELLER_ALLOWED:
            # qualquer método, qualquer conta: nunca pode devolver 200
            for sv in (SELLER_A, SELLER_B):
                try:
                    url = _build(app, rule, sv)
                except Exception:
                    continue
                for m in ("GET", "POST"):
                    resp = _request(seller_a, m, url)
                    if resp.status_code == 200:
                        leaks.append(f"{ep} [{m} {url}] -> 200")
            continue

        # endpoint permitido — se tem <seller_id>, tem que travar por conta
        if "seller_id" not in rule.arguments:
            continue

        url_own   = _build(app, rule, SELLER_A)
        url_other = _build(app, rule, SELLER_B)
        url_bogus = _build(app, rule, BOGUS)

        if _request(seller_a, method, url_own).status_code == 403:
            own_403.append(f"{ep} [{method} {url_own}]")
        if _request(seller_a, method, url_other).status_code != 403:
            r = _request(seller_a, method, url_other)
            other_ok.append(f"{ep} [{method} {url_other}] -> {r.status_code}")
        if _request(seller_a, method, url_bogus).status_code != 403:
            r = _request(seller_a, method, url_bogus)
            bogus_ok.append(f"{ep} [{method} {url_bogus}] -> {r.status_code}")

    assert not leaks,    "endpoints fora da allowlist devolveram 200 pro seller:\n" + "\n".join(leaks)
    assert not own_403,  "própria conta barrada com 403 indevidamente:\n" + "\n".join(own_403)
    assert not other_ok, "conta ALHEIA acessível pelo seller (VAZAMENTO):\n" + "\n".join(other_ok)
    assert not bogus_ok, "seller_id inexistente não barrado:\n" + "\n".join(bogus_ok)


def test_known_admin_endpoints_are_forbidden_for_seller(app, seller_a):
    for url in ["/", "/dashboard", "/dashboard?account=Querencia",
                "/admin/seller-users", "/admin/aprovacoes",
                "/custos/111/save", "/custos/111/save-defaults",
                "/custos/111/refresh-precos", "/diagnostico/111/recalcular",
                "/diagnostico/111/override/MLBA", "/estoque/111/entrada",
                "/concorrencia", "/ads/111/config", "/ads/111/experimentos",
                "/collect-all"]:
        rg = seller_a.get(url)
        rp = seller_a.post(url)
        assert rg.status_code != 200, f"seller acessou {url} via GET -> {rg.status_code}"
        assert rp.status_code != 200, f"seller acessou {url} via POST -> {rp.status_code}"


def test_seller_reaches_own_read_views(app, seller_a):
    for url in ["/painel", "/custos/111", "/diagnostico/111", "/estoque/111",
                "/calendario/111", "/analise-queda/111", "/relatorio/111",
                "/promocoes/111", "/ads/111"]:
        r = seller_a.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code} (esperado 200)"


# ── 3 — seller_id forjado no cliente é ignorado ───────────────────────────

def test_forged_seller_id_in_form_is_ignored_suggest_status(app, seller_a):
    import app.database as database
    r = seller_a.post("/painel/status/sugerir", data={
        "item_id": "MLBX", "suggested_status": "descontinuar",
        "comment": "teste", "seller_id": SELLER_B, "created_by": "999",
    })
    assert r.status_code == 302
    rows = database.list_suggestions()
    assert len(rows) == 1
    assert rows[0]["seller_id"] == SELLER_A          # sessão, não o form
    assert database.list_suggestions(seller_id=SELLER_B) == []


def test_forged_seller_id_in_form_is_ignored_full_shipment(app, seller_a):
    import app.database as database
    # dá estoque próprio pro item da conta A
    conn = database.get_conn()
    conn.execute("INSERT OR REPLACE INTO stock_own (item_id, seller_id, available_qty, updated_at) VALUES ('MLBA', ?, 10, strftime('%s','now'))", (SELLER_A,))
    conn.commit(); conn.close()

    r = seller_a.post("/painel/envio-full", data={
        "item_id": "MLBA", "quantity": "3", "shipped_at": "2026-08-01",
        "seller_id": SELLER_B,
    })
    assert r.status_code == 302
    conn = database.get_conn()
    a_ship = conn.execute("SELECT COUNT(*) FROM shipments_full WHERE seller_id=?", (SELLER_A,)).fetchone()[0]
    b_ship = conn.execute("SELECT COUNT(*) FROM shipments_full WHERE seller_id=?", (SELLER_B,)).fetchone()[0]
    conn.close()
    assert a_ship == 1 and b_ship == 0


# ── 4 — suggested_qty recalculado no servidor ─────────────────────────────

def test_induction_suggested_qty_is_server_side(app, seller_a):
    import app.database as database
    from app.services.analytics import compute_all, compute_induction_enhanced

    item = next((i for i in compute_all(SELLER_A) if str(i["id"]) == "MLBA"), None)
    if item is None:
        pytest.skip("compute_all não retornou o item sintético — cobrir no teste de integração")
    expected = compute_induction_enhanced(item, database.get_stock_history(SELLER_A, "MLBA", days=90)).get("qty")

    # cliente tenta injetar suggested_qty absurdo — não existe esse campo no handler
    r = seller_a.post("/painel/inducao/decidir", data={
        "item_id": "MLBA", "decisao": "approved", "decided_qty": "40",
        "suggested_qty": "99999", "comment": "x",
    })
    assert r.status_code == 302
    dec = database.list_induction_decisions(seller_id=SELLER_A)
    assert len(dec) == 1
    assert dec[0]["suggested_qty"] == expected      # servidor, não os 99999 do form
    assert dec[0]["decided_qty"] == 40
    assert dec[0]["seller_id"] == SELLER_A

    # rejeição zera decided_qty independente do form
    seller_a.post("/painel/inducao/decidir", data={
        "item_id": "MLBA", "decisao": "rejected", "decided_qty": "77", "comment": "não",
    })
    dec = database.list_induction_decisions(seller_id=SELLER_A)
    assert dec[0]["decision"] == "rejected" and dec[0]["decided_qty"] is None


# ── 5 — swap de cookie: identidade é 100% do cookie ──────────────────────

def _session_cookie(resp):
    for h in resp.headers.getlist("Set-Cookie"):
        if h.startswith("session="):
            return h.split(";", 1)[0].split("=", 1)[1]
    return None


def test_cookie_swap_fully_swaps_identity(app):
    ca = app.test_client()
    ca.post("/login", data={"modo": "seller", "email": "a@x.com", "password": "senha12345"})

    cb = app.test_client()
    rb = cb.post("/login", data={"modo": "seller", "email": "b@x.com", "password": "senha12345"})
    tok_b = _session_cookie(rb)
    assert tok_b

    # cliente novo, só com o cookie do seller B injetado
    cx = app.test_client()
    cx.set_cookie("session", tok_b)
    assert cx.get("/custos/222").status_code == 200      # agora É o B
    assert cx.get("/custos/111").status_code == 403      # e está barrado da conta A
    assert cx.get("/").status_code == 403                # continua sendo seller, não admin


# ── 6 — admin sem regressão ──────────────────────────────────────────────

def test_admin_still_reaches_everything(admin_client):
    for url in ["/", "/dashboard?account=Maximus", "/admin/seller-users",
                "/admin/aprovacoes", "/custos/111", "/diagnostico/111",
                "/estoque/111", "/ads/111", "/custos/222", "/ads/222"]:
        r = admin_client.get(url)
        assert r.status_code == 200, f"admin bloqueado em {url} -> {r.status_code}"


# ── 7 — grep estático: session["seller_id"] só é escrito em auth.py ──────

def test_seller_id_only_assigned_in_auth_route():
    assign = re.compile(r"""session\[\s*["'](seller_id|role|seller_user_id)["']\s*\]\s*=(?!=)""")
    offenders = []
    for path in (PROJ / "app").rglob("*.py"):
        for m in assign.finditer(path.read_text(encoding="utf-8")):
            rel = path.relative_to(PROJ)
            if str(rel) != "app/routes/auth.py":
                offenders.append(f"{rel}: {m.group(0)}")
    assert not offenders, (
        "session[...] de identidade atribuído fora de app/routes/auth.py:\n"
        + "\n".join(offenders)
    )
