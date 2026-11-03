"""
Client minimal pour l'API Ximi V2 (Xelya) - apps Aid'Aux.

Variables d'environnement (Vercel / Render) :
  XIMI_API_KEY_ID   -> identifiant de clé fourni par Xelya (format a067d785-....)
  XIMI_PRIVATE_KEY  -> contenu complet de la clé privée PEM (BEGIN ... END inclus)
  XIMI_BASE_URL     -> optionnel, défaut https://api.ximi.xelya.io/Ximi3
  XIMI_CLEARANCE    -> optionnel (0, 1 ou 2), seulement si Xelya vous l'indique

requirements.txt : PyJWT, cryptography, requests
"""
import os
import time

import jwt  # PyJWT
import requests

BASE_URL = os.environ.get("XIMI_BASE_URL", "https://api.ximi.xelya.io/Ximi3").rstrip("/")
TOKEN_DURATION = 15 * 60  # secondes

_cache = {"token": None, "exp": 0}


def _private_key():
    key = os.environ.get("XIMI_PRIVATE_KEY", "")
    # Si la clé a été collée sur une seule ligne avec des \n littéraux
    return key.replace("\\n", "\n").strip()


def _api_key():
    """Génère (ou réutilise) le JWT signé RS512 à mettre dans le header Api-Key."""
    now = int(time.time())
    if _cache["token"] and _cache["exp"] - now > 60:
        return _cache["token"]

    payload = {"sub": os.environ["XIMI_API_KEY_ID"], "exp": now + TOKEN_DURATION}
    clearance = os.environ.get("XIMI_CLEARANCE")
    if clearance not in (None, ""):
        payload["clearance"] = int(clearance)

    token = jwt.encode(payload, _private_key(), algorithm="RS512")
    _cache.update(token=token, exp=payload["exp"])
    return token


def _headers():
    return {"Content-Type": "application/json", "Api-Key": _api_key()}


def get(path, params=None, timeout=30):
    """GET simple. path ex : 'api/agents' ou 'api/agents/123'."""
    url = f"{BASE_URL}/{path.lstrip('/')}"
    r = requests.get(url, headers=_headers(), params=params or {}, timeout=timeout)
    if r.status_code >= 400:
        # On garde le message renvoyé par Ximi : il explique souvent la cause
        raise Exception(f"{r.status_code} {r.reason} - reponse Ximi : {r.text[:500]}")
    return r.json()


def get_all(path, params=None, page_size=1000):
    """Récupère toutes les pages d'une liste (Offset / Top / HasMoreRows)."""
    params = dict(params or {})
    results, offset = [], 0
    while True:
        params.update(Offset=offset, Top=page_size, ComputeHasMoreRows="true")
        data = get(path, params)
        page = data.get("Results", [])
        results.extend(page)
        if not data.get("HasMoreRows") or not page:
            return results
        offset += len(page)


def ping():
    """Petit test de connexion, identique à l'exemple Postman."""
    return get("api/contactSources", {"Offset": 0, "Top": 5})
