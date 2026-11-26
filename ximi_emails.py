"""Récupération des emails des salariés depuis l'API Ximi (agents).

- GET /api/ximi/emails : liste {nom, mail} pour l'écran « Emails Ximi » (aperçu avant enregistrement)
- remplir_emails_vides() : appelé chaque matin par le cron, remplit les emails encore vides
"""
import re
import unicodedata

from flask import Blueprint, jsonify
from psycopg2.extras import RealDictCursor

import auth
import ximi

bp = Blueprint("ximi_emails", __name__)

_RE_MAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _nk(x):
    x = unicodedata.normalize("NFD", str(x or ""))
    x = "".join(c for c in x if unicodedata.category(c) != "Mn").upper()
    return re.sub(r"[^A-Z]+", " ", x).strip()


def _cle(x):
    return " ".join(sorted(w for w in _nk(x).split(" ") if w))


def _trouver_email(obj, profondeur=0):
    """Cherche une adresse email dans la fiche agent, quel que soit le nom du champ."""
    if profondeur > 3:
        return None
    if isinstance(obj, dict):
        # Champs « mail » d'abord, en préférant le mail principal / perso / pro
        cles = sorted(obj.keys(), key=lambda k: (0 if k.lower() in ("email", "mail", "emailaddress") else 1, k))
        for k in cles:
            v = obj[k]
            if "mail" in k.lower() and isinstance(v, str) and _RE_MAIL.match(v.strip()):
                return v.strip().lower()
        for v in obj.values():
            if isinstance(v, (dict, list)):
                m = _trouver_email(v, profondeur + 1)
                if m:
                    return m
    elif isinstance(obj, list):
        for v in obj:
            m = _trouver_email(v, profondeur + 1)
            if m:
                return m
    return None


def _nom_agent(a):
    return f"{a.get('LastName') or ''} {a.get('FirstName') or ''}".strip()


def _salaries(cur):
    cur.execute("SELECT id, nom, email FROM dossiers_salaries WHERE actif")
    return cur.fetchall()


def _correspond(nom_salarie, agents):
    """Retrouve l'agent Ximi d'un salarié (nom identique, sinon tous les mots inclus, si unique)."""
    k = _cle(nom_salarie)
    exacts = [a for a in agents if _cle(_nom_agent(a)) == k]
    if len(exacts) == 1:
        return exacts[0]
    mots = k.split(" ")
    inclus = [a for a in agents if all(w in _cle(_nom_agent(a)).split(" ") for w in mots)]
    return inclus[0] if len(inclus) == 1 else None


def emails_ximi(salaries):
    """Renvoie [{nom, mail, salarie_id}] pour les salariés retrouvés dans Ximi avec un email."""
    agents = ximi.get_all("api/agents")
    out = []
    for s in salaries:
        a = _correspond(s["nom"], agents)
        if not a:
            continue
        mail = _trouver_email(a)
        if not mail and a.get("Id") is not None:
            try:  # l'email n'est parfois que dans la fiche détaillée
                mail = _trouver_email(ximi.get(f"api/agents/{a['Id']}"))
            except Exception:
                mail = None
        if mail:
            out.append({"nom": s["nom"], "mail": mail, "salarie_id": s["id"]})
    return out


# ---- Journal Ximi : chaque relance est aussi notée sur la fiche de l'intervenant ----
_agents_cache = {"liste": None, "t": 0}


def _agents():
    import time
    if _agents_cache["liste"] is None or time.time() - _agents_cache["t"] > 600:
        _agents_cache.update(liste=ximi.get_all("api/agents"), t=time.time())
    return _agents_cache["liste"]


def corps_note(texte):
    """Corps envoyé à PUT api/agents/{id}/notes (format de la doc Ximi « Ajouter une Note dans le Journal »)."""
    return {"Content": texte}


def noter_dans_ximi(nom_salarie, texte):
    """Ajoute une note dans le Journal Ximi du salarié. Renvoie (ok, message) ; ne lève jamais d'erreur."""
    try:
        a = _correspond(nom_salarie, _agents())
        if not a or a.get("Id") is None:
            return False, "salarié introuvable dans Ximi"
        ximi.put(f"api/agents/{a['Id']}/notes", corps_note(texte[:2000]))
        return True, "noté dans Ximi"
    except Exception as e:
        return False, str(e)[:300]


@bp.route("/api/salaries/<int:sid>/ximi-note", methods=["POST"])
def api_note_ximi(sid):
    from flask import request
    texte = str((request.get_json(silent=True) or {}).get("texte") or "").strip()
    if not texte:
        return jsonify({"ok": False, "erreur": "Note vide"}), 400
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT nom FROM dossiers_salaries WHERE id=%s", (sid,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"ok": False, "erreur": "Salarié introuvable"}), 404
    ok, msg = noter_dans_ximi(row["nom"], texte)
    return jsonify({"ok": ok, "message": msg, "erreur": None if ok else msg}), (200 if ok else 502)


@bp.route("/api/ximi/emails")
def api_emails():
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            salaries = _salaries(cur)
    finally:
        conn.close()
    try:
        return jsonify({"ok": True, "lignes": emails_ximi(salaries)})
    except Exception as e:
        return jsonify({"ok": False, "erreur": f"Ximi : {e}"}), 502


def remplir_emails_vides():
    """Pour le cron : complète uniquement les emails encore vides (n'écrase jamais une saisie)."""
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            vides = [s for s in _salaries(cur) if not s["email"]]
            if not vides:
                return 0
            lignes = emails_ximi(vides)
            for l in lignes:
                cur.execute("UPDATE dossiers_salaries SET email=%s WHERE id=%s AND email IS NULL",
                            (l["mail"], l["salarie_id"]))
        conn.commit()
        return len(lignes)
    finally:
        conn.close()
