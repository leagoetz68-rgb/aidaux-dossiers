"""Relances automatiques des titres de séjour et des attestations CSS.

Appelé une fois par jour par le Cron Vercel (voir vercel.json) :
  - 3 mois avant la fin de validité  → mail de relance au salarié
  - 1 mois avant                     → 2e relance au salarié
  - 15 jours avant (ou déjà expiré)  → alerte par mail à Marie

Même chose pour l'attestation CSS (complémentaire santé solidaire), pour tous
les salariés qui ont une date « Valable jusqu'au » renseignée.

Chaque mail n'est envoyé qu'une seule fois par document (table dossiers_relances).
Quand une nouvelle date de validité est saisie, le cycle repart de zéro.

Visite médecine du travail : rappel par mail au salarié 7 jours avant la
date du RDV (champ « RDV pris le »), une seule fois par RDV.
"""
import os
from datetime import date

from flask import Blueprint, jsonify, request
from psycopg2.extras import Json, RealDictCursor

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


def _noter_relance(cur, salarie_id, jour, commentaire):
    """Ajoute une relance automatique (mail) à l'historique « Suivi des relances »
    de la fiche du salarié (champ vie.relances), sans toucher au reste de « vie »."""
    entree = Json({"d": jour.isoformat(), "t": "mail", "c": commentaire[:200], "auto": True})
    cur.execute(
        """UPDATE dossiers_salaries
           SET vie = jsonb_set(COALESCE(vie, '{}'::jsonb), '{relances}',
                               COALESCE(vie->'relances', '[]'::jsonb) || jsonb_build_array(%s::jsonb))
           WHERE id = %s""",
        (entree, salarie_id),
    )


def _attestation_perimable(docs):
    """Adhésion à la mutuelle : attestation demandée une seule fois, sans date de validité (pas de relance).
    Dispense (CSS, mutuelle perso, autre) ou choix pas encore fait : elle expire et doit être redemandée."""
    m = (docs or {}).get("mutuelle") or {}
    if m.get("mut") == "adhesion":
        return False
    # dispense justifiée par autre chose que la CSS (CPAM, mutuelle perso, autre) : pas d'attestation CSS à suivre
    return not (m.get("mut") == "dispense" and m.get("just") in ("cpam", "perso", "autre"))


def _justif_dispense(docs):
    m = (docs or {}).get("mutuelle") or {}
    return m.get("just") if m.get("mut") == "dispense" else None


def _a_un_titre_de_sejour(docs):
    piece = (docs or {}).get("piece") or {}
    types = piece.get("types")
    if isinstance(types, dict) and types:
        return bool(types.get("titre"))
    return piece.get("t") == "titre"


# Documents suivis : (clé de l'échéance, préfixe des paliers en base, libellés)
DOCUMENTS = [
    {"ech": "piece", "prefixe": "", "nom": "titre de séjour", "votre": "votre titre de séjour",
     "le": "le titre de séjour", "nouveau": "votre nouveau titre de séjour (ou le récépissé de demande)",
     "Alerte": "Alerte titre de séjour", "suivi": _a_un_titre_de_sejour,
     "faire": "faire votre demande de renouvellement",
     "fin": "Sans document valable à la date d'échéance, nous ne pourrons malheureusement pas "
            "maintenir votre planning.\n\n",
     "point": "récépissé de demande, nouveau titre"},
    {"ech": "cpam", "prefixe": "css_", "nom": "attestation CSS", "votre": "votre attestation CSS (complémentaire santé solidaire)",
     "le": "l'attestation CSS", "nouveau": "votre nouvelle attestation CSS",
     "Alerte": "Alerte attestation CSS", "suivi": lambda docs: _attestation_perimable(docs),
     "faire": "demander son renouvellement auprès de votre CPAM (ameli.fr)",
     "fin": "",
     "point": "demande de renouvellement, nouvelle attestation"},
    {"ech": "attcpam", "prefixe": "cpam_", "nom": "attestation CPAM", "votre": "votre attestation CPAM (droits à l'assurance maladie)",
     "le": "l'attestation CPAM", "nouveau": "votre nouvelle attestation CPAM",
     "Alerte": "Alerte attestation CPAM", "suivi": lambda docs: _justif_dispense(docs) == "cpam",
     "faire": "la télécharger à nouveau sur votre compte ameli.fr",
     "fin": "",
     "point": "nouvelle attestation"},
]


def _fmt(d):
    return d.strftime("%d/%m/%Y")


