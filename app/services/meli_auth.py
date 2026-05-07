import time
import urllib.parse
import requests

from app.config import (
    CLIENT_ID, CLIENT_SECRET, REDIRECT_URI,
    MELI_AUTH_URL, MELI_TOKEN_URL, MELI_API_URL
)
from app.database import save_tokens, update_access_token


def build_auth_url(account_name: str) -> str:
    params = {
        "response_type": "code",
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "state":         account_name,
    }
    return f"{MELI_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(code: str, account_name: str) -> dict:
    payload = {
        "grant_type":    "authorization_code",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
    }

    safe_payload = {k: v for k, v in payload.items() if k != "client_secret"}
    safe_payload["client_secret"] = "***"
    print(f"\n[exchange_code] account={account_name}")
    print(f"[exchange_code] URL: {MELI_TOKEN_URL}")
    print(f"[exchange_code] payload: {safe_payload}")

    res = requests.post(
        MELI_TOKEN_URL,
        data=payload,
        headers={
            "accept":       "application/json",
            "content-type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )

    print(f"[exchange_code] status: {res.status_code}")
    if res.status_code != 200:
        print(f"[exchange_code] error body: {res.text}")
        res.raise_for_status()

    data = res.json()

    user_info = get_user_info(data["access_token"])

    save_tokens(
        name          = account_name,
        seller_id     = str(user_info["id"]),
        nickname      = user_info.get("nickname", ""),
        access_token  = data["access_token"],
        refresh_token = data["refresh_token"],
        expires_in    = data.get("expires_in", 21600),
    )
    return data


def refresh_token(account: dict) -> str:
    payload = {
        "grant_type":    "refresh_token",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": account["refresh_token"],
    }
    res = requests.post(
        MELI_TOKEN_URL,
        data=payload,
        headers={
            "accept":       "application/json",
            "content-type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )
    res.raise_for_status()
    data = res.json()

    update_access_token(
        seller_id     = account["seller_id"],
        access_token  = data["access_token"],
        refresh_token = data["refresh_token"],
        expires_in    = data.get("expires_in", 21600),
    )
    return data["access_token"]


def get_valid_token(account: dict) -> str:
    margem = 300
    if account["expires_at"] and int(time.time()) < (account["expires_at"] - margem):
        return account["access_token"]
    return refresh_token(account)


def get_user_info(access_token: str) -> dict:
    res = requests.get(
        f"{MELI_API_URL}/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    res.raise_for_status()
    return res.json()
