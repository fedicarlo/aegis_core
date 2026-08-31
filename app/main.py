from flask import Flask, request, session, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from app import config
from app.database import init_db
from app.routes.auth import auth_bp
from app.routes.web import web_bp
from app.routes.costs import costs_bp
from app.routes.promotions import promotions_bp
from app.routes.calendario import calendario_bp
from app.routes.produto import produto_bp
from app.routes.relatorio import relatorio_bp
from app.routes.simulador import simulador_bp
from app.routes.financeiro import financeiro_bp
from app.routes.analise_queda import analise_queda_bp
from app.routes.stock import stock_bp
from app.routes.concorrencia import concorrencia_bp
from app.routes.ads import ads_bp
from app.routes.seller import seller_bp
from app.auth_policy import enforce_seller_policy


def _num_br(value) -> str:
    """Formata número como moeda brasileira: 20.933,30"""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "0,00"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _brl(value) -> str:
    """Formata como R$ XX.XXX,XX"""
    return f"R$ {_num_br(value)}"


def _timestamp_to_str(ts) -> str:
    """Formata timestamp unix como dd/mm HH:MM."""
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(int(ts)).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return "—"


PUBLIC_ENDPOINTS = {"auth.authorize", "auth.callback", "auth.login", "static"}


def create_app():
    app = Flask(__name__, template_folder="templates")
    app.secret_key = config.FLASK_SECRET_KEY

    # Necessário para funcionar corretamente atrás do ngrok
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.before_request
    def require_login():
        if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
            return
        # compat: sessões antigas têm só session["admin"]; novas têm session["role"]
        role = session.get("role") or ("admin" if session.get("admin") else None)
        if not role:
            return redirect(url_for("auth.login", next=request.path))
        if role == "admin":
            return                       # acesso irrestrito — igual a hoje
        return enforce_seller_policy()    # seller: allowlist + trava de seller_id

    @app.context_processor
    def inject_role():
        role = session.get("role") or ("admin" if session.get("admin") else None)
        return {"role": role, "is_seller": role == "seller"}

    app.jinja_env.filters["num_br"] = _num_br
    app.jinja_env.filters["brl"]    = _brl
    app.jinja_env.filters["timestamp_to_str"] = _timestamp_to_str

    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(costs_bp)
    app.register_blueprint(promotions_bp)
    app.register_blueprint(calendario_bp)
    app.register_blueprint(produto_bp)
    app.register_blueprint(relatorio_bp)
    app.register_blueprint(simulador_bp)
    app.register_blueprint(financeiro_bp)
    app.register_blueprint(analise_queda_bp)
    app.register_blueprint(stock_bp)
    app.register_blueprint(concorrencia_bp)
    app.register_blueprint(ads_bp)
    app.register_blueprint(seller_bp)

    with app.app_context():
        from app.database import (
            init_data_tables, init_costs_table,
            init_promotions_table, init_history_tables,
            init_meli_finance_tables, init_mp_tables,
            init_mp_other_movements_table, init_stock_own_tables,
            init_nfe_tables, init_diagnostico_tables, init_concorrencia_tables,
            init_ads_tables, init_seller_auth_tables,
        )
        init_db()
        init_data_tables()
        init_costs_table()
        init_promotions_table()
        init_history_tables()
        init_mp_tables()
        init_meli_finance_tables()
        init_mp_other_movements_table()
        init_stock_own_tables()
        init_nfe_tables()
        init_diagnostico_tables()
        init_concorrencia_tables()
        init_ads_tables()
        init_seller_auth_tables()

        from app.services.ads_strategy import seed_default_profile
        seed_default_profile()

        from app.services.scheduler import start_scheduler
        start_scheduler()

    return app


if __name__ == "__main__":
    import os

    port = int(os.getenv("PORT", 8080))
    app = create_app()
    print(f"\n🟢 AEGIS rodando em http://localhost:{port}\n")
    app.run(debug=True, host="0.0.0.0", port=port)
