import datetime
import io
import json
import os
import re
import unicodedata

from flask import Flask, Response, abort, send_file, jsonify, render_template, request, send_from_directory, session
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
# Catégorie de salarié : intervenant terrain (par défaut) ou personnel administratif
CATEGORIES = {"terrain", "administratif", "mixte"}  # mixte = terrain + administratif
# Salariés administratifs connus (noms de famille), classés automatiquement une
# fois. Ensuite la catégorie se change à la main dans la fiche du salarié.
ADMINISTRATIFS_INITIAUX = ["goetz", "mischel", "pouillaude", "suplon", "soulliez", "fux"]
CHAMPS_JSON = ("docs", "classeur", "dates", "ech", "vie")


def _mots(texte):
    t = unicodedata.normalize("NFD", str(texte or "")).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z]+", t)


def _est_administratif_connu(nom, agence):
    if agence == "Personnel administratif":
        return True
    mots = _mots(nom)
    return any(famille in mots for famille in ADMINISTRATIFS_INITIAUX)


def _classer_categories(cur):
    # 1) Salariés jamais classés : terrain, sauf les administratifs connus.
    cur.execute("SELECT id, nom, agence FROM dossiers_salaries WHERE categorie IS NULL")
    for sid, nom, agence in cur.fetchall():
        cat = "administratif" if _est_administratif_connu(nom, agence) else "terrain"
        cur.execute("UPDATE dossiers_salaries SET categorie = %s WHERE id = %s", (cat, sid))
    # 2) Une seule fois (repère dans dossiers_meta) : les administratifs connus
    #    passent en « administratif », même s'ils avaient déjà été classés terrain.
    #    Après, un choix fait dans la fiche n'est plus jamais écrasé.
    cur.execute("CREATE TABLE IF NOT EXISTS dossiers_meta (cle TEXT PRIMARY KEY)")
    cur.execute("INSERT INTO dossiers_meta (cle) VALUES ('categories_admin_v2') ON CONFLICT DO NOTHING")
    if cur.rowcount:
        cur.execute("SELECT id, nom, agence FROM dossiers_salaries")
        for sid, nom, agence in cur.fetchall():
            if _est_administratif_connu(nom, agence):
                cur.execute("UPDATE dossiers_salaries SET categorie = 'administratif' WHERE id = %s", (sid,))


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
            # Éléments manquants (calculés par l'appli) et dernière liste notée dans le Journal Ximi
            cur.execute("ALTER TABLE dossiers_salaries ADD COLUMN IF NOT EXISTS manquants JSONB")
            cur.execute("ALTER TABLE dossiers_salaries ADD COLUMN IF NOT EXISTS manquants_ximi JSONB")
            # Catégorie : 'terrain' ou 'administratif' (NULL = pas encore classé)
            cur.execute("ALTER TABLE dossiers_salaries ADD COLUMN IF NOT EXISTS categorie TEXT")
            _classer_categories(cur)
            # Entretiens du personnel administratif : PDF importé par salarié,
            # par type (annuel / professionnel) et par année, avec le texte écrit dessus.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS dossiers_pdf (
                    id          SERIAL PRIMARY KEY,
                    salarie_id  INTEGER NOT NULL REFERENCES dossiers_salaries(id) ON DELETE CASCADE,
                    type        TEXT NOT NULL,
                    annee       INTEGER NOT NULL,
                    nom_fichier TEXT,
                    contenu     BYTEA NOT NULL,
                    notes       JSONB NOT NULL DEFAULT '[]'::jsonb,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_by  TEXT
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS dossiers_pdf_salarie ON dossiers_pdf (salarie_id)")
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
    d["categorie"] = d.get("categorie") or "terrain"
    for k in ("created_at", "updated_at", "manquants_ximi"):
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
    categorie = body.get("categorie") if body.get("categorie") in CATEGORIES else "terrain"
    if not nom:
        return jsonify({"ok": False, "erreur": "Indiquez le nom du salarié"}), 400
    if agence not in AGENCES:
        return jsonify({"ok": False, "erreur": "Agence inconnue"}), 400
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO dossiers_salaries (nom, agence, embauche, phase, categorie, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (nom, agence, embauche, phase, categorie, session.get("user_email")),
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
    if body.get("categorie") in CATEGORIES:
        champs.append("categorie = %s")
        valeurs.append(body["categorie"])
    if "embauche" in body:
        champs.append("embauche = %s")
        valeurs.append(body["embauche"] or None)
    if isinstance(body.get("manquants"), list):
        champs.append("manquants = %s")
        valeurs.append(Json([str(x)[:200] for x in body["manquants"][:100]]))
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


# ---- Éléments manquants : mise à jour groupée (à l'ouverture de l'appli) ----
@app.route("/api/manquants", methods=["POST"])
def api_manquants():
    lignes = (request.get_json(silent=True) or {}).get("lignes") or {}
    if not isinstance(lignes, dict):
        return jsonify({"ok": False, "erreur": "format invalide"}), 400
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            for sid, liste in list(lignes.items())[:500]:
                if isinstance(liste, list):
                    cur.execute("UPDATE dossiers_salaries SET manquants = %s WHERE id = %s",
                                (Json([str(x)[:200] for x in liste[:100]]), int(sid)))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


# ---- Entretiens du personnel administratif (PDF) ----
TYPES_PDF = {"annuel": "Entretien annuel", "pro": "Entretien professionnel"}
TAILLE_MAX_PDF = 4 * 1024 * 1024  # Vercel refuse les envois de plus de 4,5 Mo


def _pdf_info(r):
    return {"id": r["id"], "type": r["type"], "annee": r["annee"], "nom": r["nom_fichier"] or "",
            "maj": r["updated_at"].isoformat() if r.get("updated_at") else ""}


@app.route("/api/salaries/<int:sid>/pdf")
def api_pdf_liste(sid):
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT id, type, annee, nom_fichier, updated_at FROM dossiers_pdf
                   WHERE salarie_id = %s ORDER BY annee DESC, id DESC""",
                (sid,),
            )
            return jsonify({"ok": True, "pdf": [_pdf_info(r) for r in cur.fetchall()]})
    finally:
        conn.close()


@app.route("/api/salaries/<int:sid>/pdf", methods=["POST"])
def api_pdf_importer(sid):
    f = request.files.get("fichier")
    typ = request.form.get("type")
    try:
        annee = int(request.form.get("annee") or 0)
    except ValueError:
        annee = 0
    if typ not in TYPES_PDF:
        return jsonify({"ok": False, "erreur": "Type d'entretien inconnu"}), 400
    if not 2000 <= annee <= 2100:
        return jsonify({"ok": False, "erreur": "Année invalide"}), 400
    if not f:
        return jsonify({"ok": False, "erreur": "Aucun fichier"}), 400
    contenu = f.read()
    if not contenu.startswith(b"%PDF"):
        return jsonify({"ok": False, "erreur": "Le fichier n'est pas un PDF"}), 400
    if len(contenu) > TAILLE_MAX_PDF:
        return jsonify({"ok": False, "erreur": "PDF trop lourd (4 Mo maximum)"}), 400
    conn = auth._conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT nom FROM dossiers_salaries WHERE id = %s", (sid,))
            sal = cur.fetchone()
            if not sal:
                return jsonify({"ok": False, "erreur": "Salarié introuvable"}), 404
            cur.execute(
                """INSERT INTO dossiers_pdf (salarie_id, type, annee, nom_fichier, contenu, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, type, annee, nom_fichier, updated_at""",
                (sid, typ, annee, (f.filename or "")[:200], contenu, session.get("user_email")),
            )
            info = _pdf_info(cur.fetchone())
            _ajouter_news(cur, {"txt": f"{TYPES_PDF[typ]} {annee} de {sal['nom']} importé"})
            feed = _feed(cur)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "pdf": info, "feed": feed})


@app.route("/api/pdf/<int:pid>")
def api_pdf_fichier(pid):
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT contenu FROM dossiers_pdf WHERE id = %s", (pid,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    resp = Response(bytes(row[0]), mimetype="application/pdf")
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.route("/api/pdf/<int:pid>/notes")
def api_pdf_notes(pid):
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT notes FROM dossiers_pdf WHERE id = %s", (pid,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"ok": False, "erreur": "Document introuvable"}), 404
    return jsonify({"ok": True, "notes": row[0] or []})


@app.route("/api/pdf/<int:pid>/notes", methods=["PUT"])
def api_pdf_notes_maj(pid):
    notes = (request.get_json(silent=True) or {}).get("notes")
    if not isinstance(notes, list) or len(notes) > 2000:
        return jsonify({"ok": False, "erreur": "Notes invalides"}), 400
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dossiers_pdf SET notes = %s, updated_at = now(), updated_by = %s WHERE id = %s",
                (Json(notes), session.get("user_email"), pid),
            )
            if cur.rowcount == 0:
                return jsonify({"ok": False, "erreur": "Document introuvable"}), 404
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/pdf/<int:pid>", methods=["DELETE"])
def api_pdf_supprimer(pid):
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dossiers_pdf WHERE id = %s", (pid,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


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


# Modèles Word des entretiens (fichiers .docx à la racine du dépôt), pré-remplis
# avec les infos du salarié quand on passe ?sid=<id> — accès réservé aux comptes connectés
import modeles_word  # noqa: E402

MODELES = set(modeles_word.MODELES)


@app.route("/modeles/<nom>")
def modele(nom):
    if nom not in MODELES:
        abort(404)
    chemin = os.path.join(app.root_path, nom)
    sid = request.args.get("sid", type=int)
    if sid:
        try:
            conn = auth._conn()
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT nom, agence, embauche, vie FROM dossiers_salaries WHERE id = %s", (sid,))
                    row = cur.fetchone()
            finally:
                conn.close()
            if row:
                ent = (row.get("vie") or {}).get("ent") or {}
                contenu = modeles_word.remplir(chemin, nom, row["nom"], row["agence"], row["embauche"], ent)
                return send_file(
                    io.BytesIO(contenu),
                    as_attachment=True,
                    download_name=modeles_word.nom_fichier(nom, row["nom"]),
                    mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
        except Exception:
            pass  # en cas de souci : on donne le modèle vierge plutôt qu'une erreur
    return send_from_directory(app.root_path, nom, as_attachment=True)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.root_path, "favicon.ico")


@app.route("/logo.png")
def logo():
    return send_from_directory(app.root_path, "logo.png")


if __name__ == "__main__":
    app.run(debug=True)
