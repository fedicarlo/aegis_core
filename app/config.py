import os
import secrets
from dotenv import load_dotenv, set_key

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(ENV_PATH)


def _get_or_create_secret(key: str, generator) -> str:
    """Lê a chave do .env; se não existir, gera uma e persiste no arquivo."""
    value = os.getenv(key)
    if not value:
        value = generator()
        set_key(ENV_PATH, key, value)
        os.environ[key] = value
    return value


CLIENT_ID     = os.getenv("MELI_CLIENT_ID")
CLIENT_SECRET = os.getenv("MELI_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("MELI_REDIRECT_URI", "http://localhost:5000/callback")

# Sessão administrativa (protege todas as rotas exceto /authorize e /callback)
FLASK_SECRET_KEY = _get_or_create_secret("FLASK_SECRET_KEY", lambda: secrets.token_hex(32))
ADMIN_PASSWORD   = _get_or_create_secret("ADMIN_PASSWORD", lambda: secrets.token_urlsafe(9))

MELI_AUTH_URL  = "https://auth.mercadolivre.com.br/authorization"
MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
MELI_API_URL   = "https://api.mercadolibre.com"

DB_PATH = os.getenv("DB_PATH", "aegis.db")

# Sync automática (scheduler). Desligada por padrão — precisa ser ligada
# explicitamente via env var (ex: Railway) pra não disparar chamadas reais
# à API do ML sempre que alguém rodar o app localmente.
AUTO_SYNC_ENABLED        = os.getenv("AUTO_SYNC_ENABLED", "false").lower() == "true"
AUTO_SYNC_INTERVAL_HOURS = int(os.getenv("AUTO_SYNC_INTERVAL_HOURS", "6"))

# Sync de Ads (Mercado Ads). Job separado do auto-sync de itens/pedidos: as
# métricas de Ads do ML só consolidam ~10h GMT-3 (13h UTC), então roda 1x/dia
# de madrugada-manhã UTC, não a cada 6h. Também desligada por padrão.
ADS_SYNC_ENABLED     = os.getenv("ADS_SYNC_ENABLED", "false").lower() == "true"
ADS_SYNC_HOUR_UTC    = int(os.getenv("ADS_SYNC_HOUR_UTC", "13"))
ADS_SYNC_WINDOW_DAYS = int(os.getenv("ADS_SYNC_WINDOW_DAYS", "35"))

ACCOUNTS = [
    "Maximus",
    "Querencia",
    "Smash",
    "ECO3D",
    "The Good Store",
    "NEXORAHUB",
    "VIVALEVE",
    "BENETIMCOMER",
]
