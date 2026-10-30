# auth.py — Système de comptes partagé pour les applications AID'Aux
#
# Fournit :
#   - une table de comptes (email + mot de passe chiffré) et une table de jetons
#     de réinitialisation, dans une base PostgreSQL (Neon) ;
#   - l'inscription réservée à une liste d'emails autorisés ;
#   - un flux « définir / réinitialiser mon mot de passe » par email ;
#   - un blueprint Flask avec les pages /login, /logout,
#     /mot-de-passe-oublie et /definir-mot-de-passe/<token> ;
#   - un garde `proteger(app)` qui exige la connexion sur toutes les pages.
#
# Conçu pour être copié tel quel dans les autres applis Flask AID'Aux.
# La seule dépendance externe est `requests` (déjà présent) ; le hachage des
# mots de passe utilise Werkzeug (fourni avec Flask).

import os
import secrets
from datetime import datetime, timedelta

import psycopg2
import requests
from flask import (
    Blueprint,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

# ───────────────────────── Configuration ─────────────────────────

# Emails autorisés à posséder un compte. Modifiable sans toucher au code via la
# variable d'environnement AUTH_ALLOWED_EMAILS (adresses séparées par des virgules).
_EMAILS_PAR_DEFAUT = [
    "magali.metz@aidaux.fr",
    "magalie.fux@aidaux.fr",
    "lea.goetz@aidaux.fr",
    "marie.mischel@aidaux.fr",
    "tatiana.suplon@aidaux.fr",
    "jennifer.soulliez@aidaux.fr",
]


def _emails_autorises():
    brut = os.environ.get("AUTH_ALLOWED_EMAILS", "").strip()
    if brut:
        return [e.strip().lower() for e in brut.split(",") if e.strip()]
    return [e.lower() for e in _EMAILS_PAR_DEFAUT]


EMAILS_AUTORISES = _emails_autorises()

# Base de comptes : partagée entre toutes les applis. On réutilise DATABASE_URL
# par défaut, mais on peut pointer vers une base dédiée via AUTH_DATABASE_URL.
AUTH_DATABASE_URL = os.environ.get("AUTH_DATABASE_URL") or os.environ.get("DATABASE_URL")

APP_NOM = os.environ.get("AUTH_APP_NOM", "AID'Aux")
TOKEN_TTL_HEURES = int(os.environ.get("AUTH_TOKEN_TTL_HEURES", "2"))
LONGUEUR_MDP_MIN = 8

# Envoi d'emails (Brevo) — mêmes variables que le reste de l'appli.
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
EXPEDITEUR_EMAIL = os.environ.get("EXPEDITEUR_EMAIL", "lea.goetz@aidaux.fr")
EXPEDITEUR_NOM = os.environ.get("EXPEDITEUR_NOM", "AID'Aux")


# ───────────────────────── Base de données ─────────────────────────


def _conn():
    if not AUTH_DATABASE_URL:
        raise RuntimeError(
            "AUTH_DATABASE_URL (ou DATABASE_URL) manquant : impossible de gérer les comptes."
        )
    return psycopg2.connect(AUTH_DATABASE_URL)


def init_auth_db():
    """Crée les tables de comptes si elles n'existent pas."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS comptes (
            email             TEXT PRIMARY KEY,
            nom               TEXT,
            mot_de_passe_hash TEXT,
            cree_at           TIMESTAMP DEFAULT NOW(),
            maj_at            TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS comptes_tokens (
            token      TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            expire_at  TIMESTAMP NOT NULL,
            utilise_at TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def email_autorise(email):
    return (email or "").strip().lower() in EMAILS_AUTORISES


# ───────────────────────── Emails ─────────────────────────


def _envoyer_mail(destinataire, sujet, corps):
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY non configurée : envoi d'email impossible.")
    reponse = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "sender": {"name": EXPEDITEUR_NOM, "email": EXPEDITEUR_EMAIL},
            "to": [{"email": destinataire}],
            "subject": sujet,
            "textContent": corps,
        },
        timeout=10,
    )
    if reponse.status_code >= 300:
        raise RuntimeError(f"Brevo a refusé l'envoi ({reponse.status_code}) : {reponse.text}")


def envoyer_lien_mot_de_passe(email, base_url):
    """Crée un jeton et envoie par email le lien de définition du mot de passe."""
    email = email.strip().lower()
    token = secrets.token_urlsafe(32)
    expire = datetime.utcnow() + timedelta(hours=TOKEN_TTL_HEURES)
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO comptes_tokens (token, email, expire_at) VALUES (%s, %s, %s)",
        (token, email, expire),
    )
    conn.commit()
    conn.close()

    lien = base_url.rstrip("/") + "/definir-mot-de-passe/" + token
    corps = (
        f"Bonjour,\n\n"
        f"Vous avez demandé à définir ou réinitialiser votre mot de passe pour "
        f"l'application {APP_NOM}.\n\n"
        f"Cliquez sur ce lien (valable {TOKEN_TTL_HEURES} h) pour choisir votre "
        f"mot de passe :\n{lien}\n\n"
        f"Si vous n'êtes pas à l'origine de cette demande, ignorez simplement ce "
        f"message.\n\n"
        f"— {APP_NOM}\n"
    )
    _envoyer_mail(email, f"{APP_NOM} — définir votre mot de passe", corps)


def _email_du_token(token):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT email, expire_at, utilise_at FROM comptes_tokens WHERE token = %s",
        (token,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    email, expire_at, utilise_at = row
    if utilise_at is not None:
        return None
    if expire_at < datetime.utcnow():
        return None
    return email


def definir_mot_de_passe(token, mot_de_passe):
    """Valide le jeton et enregistre le mot de passe (chiffré). Renvoie l'email ou None."""
    email = _email_du_token(token)
    if not email:
        return None
    h = generate_password_hash(mot_de_passe)
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO comptes (email, mot_de_passe_hash, maj_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (email)
        DO UPDATE SET mot_de_passe_hash = EXCLUDED.mot_de_passe_hash, maj_at = NOW()
        """,
        (email, h),
    )
    cur.execute("UPDATE comptes_tokens SET utilise_at = NOW() WHERE token = %s", (token,))
    conn.commit()
    conn.close()
    return email


def verifier_login(email, mot_de_passe):
    email = (email or "").strip().lower()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT mot_de_passe_hash FROM comptes WHERE email = %s", (email,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return False
    return check_password_hash(row[0], mot_de_passe or "")


# ───────────────────────── Pages (blueprint) ─────────────────────────

bp = Blueprint("auth", __name__)

_STYLE = """
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'Segoe UI',sans-serif; background:#f0f2f5; display:flex;
         justify-content:center; align-items:center; min-height:100vh; padding:20px; }
  .card { background:#fff; padding:40px; border-radius:12px;
          box-shadow:0 4px 20px rgba(0,0,0,0.1); width:100%; max-width:380px; }
  h1 { color:#1c3885; font-size:22px; margin-bottom:6px; }
  p.sub { color:#666; font-size:14px; margin-bottom:22px; }
  label { font-size:13px; color:#444; display:block; margin-bottom:6px; margin-top:4px; }
  input { width:100%; padding:10px 14px; border:1px solid #ddd; border-radius:8px;
          font-size:14px; margin-bottom:16px; }
  button { width:100%; padding:11px; background:#1c3885; color:#fff; border:none;
           border-radius:8px; font-size:15px; cursor:pointer; }
  button:hover { background:#00aaaa; }
  .error { background:#fdecea; color:#c0392b; padding:10px; border-radius:6px;
           font-size:13px; margin-bottom:16px; }
  .ok { background:#eafaf1; color:#1e8449; padding:10px; border-radius:6px;
        font-size:13px; margin-bottom:16px; }
  .links { margin-top:18px; font-size:13px; text-align:center; }
  .links a { color:#1c3885; text-decoration:none; }
  .links a:hover { text-decoration:underline; }
"""

_PAGE = """<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ titre }} — {{ app_nom }}</title><style>""" + _STYLE + """</style></head>
<body><div class="card">
  <h1>{{ app_nom }}</h1>
  <p class="sub">{{ sous_titre }}</p>
  {% if erreur %}<div class="error">{{ erreur }}</div>{% endif %}
  {% if succes %}<div class="ok">{{ succes }}</div>{% endif %}
  {{ corps|safe }}
</div></body></html>"""


def _rendre(titre, sous_titre, corps, erreur=None, succes=None):
    return render_template_string(
        _PAGE,
        titre=titre,
        app_nom=APP_NOM,
        sous_titre=sous_titre,
        corps=corps,
        erreur=erreur,
        succes=succes,
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    erreur = None
    if request.method == "POST":
        email = request.form.get("email", "")
        mdp = request.form.get("password", "")
        if verifier_login(email, mdp):
            session["user_email"] = email.strip().lower()
            nxt = request.args.get("next") or "/"
            # N'autorise qu'une redirection interne (évite une redirection ouverte).
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"
            return redirect(nxt)
        erreur = "Email ou mot de passe incorrect."
    corps = """
    <form method="POST">
      <label>Adresse email</label>
      <input type="email" name="email" autofocus required>
      <label>Mot de passe</label>
      <input type="password" name="password" required>
      <button type="submit">Se connecter</button>
    </form>
    <div class="links"><a href="/mot-de-passe-oublie">Mot de passe oublié / première connexion</a></div>
    """
    return _rendre("Connexion", "Connectez-vous pour accéder à l'application", corps, erreur=erreur)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/mot-de-passe-oublie", methods=["GET", "POST"])
def oubli():
    erreur = None
    succes = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email_autorise(email):
            erreur = "Cette adresse n'est pas autorisée. Contactez Léa Goetz (lea.goetz@aidaux.fr)."
        else:
            try:
                envoyer_lien_mot_de_passe(email, request.url_root)
                succes = "Un email vient de vous être envoyé avec un lien pour définir votre mot de passe. Pensez à vérifier vos spams."
            except Exception as e:
                erreur = "L'envoi de l'email a échoué : " + str(e)
    corps = """
    <form method="POST">
      <label>Votre adresse email professionnelle</label>
      <input type="email" name="email" autofocus required>
      <button type="submit">Recevoir le lien</button>
    </form>
    <div class="links"><a href="/login">Retour à la connexion</a></div>
    """
    return _rendre(
        "Mot de passe",
        "Recevez un lien par email pour définir votre mot de passe",
        corps,
        erreur=erreur,
        succes=succes,
    )


@bp.route("/definir-mot-de-passe/<token>", methods=["GET", "POST"])
def definir(token):
    email = _email_du_token(token)
    if not email:
        corps = '<div class="links"><a href="/mot-de-passe-oublie">Demander un nouveau lien</a></div>'
        return _rendre(
            "Lien invalide",
            "Ce lien est invalide ou a expiré.",
            corps,
            erreur="Le lien n'est plus valable (expiré ou déjà utilisé).",
        )
    erreur = None
    if request.method == "POST":
        mdp = request.form.get("password", "")
        mdp2 = request.form.get("password2", "")
        if len(mdp) < LONGUEUR_MDP_MIN:
            erreur = f"Le mot de passe doit contenir au moins {LONGUEUR_MDP_MIN} caractères."
        elif mdp != mdp2:
            erreur = "Les deux mots de passe ne correspondent pas."
        else:
            if definir_mot_de_passe(token, mdp):
                corps = '<div class="links"><a href="/login">Se connecter</a></div>'
                return _rendre(
                    "Mot de passe enregistré",
                    "Votre mot de passe a bien été enregistré.",
                    corps,
                    succes="C'est fait ! Vous pouvez maintenant vous connecter.",
                )
            erreur = "Le lien n'est plus valable. Redemandez-en un."
    corps = f"""
    <form method="POST">
      <label>Compte : {email}</label>
      <label>Nouveau mot de passe (min. {LONGUEUR_MDP_MIN} caractères)</label>
      <input type="password" name="password" autofocus required>
      <label>Confirmer le mot de passe</label>
      <input type="password" name="password2" required>
      <button type="submit">Enregistrer</button>
    </form>
    """
    return _rendre("Définir le mot de passe", "Choisissez votre mot de passe", corps, erreur=erreur)


# ───────────────────────── Garde de connexion ─────────────────────────

_ENDPOINTS_PUBLICS = {"auth.login", "auth.logout", "auth.oubli", "auth.definir", "static", "manifest", "service_worker"}


def est_connecte():
    return bool(session.get("user_email"))


def proteger(app, endpoints_publics=()):
    """Installe un garde : toute page hors liste publique exige la connexion."""
    publics = _ENDPOINTS_PUBLICS | set(endpoints_publics)

    @app.before_request
    def _exiger_connexion():
        if request.endpoint is None or request.endpoint in publics:
            return
        if not est_connecte():
            return redirect(url_for("auth.login", next=request.path))
