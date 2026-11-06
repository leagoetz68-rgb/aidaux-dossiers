import json
import os

from flask import Flask, Response, jsonify, render_template, request, send_from_directory, session
from psycopg2.extras import Json, RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-moi-en-prod")

# Système de comptes partagé (login individuel par email + mot de passe,
# lien "définir mon mot de passe" envoyé par email). Voir auth.py — copié
# tel quel depuis les autres applis AID'Aux, base de comptes commune via
# AUTH_DATABASE_URL (ou DATABASE_URL).
import auth  # noqa: E402

auth.init_auth_db()
app.register_blueprint(auth.bp)

# Relances automatiques des titres de séjour (Cron Vercel, 1x/jour)
import relances  # noqa: E402

app.register_blueprint(relances.bp)
import ximi_emails  # noqa: E402

app.register_blueprint(ximi_emails.bp)
auth.proteger(app, endpoints_publics={"relances.cron_relances"})

AGENCES = ["Cronenbourg", "Vendenheim", "Personnel administratif"]
PHASES = {"arrivee", "poste"}
CHAMPS_JSON = ("docs", "classeur", "dates", "ech", "vie")


# ---- Base de données (même base Neon que les autres applis) ----
def init_db():
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS dossiers_salaries (
                    id         SERIAL PRIMARY KEY,
                    nom        TEXT NOT NULL,
                    agence     TEXT NOT NULL,
                    embauche   DATE,
                    phase      TEXT NOT NULL DEFAULT 'arrivee',
                    docs       JSONB NOT NULL DEFAULT '{}'::jsonb,
                    classeur   JSONB NOT NULL DEFAULT '{}'::jsonb,
                    dates      JSONB NOT NULL DEFAULT '{}'::jsonb,
                    ech        JSONB NOT NULL DEFAULT '{}'::jsonb,
                    actif      BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_by TEXT
                )
                """
            )
            # « La vie du salarié » : avenants, CSS, médecine, entretiens
            cur.execute(
                "ALTER TABLE dossiers_salaries ADD COLUMN IF NOT EXISTS vie JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
            # Archives : salariés partis d'AID'Aux (gardés, consultables, désarchivables)
            cur.execute(
                "ALTER TABLE dossiers_salaries ADD COLUMN IF NOT EXISTS archive BOOLEAN NOT NULL DEFAULT FALSE"
            )
            cur.execute("ALTER TABLE dossiers_salaries ADD COLUMN IF NOT EXISTS archive_le DATE")
            # Fil d'actualité affiché en haut de l'appli
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS dossiers_feed (
                    id         SERIAL PRIMARY KEY,
                    texte      TEXT NOT NULL,
                    couleur    TEXT NOT NULL DEFAULT 'teal',
                    auteur     TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


init_db()
relances.init_db()


def _salarie(row):
    d = dict(row)
    d["embauche"] = d["embauche"].isoformat() if d.get("embauche") else ""
    d["archive_le"] = d["archive_le"].isoformat() if d.get("archive_le") else ""
    for k in ("created_at", "updated_at"):
        d.pop(k, None)
    return d


def _feed(cur, limite=30):
    cur.execute(
        """SELECT texte AS txt, couleur AS c,
                  to_char(created_at AT TIME ZONE 'Europe/Paris', 'YYYY-MM-DD') AS t
           FROM dossiers_feed ORDER BY created_at DESC, id DESC LIMIT %s""",
        (limite,),
    )
    return [dict(r) for r in cur.fetchall()]


def _ajouter_news(cur, news):
    if not news:
        return
    texte = str(news.get("txt") or "").strip()[:300]
    if not texte:
        return
    couleur = "coral" if news.get("c") == "coral" else "teal"
    cur.execute(
        "INSERT INTO dossiers_feed (texte, couleur, auteur) VALUES (%s, %s, %s)",
        (texte, couleur, session.get("user_email")),
    )
    # On ne garde que les 200 dernières nouvelles
    cur.execute(
        """DELETE FROM dossiers_feed WHERE id NOT IN
           (SELECT id FROM dossiers_feed ORDER BY created_at DESC, id DESC LIMIT 200)"""
    )


# ---- API ----
@app.route("/api/etat")
def api_etat():
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM dossiers_salaries WHERE actif ORDER BY nom")
            salaries = [_salarie(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM dossiers_salaries WHERE NOT actif AND archive ORDER BY nom")
            archives = [_salarie(r) for r in cur.fetchall()]
            feed = _feed(cur)
    finally:
        conn.close()
    return jsonify({"ok": True, "salaries": salaries, "archives": archives, "feed": feed, "agences": AGENCES})


@app.route("/api/salaries", methods=["POST"])
def api_creer():
    body = request.get_json(silent=True) or {}
    nom = (body.get("nom") or "").strip()
    agence = body.get("agence")
    phase = body.get("phase") if body.get("phase") in PHASES else "arrivee"
    embauche = body.get("embauche") or None
    if not nom:
        return jsonify({"ok": False, "erreur": "Indiquez le nom du salarié"}), 400
    if agence not in AGENCES:
        return jsonify({"ok": False, "erreur": "Agence inconnue"}), 400
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO dossiers_salaries (nom, agence, embauche, phase, updated_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (nom, agence, embauche, phase, session.get("user_email")),
            )
            salarie = _salarie(cur.fetchone())
            if phase == "arrivee":
                _ajouter_news(cur, {"txt": f"{nom} a rejoint l’agence de {agence}"})
            feed = _feed(cur)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "salarie": salarie, "feed": feed})


@app.route("/api/salaries/<int:sid>", methods=["PUT"])
def api_maj(sid):
    body = request.get_json(silent=True) or {}
    champs, valeurs = [], []
    for k in CHAMPS_JSON:
        if isinstance(body.get(k), dict):
            champs.append(f"{k} = %s")
            valeurs.append(Json(body[k]))
    if body.get("phase") in PHASES:
        champs.append("phase = %s")
        valeurs.append(body["phase"])
    if (body.get("nom") or "").strip():
        champs.append("nom = %s")
        valeurs.append(body["nom"].strip())
    if body.get("agence") in AGENCES:
        champs.append("agence = %s")
        valeurs.append(body["agence"])
    if "embauche" in body:
        champs.append("embauche = %s")
        valeurs.append(body["embauche"] or None)
    if "email" in body:
        champs.append("email = %s")
        valeurs.append((body["email"] or "").strip().lower() or None)
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if champs:
                cur.execute(
                    f"""UPDATE dossiers_salaries SET {", ".join(champs)},
                        updated_at = now(), updated_by = %s WHERE id = %s AND actif""",
                    (*valeurs, session.get("user_email"), sid),
                )
                if cur.rowcount == 0:
                    return jsonify({"ok": False, "erreur": "Salarié introuvable"}), 404
            _ajouter_news(cur, body.get("news"))
            feed = _feed(cur)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "feed": feed})


@app.route("/api/salaries/<int:sid>", methods=["DELETE"])
def api_retirer(sid):
    # On n'efface rien : le salarié est simplement retiré des listes (sortie).
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dossiers_salaries SET actif = FALSE, updated_at = now(), updated_by = %s WHERE id = %s",
                (session.get("user_email"), sid),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/salaries/<int:sid>/archive", methods=["POST"])
def api_archiver(sid):
    # archive=true : départ d'AID'Aux (sorti des listes et des relances, gardé
    # dans « Archives ») ; archive=false : retour dans les listes.
    archiver = bool((request.get_json(silent=True) or {}).get("archive", True))
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """UPDATE dossiers_salaries SET actif = %s, archive = %s,
                   archive_le = CASE WHEN %s THEN CURRENT_DATE ELSE NULL END,
                   updated_at = now(), updated_by = %s WHERE id = %s RETURNING *""",
                (not archiver, archiver, archiver, session.get("user_email"), sid),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "erreur": "Salarié introuvable"}), 404
            salarie = _salarie(row)
            _ajouter_news(cur, {"txt": f"{salarie['nom']} {'a quitté AID’Aux (archivé)' if archiver else 'est de retour (désarchivé)'}"})
            feed = _feed(cur)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "salarie": salarie, "feed": feed})


# --- Application installable (écran d'accueil iPhone / Android) ---
MANIFEST = {
    "name": "Aid'Aux Dossiers",
    "short_name": "Dossiers",
    "description": "Suivi des dossiers salariés AID'Aux",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#ffffff",
    "theme_color": "#1B2A6B",
    "lang": "fr",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

SERVICE_WORKER = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""


@app.route("/manifest.webmanifest")
def manifest():
    return Response(json.dumps(MANIFEST, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    return Response(SERVICE_WORKER, mimetype="application/javascript", headers={"Cache-Control": "no-cache"})


@app.route("/")
def index():
    return render_template("index.html", user_email=session.get("user_email", ""))


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.root_path, "favicon.ico")


@app.route("/logo.png")
def logo():
    return send_from_directory(app.root_path, "logo.png")


if __name__ == "__main__":
    app.run(debug=True)
