import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.getenv("MELI_CLIENT_ID")
CLIENT_SECRET = os.getenv("MELI_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("MELI_REDIRECT_URI", "http://localhost:5000/callback")

MELI_AUTH_URL  = "https://auth.mercadolivre.com.br/authorization"
MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
MELI_API_URL   = "https://api.mercadolibre.com"

DB_PATH = "aegis.db"

ACCOUNTS = [
    "Maximus",
    "Amigão Suplementos",
    "Querencia",
    "Smash",
    "Member XXX",
    "Foco Fit",
    "Profit",
    "Ocean Drop",
    "Renova Be",
    "Iron Meal",
    "Max Fem",
    "My Whey",
    "Gloryful",
    "The Good Store",
    "Under Labz",
    "Strongest",
]
