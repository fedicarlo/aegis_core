#!/usr/bin/env python3
"""Validação da Etapa 8 (UI) — renderiza as telas contra Maximus via test client."""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta

ROOT = "/Volumes/AegisData/Gerais/Consultorias/projetos/aegis_core"
SCRATCH = ("/private/tmp/claude-501/-Users-felipedicarlo/"
           "61c17962-4515-49cb-bc06-165a556019ed/scratchpad/aegis_ads_ui.db")
if "--reuse" not in sys.argv or not os.path.exists(SCRATCH):
    shutil.copyfile(os.path.join(ROOT, "aegis.db"), SCRATCH)
os.environ["DB_PATH"] = SCRATCH
os.environ.setdefault("ADMIN_PASSWORD", "x")
sys.path.insert(0, ROOT)

from app.database import init_ads_tables, get_account_by_name  # noqa: E402
from app.services.ads_collector import collect_ads_account  # noqa: E402
from app.services import ads_alerts, ads_strategy, ads_view  # noqa: E402
from scripts.backfill_buyer_ids import backfill_account  # noqa: E402

RENDER_DIR = "/private/tmp/claude-501/-Users-felipedicarlo/61c17962-4515-49cb-bc06-165a556019ed/scratchpad/ads_ui"


def prod_token(account="Maximus"):
    code = ("from app.database import get_account_by_name;"
            f"a=get_account_by_name({account!r});print(a['access_token'])")
    out = subprocess.check_output(
        ["railway", "ssh", f'cd /app && .venv/bin/python -c "{code}"'], text=True)
    for line in out.splitlines():
        if line.strip().startswith("APP_USR-"):
            return line.strip()
    raise SystemExit("token não encontrado")


def hr(t):
    print(f"\n{'═' * 82}\n{t}\n{'═' * 82}")


def main():
    init_ads_tables()
    ads_strategy.seed_default_profile()
    sid = "2124526664"
    if "--reuse" not in sys.argv:
        acc = get_account_by_name("Maximus")
        tok = prod_token("Maximus")
        collect_ads_account(acc, token=tok, window_days=40)
        backfill_account(sid, tok, days=220)
        ads_alerts.run_alerts(sid)  # popula ads_alerts p/ o cockpit mostrar

    hr("1) ads_view.cockpit — dict montado")
    d = ads_view.cockpit(sid, period=30)
    print(json.dumps({k: d[k] for k in (
        "window", "status_geral", "investimento", "roas_objetivo",
        "profile_source")}, ensure_ascii=False, indent=2, default=str))
    print("\nfunil:"); print(json.dumps(d["funil"], ensure_ascii=False, indent=2, default=str))
    print("\neconomia:"); print(json.dumps(d["economia"], ensure_ascii=False, indent=2, default=str))
    print(f"\nalertas_abertos: {len(d['alertas_abertos'])}")
    for a in d["alertas_abertos"]:
        print(f"  [{a['severidade']}] {a['tipo']} · {a['scope']} {a['target_id']}")
    print("\ncampanhas:")
    for c in d["campanhas"]:
        print(f"  {c['name']:22s} serving={c['serving']} diag={c['diag_status']} "
              f"caso={c['caso_primario']} is%={c['impression_share_pct']} "
              f"acos={c['acos']} roas={c['roas']}")

    hr("2) render via test client — HTTP 200 e sem template error")
    from app.main import create_app
    app = create_app()
    app.config["TESTING"] = True
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["admin"] = True
    paths = [f"/ads/{sid}", f"/ads/{sid}?period=7"]
    if "--8b" in sys.argv:
        paths.append(f"/ads/{sid}/campanhas?period=30")
        paths.append(f"/ads/{sid}/campanhas?period=30&desde_ultima_alteracao=1")
    if "--8c" in sys.argv:
        paths.append(f"/ads/{sid}/campanha/354852122?period=30")
        paths.append(f"/ads/{sid}/campanha/358737092?period=30")
        paths.append(f"/ads/{sid}/campanha/354852122?period=14&desde_ultima_alteracao=1")
    if "--8d" in sys.argv:
        paths.append(f"/ads/{sid}/ad-group/1538648021?period=30")
        paths.append(f"/ads/{sid}/ad-group/2762365993?period=30")
    if "--8e" in sys.argv:
        paths.append(f"/ads/{sid}/experimentos")
    if "--8f" in sys.argv:
        paths.append(f"/ads/{sid}/config")
    os.makedirs(RENDER_DIR, exist_ok=True)

    if "--8e" in sys.argv:
        from datetime import date as _d, timedelta as _td
        rp = cli.post(f"/ads/{sid}/experimentos", data={
            "acao": "experimento", "alvo": "campaign:354852122",
            "hipotese": "baixar ROAS-alvo 14->12 aumenta volume mantendo margem",
            "intervencao": "roas_target 14 -> 12",
            "janela_inicio": (_d.today() - _td(days=12)).isoformat(),
        }, follow_redirects=False)
        print(f"  POST criar experimento -> {rp.status_code} (redirect {rp.headers.get('Location')})")
        xid = rp.headers.get("Location", "/0").rstrip("/").split("/")[-1]
        cli.post(f"/ads/{sid}/experimentos", data={
            "acao": "evento", "alvo": "campaign:354852122", "field": "roas_target",
            "old_value": "14", "new_value": "12", "author": "lipe",
            "motivo": "margem depois de Ads no limite"}, follow_redirects=False)
        paths.append(f"/ads/{sid}/experimentos")
        paths.append(f"/ads/{sid}/experimento/{xid}")
        rp2 = cli.post(f"/ads/{sid}/experimento/{xid}", data={
            "status": "concluido", "conclusao": "volume subiu, margem manteve",
            "recomputar": "1"}, follow_redirects=False)
        print(f"  POST concluir experimento -> {rp2.status_code}")

    for path in paths:
        r = cli.get(path)
        html = r.get_data(as_text=True)
        err = re.search(r"(TemplateError|UndefinedError|jinja2|Traceback)", html)
        fname = os.path.join(RENDER_DIR, re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") + ".html")
        with open(fname, "w") as fh:
            fh.write(html)
        print(f"  GET {path:48s} -> {r.status_code}  len={len(html)}  {'ERRO TEMPLATE!' if err else 'ok'}  -> {fname}")
        assert r.status_code == 200 and not err, html[:2000]

    print(f"\nHTML renderizado salvo em {RENDER_DIR}/ — abre no navegador pra revisar")
    print("\n== fim — asserts OK ==")


if __name__ == "__main__":
    main()
