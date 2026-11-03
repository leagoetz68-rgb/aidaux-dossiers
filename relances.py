"""Relances automatiques des titres de séjour.

Appelé une fois par jour par le Cron Vercel (voir vercel.json) :
  - 3 mois avant la fin de validité  → mail de relance au salarié
  - 1 mois avant                     → 2e relance au salarié
  - 15 jours avant (ou déjà expiré)  → alerte par mail à Marie

Chaque mail n'est envoyé qu'une seule fois par titre (table dossiers_relances).
Quand une nouvelle date de validité est saisie, le cycle repart de zéro.
"""
import os
from datetime import date

from flask import Blueprint, jsonify, request
from psycopg2.extras import RealDictCursor

import auth

bp = Blueprint("relances", __name__)

# Paliers du plus urgent au moins urgent : (code, jours avant la fin)
PALIERS = [("alerte_15j", 15), ("relance_1m", 30), ("relance_3m", 90)]

ALERTE_EMAIL = os.environ.get("ALERTE_TITRE_EMAIL", "marie.mischel@aidaux.fr")
# Copie (cachée) des relances envoyées aux salariés : vide = pas de copie
COPIE_RH = os.environ.get("RELANCE_COPIE_EMAIL", "lea.goetz@aidaux.fr")
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def init_db():
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE dossiers_salaries ADD COLUMN IF NOT EXISTS email TEXT")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS dossiers_relances (
                    id            SERIAL PRIMARY KEY,
                    salarie_id    INTEGER NOT NULL,
                    date_validite DATE NOT NULL,
                    palier        TEXT NOT NULL,
                    envoye_a      TEXT,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (salarie_id, date_validite, palier)
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


def _a_un_titre_de_sejour(docs):
    piece = (docs or {}).get("piece") or {}
    types = piece.get("types")
    if isinstance(types, dict) and types:
        return bool(types.get("titre"))
    return piece.get("t") == "titre"


def _fmt(d):
    return d.strftime("%d/%m/%Y")


def _envoyer(destinataire, sujet, corps, bcc=None):
    if not auth.BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY non configurée")
    payload = {
        "sender": {"name": auth.EXPEDITEUR_NOM, "email": auth.EXPEDITEUR_EMAIL},
        "to": [{"email": destinataire}],
        "subject": sujet,
        "textContent": corps,
    }
    if bcc and bcc.lower() != destinataire.lower():
        payload["bcc"] = [{"email": bcc}]
    r = auth.requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": auth.BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
        json=payload,
        timeout=10,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Brevo {r.status_code} : {r.text}")


def _mail_salarie(s, fin, jours, palier):
    prenom = s["nom"].split()[-1].capitalize() if s["nom"].split() else ""
    delai = "dans 3 mois" if palier == "relance_3m" else "dans 1 mois"
    sujet = "Renouvellement de votre titre de séjour"
    if palier == "relance_1m":
        sujet = "2e rappel : renouvellement de votre titre de séjour"
    corps = (
        f"Bonjour {prenom},\n\n"
        f"Votre titre de séjour arrive à échéance {delai}, le {_fmt(fin)}.\n\n"
        "Pensez à faire votre demande de renouvellement dès maintenant si ce n'est pas déjà fait. "
        "Dès que vous avez votre nouveau titre de séjour (ou le récépissé de demande), "
        "merci de nous en transmettre une copie.\n\n"
        "Sans document valable à la date d'échéance, nous ne pourrons malheureusement pas "
        "maintenir votre planning.\n\n"
        "Pour toute question, n'hésitez pas à contacter le service administratif.\n\n"
        "Bonne journée,\nLe service administratif AID'Aux"
    )
    return sujet, corps


