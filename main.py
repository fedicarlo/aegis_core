from flask import Flask
from app.database import init_db
from app.routes.auth import auth_bp
from app.routes.web import web_bp

def create_app():
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "aegis-secret-key-change-in-prod"

    # Registra blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)

    # Inicializa banco e popula contas
    with app.app_context():
        init_db()

    return app


if __name__ == "__main__":
    app = create_app()
    print("\n🟢 AEGIS rodando em http://localhost:5000\n")
    app.run(debug=True, port=5000)
