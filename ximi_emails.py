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


# Nom du champ texte de la note : on essaie les noms courants, et on retient celui que Ximi accepte.
CHAMPS_NOTE = ["Content", "Text", "Note", "Comment", "Description", "Body"]
_champ_ok = {"nom": None}


def _envoyer_note(agent_id, texte):
    essais = [_champ_ok["nom"]] if _champ_ok["nom"] else CHAMPS_NOTE
    derniere = None
    for champ in essais:
        try:
            ximi.put(f"api/agents/{agent_id}/notes", {champ: texte})
            _champ_ok["nom"] = champ
            return
        except Exception as e:  # 400 = champ refusé : on essaie le suivant ; autre erreur : on arrête
            derniere = e
            if not str(e).startswith("400"):
                break
    raise derniere


def noter_dans_ximi(nom_salarie, texte):
    """Ajoute une note dans le Journal Ximi du salarié. Renvoie (ok, message) ; ne lève jamais d'erreur."""
    try:
        a = _correspond(nom_salarie, _agents())
        if not a or a.get("Id") is None:
            return False, "salarié introuvable dans Ximi"
        _envoyer_note(a["Id"], texte[:2000])
        return True, "noté dans Ximi"
    except Exception as e:
        return False, str(e)[:300]


def synchro_manquants():
    """Note dans le Journal Ximi la liste des éléments manquants des salariés dont la liste a changé
    depuis la dernière note (une seule note par salarié et par changement)."""
    import datetime
    from psycopg2.extras import Json
    jour = datetime.date.today().strftime("%d/%m/%Y")
    res = {"notes": 0, "introuvables": [], "erreurs": []}
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT id, nom, manquants, manquants_ximi FROM dossiers_salaries
                           WHERE actif AND manquants IS NOT NULL
                             AND manquants IS DISTINCT FROM manquants_ximi""")
            for s in cur.fetchall():
                liste, avant = s["manquants"] or [], s["manquants_ximi"]
                if avant == "reset":  # reprise silencieuse après le changement de format : pas de note
                    cur.execute("UPDATE dossiers_salaries SET manquants_ximi = %s WHERE id = %s", (Json(liste), s["id"]))
                    continue
                if not liste and not avant:  # complet et jamais signalé : rien à dire
                    cur.execute("UPDATE dossiers_salaries SET manquants_ximi = %s WHERE id = %s", (Json(liste), s["id"]))
                    continue
                # Ximi n'affiche pas les retours à la ligne : note sur une ligne, éléments séparés par des virgules
                recus = [x for x in (avant or []) if x not in liste]
                if liste:
                    texte = f"Documents d'embauche – Aid'Aux Dossiers ({jour}). Manquants : {', '.join(liste)}."
                    if recus:
                        texte += f" Reçus depuis la dernière note : {', '.join(recus)}."
                else:
                    texte = f"Documents d'embauche – Aid'Aux Dossiers ({jour}). Tous les documents ont été reçus."
                ok, msg = noter_dans_ximi(s["nom"], texte)
                if ok:
                    res["notes"] += 1
                    cur.execute("UPDATE dossiers_salaries SET manquants_ximi = %s WHERE id = %s", (Json(liste), s["id"]))
                elif "introuvable" in msg:
                    res["introuvables"].append(s["nom"])
                else:
                    res["erreurs"].append(f"{s['nom']} : {msg}")
                    if len(res["erreurs"]) >= 3:  # Ximi en panne : on réessaiera demain
                        break
                conn.commit()
        conn.commit()
    finally:
        conn.close()
    return res


@bp.route("/api/ximi/synchro", methods=["POST"])
def api_synchro():
    try:
        return jsonify({"ok": True, **synchro_manquants()})
    except Exception as e:
        return jsonify({"ok": False, "erreur": f"Ximi : {e}"}), 502


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


# Diagnostic : la doc REST de Ximi n'a aucune fonction pour créer un email sur un intervenant.
# Cette page interroge la partie GraphQL de Ximi pour voir si une "mutation" le permet.
@bp.route("/api/ximi/diagnostic-emails")
def api_diag_emails():
    q = "{ __schema { mutationType { fields { name description args { name } } } } }"
    try:
        data = ximi.post("api/graphql", {"Query": q})
    except Exception as e:
        return jsonify({"ok": False, "etape": "graphql", "erreur": str(e)}), 502
    champs = (((data.get("data") or {}).get("__schema") or {}).get("mutationType") or {}).get("fields") or []
    mots = ("mail", "message", "note", "agent", "communication")
    utiles = [c for c in champs if any(m in (c.get("name") or "").lower() for m in mots)]
    return jsonify({"ok": True, "nb_mutations": len(champs), "pistes": utiles,
                    "toutes": [c.get("name") for c in champs], "brut_si_vide": None if champs else data})