def _mail_alerte(s, fin, jours):
    if jours < 0:
        quand = f"est EXPIRÉ depuis le {_fmt(fin)}"
    elif jours == 0:
        quand = "expire AUJOURD'HUI"
    else:
        quand = f"expire dans {jours} jours, le {_fmt(fin)}"
    sujet = f"⚠️ Alerte titre de séjour : {s['nom']} ({s['agence']})"
    corps = (
        "Bonjour Marie,\n\n"
        f"Le titre de séjour de {s['nom']} (agence {s['agence']}) {quand}.\n\n"
        "Aucun nouveau titre n'a été enregistré dans Aid'Aux Dossiers. "
        f"{'Les deux relances par mail ont été envoyées au salarié.' if s.get('email') else 'Aucune adresse mail n’est renseignée pour ce salarié : il n’a pas reçu de relance.'}\n\n"
        "Merci de faire le point avec lui/elle rapidement (récépissé de demande, nouveau titre).\n\n"
        "Message automatique — Aid'Aux Dossiers"
    )
    return sujet, corps


def lancer_relances(aujourdhui=None):
    aujourdhui = aujourdhui or date.today()
    rapport = []
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, nom, agence, email, docs, ech FROM dossiers_salaries WHERE actif")
            salaries = cur.fetchall()
            for s in salaries:
                if not _a_un_titre_de_sejour(s["docs"]):
                    continue
                e = (s["ech"] or {}).get("piece") or {}
                if e.get("nc") or not e.get("date"):
                    continue
                try:
                    fin = date.fromisoformat(e["date"][:10])
                except ValueError:
                    continue
                jours = (fin - aujourdhui).days
                # Palier le plus urgent atteint
                atteint = next(((code, n) for code, n in PALIERS if jours <= n), None)
                if not atteint:
                    continue
                code, _ = atteint
                cur.execute(
                    "SELECT 1 FROM dossiers_relances WHERE salarie_id=%s AND date_validite=%s AND palier=%s",
                    (s["id"], fin, code),
                )
                if cur.fetchone():
                    continue
                try:
                    if code == "alerte_15j":
                        dest = ALERTE_EMAIL
                        sujet, corps = _mail_alerte(s, fin, jours)
                        _envoyer(dest, sujet, corps)
                        news = (f"Alerte envoyée à Marie : titre de séjour de {s['nom']} "
                                f"{'expiré' if jours < 0 else 'expire le ' + _fmt(fin)}")
                    elif s.get("email"):
                        dest = s["email"].strip()
                        sujet, corps = _mail_salarie(s, fin, jours, code)
                        _envoyer(dest, sujet, corps, bcc=COPIE_RH or None)
                        num = "1re" if code == "relance_3m" else "2e"
                        news = f"{num} relance titre de séjour envoyée à {s['nom']}"
                    else:
                        dest = None
                        num = "1re" if code == "relance_3m" else "2e"
                        news = f"{num} relance titre de séjour de {s['nom']} non envoyée : pas d’email renseigné"
                except Exception as err:  # on continue avec les autres salariés
                    rapport.append({"salarie": s["nom"], "palier": code, "erreur": str(err)})
                    continue
                # On marque ce palier (et les paliers moins urgents) comme traités
                for c, n in PALIERS:
                    if n >= dict(PALIERS)[code]:
                        cur.execute(
                            """INSERT INTO dossiers_relances (salarie_id, date_validite, palier, envoye_a)
                               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                            (s["id"], fin, c, dest if c == code else None),
                        )
                cur.execute(
                    "INSERT INTO dossiers_feed (texte, couleur, auteur) VALUES (%s, %s, %s)",
                    (news[:300], "coral", "relance automatique"),
                )
                conn.commit()
                rapport.append({"salarie": s["nom"], "palier": code, "envoye_a": dest, "jours": jours})
    finally:
        conn.close()
    return rapport


@bp.route("/api/cron/relances")
def cron_relances():
    # Vercel envoie automatiquement « Authorization: Bearer <CRON_SECRET> »
    if not CRON_SECRET or request.headers.get("Authorization") != f"Bearer {CRON_SECRET}":
        return jsonify({"ok": False, "erreur": "non autorisé"}), 401
    return jsonify({"ok": True, "traites": lancer_relances()})
