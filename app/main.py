from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
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


def create_app():
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "aegis-secret-key-change-in-prod"

    # Necessário para funcionar corretamente atrás do ngrok
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.jinja_env.filters["num_br"] = _num_br
    app.jinja_env.filters["brl"]    = _brl

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

    with app.app_context():
        from app.database import (
            init_data_tables, init_costs_table,
            init_promotions_table, init_history_tables,
            init_meli_finance_tables, init_mp_tables,
            init_mp_other_movements_table,
        )
        init_db()
        init_data_tables()
        init_costs_table()
        init_promotions_table()
        init_history_tables()
        init_mp_tables()
        init_meli_finance_tables()
        init_mp_other_movements_table()

    return app


if __name__ == "__main__":
    app = create_app()
    print("\n🟢 AEGIS rodando em http://localhost:8080\n")
    app.run(debug=True, port=8080)
