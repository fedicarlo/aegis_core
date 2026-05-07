from flask import Blueprint, jsonify, request
import requests

test_meli_bp = Blueprint("test_meli", __name__)

@test_meli_bp.route("/test-search")
def test_search():
    query = request.args.get("q", "vitamina b12")

    url = "https://api.mercadolibre.com/sites/MLB/search"

    params = {
        "q": query,
        "limit": 10
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    try:
        res = requests.get(url, params=params, headers=headers, timeout=10)

        return jsonify({
            "status": res.status_code,
            "query": query,
            "response": res.json()
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        })
