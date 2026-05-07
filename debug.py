from app.main import create_app
from app.database import get_account_by_name
from app.services.meli_auth import get_valid_token
from app.services.meli_api import _get, MELI_API_URL
from datetime import datetime, timedelta, timezone

app = create_app()

with app.app_context():
    account   = get_account_by_name("Maximus")
    token     = get_valid_token(account)
    seller_id = account["seller_id"]

    # Testa sem filtro de data
    print("\n--- SEM DATA ---")
    data = _get(
        f"{MELI_API_URL}/orders/search",
        token,
        params={"seller": seller_id, "limit": 5, "offset": 0}
    )
    print(f"Total: {data.get('paging', {}).get('total')}")

    # Pega a data do primeiro pedido para entender o formato
    if data.get("results"):
        print(f"Data do primeiro pedido: {data['results'][0].get('date_created')}")

    # Testa com formato diferente
    date_from = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
    print(f"\nData enviada: {date_from}")

    print("\n--- COM DATA ---")
    data2 = _get(
        f"{MELI_API_URL}/orders/search",
        token,
        params={
            "seller": seller_id,
            "order.date_created.from": date_from,
            "limit": 5,
            "offset": 0,
        }
    )
    print(f"Total: {data2.get('paging', {}).get('total')}")
    print(f"Resposta completa: {data2}")
