from app.main import create_app

app = create_app()

if __name__ == "__main__":
    print("\n🟢 AEGIS rodando em http://localhost:5000\n")
    app.run(debug=True, port=8080)