# ---- Envoi des mails ----
# Si les variables Microsoft (MS_*) sont renseignées, les mails partent depuis
# la boîte Outlook (Microsoft 365) via Microsoft Graph et apparaissent dans
# « Éléments envoyés ». Sinon, repli sur Brevo comme avant.
MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "")
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
MS_EXPEDITEUR = os.environ.get("MS_EXPEDITEUR", "lea.goetz@aidaux.fr")


def _outlook_actif():
    return bool(MS_TENANT_ID and MS_CLIENT_ID and MS_CLIENT_SECRET)


def _jeton_microsoft():
    r = auth.requests.post(
        f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": MS_CLIENT_ID,
            "client_secret": MS_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=10,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Connexion Microsoft refusée ({r.status_code}) : {r.text}")
    return r.json()["access_token"]


def _envoyer_outlook(destinataire, sujet, corps, bcc=None):
    message = {
        "subject": sujet,
        "body": {"contentType": "Text", "content": corps},
        "toRecipients": [{"emailAddress": {"address": destinataire}}],
    }
    if bcc and bcc.lower() not in (destinataire.lower(), MS_EXPEDITEUR.lower()):
        message["bccRecipients"] = [{"emailAddress": {"address": bcc}}]
    r = auth.requests.post(
        f"https://graph.microsoft.com/v1.0/users/{MS_EXPEDITEUR}/sendMail",
        headers={"Authorization": f"Bearer {_jeton_microsoft()}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": True},
        timeout=15,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Outlook a refusé l'envoi ({r.status_code}) : {r.text}")


def _envoyer_brevo(destinataire, sujet, corps, bcc=None):
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


def _envoyer(destinataire, sujet, corps, bcc=None):
    if _outlook_actif():
        _envoyer_outlook(destinataire, sujet, corps, bcc)
    else:
        _envoyer_brevo(destinataire, sujet, corps, bcc)


def _mail_salarie(s, fin, jours, palier, doc=DOCUMENTS[0]):
    prenom = s["nom"].split()[-1].capitalize() if s["nom"].split() else ""
    delai = "dans 3 mois" if palier.endswith("relance_3m") else "dans 1 mois"
    sujet = f"Renouvellement de {doc['votre']}"
    if palier.endswith("relance_1m"):
        sujet = f"2e rappel : renouvellement de {doc['votre']}"
    v = doc["votre"]
    corps = (
        f"Bonjour {prenom},\n\n"
        f"{v[0].upper() + v[1:]} arrive à échéance {delai}, le {_fmt(fin)}.\n\n"
        f"Pensez à {doc['faire']} dès maintenant si ce n'est pas déjà fait. "
        f"Dès que vous avez {doc['nouveau']}, "
        "merci de nous en transmettre une copie.\n\n"
        f"{doc['fin']}"
        "Pour toute question, n'hésitez pas à contacter le service administratif.\n\n"
        "Bonne journée,\nLe service administratif AID'Aux"
    )
    return sujet, corps


def _mail_alerte(s, fin, jours, doc=DOCUMENTS[0]):
    if jours < 0:
        quand = f"est EXPIRÉ depuis le {_fmt(fin)}"
    elif jours == 0:
        quand = "expire AUJOURD'HUI"
    else:
        quand = f"expire dans {jours} jours, le {_fmt(fin)}"
    sujet = f"⚠️ {doc['Alerte']} : {s['nom']} ({s['agence']})"
    corps = (
        "Bonjour Marie,\n\n"
        f"{doc['le'][0].upper() + doc['le'][1:]} de {s['nom']} (agence {s['agence']}) {quand}.\n\n"
        f"Aucune nouvelle date de validité n'a été enregistrée pour {doc['le']} dans Aid'Aux Dossiers. "
        f"{'Les deux relances par mail ont été envoyées au salarié.' if s.get('email') else 'Aucune adresse mail n’est renseignée pour ce salarié : il n’a pas reçu de relance.'}\n\n"
        f"Merci de faire le point avec lui/elle rapidement ({doc['point']}).\n\n"
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
            for s, doc in ((s, doc) for s in salaries for doc in DOCUMENTS):
                if not doc["suivi"](s["docs"]):
                    continue
                e = (s["ech"] or {}).get(doc["ech"]) or {}
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
                pre = doc["prefixe"]
                cur.execute(
                    "SELECT 1 FROM dossiers_relances WHERE salarie_id=%s AND date_validite=%s AND palier=%s",
                    (s["id"], fin, pre + code),
                )
                if cur.fetchone():
                    continue
                try:
                    if code == "alerte_15j":
                        dest = ALERTE_EMAIL
                        sujet, corps = _mail_alerte(s, fin, jours, doc)
                        _envoyer(dest, sujet, corps)
                        news = (f"Alerte envoyée à Marie : {doc['nom']} de {s['nom']} "
                                f"{'expiré' if jours < 0 else 'expire le ' + _fmt(fin)}")
                    elif s.get("email"):
                        dest = s["email"].strip()
                        sujet, corps = _mail_salarie(s, fin, jours, code, doc)
                        _envoyer(dest, sujet, corps, bcc=COPIE_RH or None)
                        num = "1re" if code == "relance_3m" else "2e"
                        news = f"{num} relance {doc['nom']} envoyée à {s['nom']}"
                        _noter_relance(cur, s["id"], aujourdhui,
                                       f"Relance automatique : {doc['nom']} ({num} relance, fin le {_fmt(fin)})")
                    else:
                        dest = None
                        num = "1re" if code == "relance_3m" else "2e"
                        news = f"{num} relance {doc['nom']} de {s['nom']} non envoyée : pas d’email renseigné"
                except Exception as err:  # on continue avec les autres salariés
                    rapport.append({"salarie": s["nom"], "palier": pre + code, "erreur": str(err)})
                    continue
                # On marque ce palier (et les paliers moins urgents) comme traités
                for c, n in PALIERS:
                    if n >= dict(PALIERS)[code]:
                        cur.execute(
                            """INSERT INTO dossiers_relances (salarie_id, date_validite, palier, envoye_a)
                               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                            (s["id"], fin, pre + c, dest if c == code else None),
                        )
                cur.execute(
                    "INSERT INTO dossiers_feed (texte, couleur, auteur) VALUES (%s, %s, %s)",
                    (news[:300], "coral", "relance automatique"),
                )
                conn.commit()
                rapport.append({"salarie": s["nom"], "palier": pre + code, "envoye_a": dest, "jours": jours})
    finally:
        conn.close()
    rapport += lancer_rappels_medecine(aujourdhui)
    return rapport


# ---- Rappel visite médecine du travail : 7 jours avant le RDV ----
# Rappels de visite médicale : uniquement quand un RDV est pris, 15 jours avant puis la veille
RAPPELS_MEDECINE = [("medecine_1j", 1), ("medecine_15j", 15)]  # du plus urgent au moins urgent


def _mail_medecine(s, rdv, palier="medecine_15j"):
    prenom = s["nom"].split()[-1].capitalize() if s["nom"].split() else ""
    demain = palier == "medecine_1j"
    sujet = "Rappel : votre visite médicale du travail demain" if demain else "Rappel : votre visite médicale du travail"
    corps = (
        f"Bonjour {prenom},\n\n"
        + (f"Nous vous rappelons que vous avez rendez-vous demain, le {_fmt(rdv)}, pour votre visite médicale du travail.\n\n"
           if demain else
           f"Nous vous rappelons que vous avez rendez-vous pour votre visite médicale du travail le {_fmt(rdv)}.\n\n") +
        "Merci de bien vous y présenter. En cas d'empêchement, prévenez le service administratif "
        "au plus vite afin de reprogrammer le rendez-vous.\n\n"
        "Bonne journée,\nLe service administratif AID'Aux"
    )
    return sujet, corps


def lancer_rappels_medecine(aujourdhui=None):
    aujourdhui = aujourdhui or date.today()
    rapport = []
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, nom, agence, email, ech FROM dossiers_salaries WHERE actif")
            for s in cur.fetchall():
                e = (s["ech"] or {}).get("medecine") or {}
                if e.get("nc") or not e.get("rdv") or not (s.get("email") or "").strip():
                    continue
                try:
                    rdv = date.fromisoformat(e["rdv"][:10])
                except ValueError:
                    continue
                jours = (rdv - aujourdhui).days
                if jours < 0:
                    continue
                # palier dû : la veille (0-1 jour) ou 15 jours avant (2-15 jours) ; rien au-delà de 15 jours
                palier = next((c for c, n in RAPPELS_MEDECINE if jours <= n), None)
                if not palier:
                    continue
                cur.execute(
                    "SELECT 1 FROM dossiers_relances WHERE salarie_id=%s AND date_validite=%s AND palier=%s",
                    (s["id"], rdv, palier),
                )
                if cur.fetchone():
                    continue
                dest = s["email"].strip()
                try:
                    sujet, corps = _mail_medecine(s, rdv, palier)
                    _envoyer(dest, sujet, corps, bcc=COPIE_RH or None)
                except Exception as err:
                    rapport.append({"salarie": s["nom"], "palier": palier, "erreur": str(err)})
                    continue
                # le rappel des 15 jours est marqué fait aussi quand on envoie directement celui de la veille
                for c, n in RAPPELS_MEDECINE:
                    if n >= dict(RAPPELS_MEDECINE)[palier]:
                        cur.execute(
                            """INSERT INTO dossiers_relances (salarie_id, date_validite, palier, envoye_a)
                               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                            (s["id"], rdv, c, dest if c == palier else None),
                        )
                quand = "la veille" if palier == "medecine_1j" else "15 jours avant"
                _noter_relance(cur, s["id"], aujourdhui,
                               f"Rappel automatique ({quand}) : visite médecine du travail du {_fmt(rdv)}")
                cur.execute(
                    "INSERT INTO dossiers_feed (texte, couleur, auteur) VALUES (%s, %s, %s)",
                    (f"Rappel visite médicale du {_fmt(rdv)} ({quand}) envoyé à {s['nom']}"[:300], "coral",
                     "relance automatique"),
                )
                conn.commit()
                rapport.append({"salarie": s["nom"], "palier": palier, "envoye_a": dest, "jours": jours})
    finally:
        conn.close()
    return rapport


# ---- Historique des mails de rappel envoyés (écran « Mails envoyés ») ----
def _libelle_palier(p):
    doc = "Attestation CSS" if p.startswith("css_") else "Attestation CPAM" if p.startswith("cpam_") else "Médecine du travail" if p.startswith("medecine") else "Titre de séjour"
    code = p.replace("css_", "").replace("cpam_", "")
    quoi = {"relance_3m": "1re relance (3 mois avant)", "relance_1m": "2e relance (1 mois avant)",
            "alerte_15j": "Alerte interne (15 jours avant)", "medecine_7j": "Rappel du RDV (7 jours avant)",
            "medecine_15j": "Rappel du RDV (15 jours avant)", "medecine_1j": "Rappel du RDV (la veille)"}.get(code, code)
    return doc, quoi


@bp.route("/api/relances/historique")
def historique_relances():
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT r.id, r.salarie_id, r.date_validite, r.palier, r.envoye_a,
                          to_char(r.created_at AT TIME ZONE 'Europe/Paris', 'YYYY-MM-DD HH24:MI') AS le,
                          COALESCE(s.nom, '(salarié supprimé)') AS nom
                   FROM dossiers_relances r LEFT JOIN dossiers_salaries s ON s.id = r.salarie_id
                   WHERE r.envoye_a IS NOT NULL
                   ORDER BY r.created_at DESC, r.id DESC LIMIT 2000"""
            )
            lignes = []
            for r in cur.fetchall():
                doc, quoi = _libelle_palier(r["palier"])
                lignes.append({"id": r["id"], "sid": r["salarie_id"], "nom": r["nom"], "le": r["le"],
                               "doc": doc, "quoi": quoi, "a": r["envoye_a"],
                               "echeance": r["date_validite"].isoformat() if r["date_validite"] else ""})
    finally:
        conn.close()
    return jsonify({"ok": True, "lignes": lignes})


@bp.route("/api/cron/relances")
def cron_relances():
    # Vercel envoie automatiquement « Authorization: Bearer <CRON_SECRET> »
    if not CRON_SECRET or request.headers.get("Authorization") != f"Bearer {CRON_SECRET}":
        return jsonify({"ok": False, "erreur": "non autorisé"}), 401
    emails = None
    try:  # récupère d'abord les emails manquants depuis Ximi (sans bloquer les relances)
        import ximi_emails
        emails = ximi_emails.remplir_emails_vides()
    except Exception as e:
        emails = f"erreur Ximi : {e}"
    traites = lancer_relances()
    try:  # puis note dans le Journal Ximi ce qui manque encore (seulement si la liste a changé)
        import ximi_emails
        journal = ximi_emails.synchro_manquants()
    except Exception as e:
        journal = f"erreur Ximi : {e}"
    return jsonify({"ok": True, "envoi": "outlook" if _outlook_actif() else "brevo",
                    "emails_ximi": emails, "traites": traites, "journal_ximi": journal})
