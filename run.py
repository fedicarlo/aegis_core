import os

from app.main import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    print(f"\n🟢 AEGIS rodando em http://localhost:{port}\n")
    app.run(debug=True, host="0.0.0.0", port=port)
