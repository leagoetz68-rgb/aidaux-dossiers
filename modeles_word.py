"""Pré-remplissage des modèles Word d'entretien avec les infos du salarié."""
import datetime
import io
import re
import unicodedata

# Fichier -> (clé d'entretien dans la fiche, titre pour le nom du fichier)
MODELES = {
    "01_Suivi_1_mois.docx": ("m1", "Suivi_1_mois"),
    "02_Suivi_3_mois.docx": ("m3", "Suivi_3_mois"),
    "03_Suivi_6_mois.docx": ("m6", "Suivi_6_mois"),
    "04_Entretien_annuel.docx": ("annuel", "Entretien_annuel"),
    "05_Entretien_parcours_professionnel.docx": ("parcours", "Entretien_parcours_pro"),
}
# Entretien précédent à rappeler : (clé, libellé)
PRECEDENT = {
    "m3": ("m1", "Suivi 1 mois"),
    "m6": ("m3", "Suivi 3 mois"),
    "annuel": ("annuel", "Entretien annuel précédent"),
    "parcours": ("parcours", "Entretien de parcours précédent"),
}


def _fr(iso):
    try:
        y, m, d = str(iso).split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return ""


def _anciennete(embauche, aujourdhui):
    mois = (aujourdhui.year - embauche.year) * 12 + aujourdhui.month - embauche.month
    if aujourdhui.day < embauche.day:
        mois -= 1
    if mois < 0:
        return ""
    ans, m = divmod(mois, 12)
    morceaux = []
    if ans:
        morceaux.append(f"{ans} an" + ("s" if ans > 1 else ""))
    if m or not ans:
        morceaux.append(f"{m} mois")
    return " ".join(morceaux)


def nom_fichier(fichier, nom_salarie):
    titre = MODELES[fichier][1]
    brut = unicodedata.normalize("NFKD", nom_salarie or "").encode("ascii", "ignore").decode()
    brut = re.sub(r"[^A-Za-z0-9]+", "_", brut).strip("_")
    return f"{titre}_{brut}.docx" if brut else f"{titre}.docx"


def remplir(chemin, fichier, nom, agence, embauche, ent):
    """Renvoie le .docx (bytes) avec le tableau d'en-tête pré-rempli.
    embauche : datetime.date ou None ; ent : dict des entretiens de la fiche."""
    from docx import Document

    cle = MODELES[fichier][0]
    ent = ent if isinstance(ent, dict) else {}
    e = ent.get(cle) if isinstance(ent.get(cle), dict) else {}
    valeurs = {"nom": nom or "", "agence": agence or ""}
    if embauche:
        anc = _anciennete(embauche, datetime.date.today())
        valeurs["embauche"] = embauche.strftime("%d/%m/%Y") + (f" ({anc})" if anc else "")
    if e.get("rdv"):
        valeurs["date"] = _fr(e["rdv"])
    if cle in PRECEDENT:
        k, lib = PRECEDENT[cle]
        prec = ent.get(k) if isinstance(ent.get(k), dict) else {}
        if prec.get("le"):
            valeurs["periode"] = f"{lib} : {_fr(prec['le'])}"

    doc = Document(chemin)
    tableau = doc.tables[0]
    for ligne in tableau.rows:
        etiquette = ligne.cells[0].text.strip().lower()
        if etiquette.startswith("nom"):
            v = valeurs.get("nom")
        elif etiquette.startswith("agence"):
            v = valeurs.get("agence")
        elif "embauche" in etiquette:
            v = valeurs.get("embauche")
        elif etiquette.startswith("date et lieu"):
            v = valeurs.get("date")
        elif etiquette.startswith("période") or etiquette.startswith("periode"):
            v = valeurs.get("periode")
        else:
            v = None
        if not v:
            continue
        par = ligne.cells[1].paragraphs[0]
        if par.runs:
            par.runs[-1].text = v  # garde la mise en forme du modèle
        else:
            par.add_run(v)
    sortie = io.BytesIO()
    doc.save(sortie)
    return sortie.getvalue()
