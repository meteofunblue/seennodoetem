"""
Zones d'exercices et de tir (cartes ZONEX) du Shom -> app/zonex.json, calque affiché à la demande dans l'appli.

Données : Shom, « Zones d'exercices » (objets S-57 MIPARE), Licence Ouverte Etalab 2.0.
Sources essayées dans l'ordre :
  1. service WFS du Shom (GeoJSON) ;
  2. Shapefile publié sur data.gouv.fr ;
  3. archive ZONEX_PACK.7z du Shom.

  python zonex.py --sortie app/zonex.json

Le journal affiche les champs trouvés et quelques exemples : à envoyer si les noms de zones n'apparaissent pas.
"""
import argparse
import io
import json
import math
import re
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

import requests

WFS = "https://services.data.shom.fr/INSPIRE/wfs"
TYPE = "ZONEX_BDD_WFS:mipare"
SHP_DATAGOUV = "https://www.data.gouv.fr/api/1/datasets/r/2e6f2354-5c50-49b9-aefe-cc1b77deef3c"
PACK_7Z = ("https://services.data.shom.fr/INSPIRE/telechargement/prepackageGroup/ZONEX_PACK_DL/"
           "prepackage/ZONEX_PACK/file/ZONEX_PACK.7z")
ENTETES = {"User-Agent": "modeles-marine/1.0 (outil pedagogique)"}

# catégories S-57 CATMPA
CATEGORIES = {1: "zone d'exercice", 2: "exercice de torpilles", 3: "exercice sous-marin", 4: "zone de tir",
              5: "mouillage de mines d'exercice", 6: "tir aux armes légères"}
CHAMPS_NOM = ["OBJNAM", "NOBJNM", "objnam", "nobjnm", "nom", "NOM", "name", "NAME", "libelle", "LIBELLE", "id_zone", "ID_ZONE"]
CHAMPS_CAT = ["CATMPA", "catmpa", "categorie", "CATEGORIE", "type", "TYPE"]
CHAMPS_INFO = ["NINFOM", "ninfom", "INFORM", "inform", "TXTDSC", "txtdsc", "NTXTDS", "ntxtds", "commentaire", "COMMENTAIRE"]


def premier(props, noms):
    for n in noms:
        v = props.get(n)
        if v not in (None, "", "null"):
            return str(v).strip()
    return ""


# ---------- lecture des sources ----------
def depuis_wfs():
    r = requests.get(WFS, params={"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": TYPE,
                                  "outputFormat": "application/json", "srsName": "EPSG:4326"},
                     headers=ENTETES, timeout=180)
    r.raise_for_status()
    d = r.json()
    if not d.get("features"):
        raise ValueError("aucun objet renvoyé")
    return [(f.get("properties") or {}, f.get("geometry")) for f in d["features"]]


def lire_shp(dossier):
    import shapefile   # pyshp
    shp = next((os.path.join(r, f) for r, _, fs in os.walk(dossier) for f in fs if f.lower().endswith(".shp")), None)
    if not shp:
        raise ValueError("pas de fichier .shp dans l'archive")
    print(f"  fichier : {os.path.relpath(shp, dossier)}")
    prj = shp[:-4] + ".prj"
    if os.path.exists(prj):
        print(f"  projection : {open(prj, errors='replace').read()[:90]}…")
    for enc in ("utf-8", "latin-1"):
        try:
            sf = shapefile.Reader(shp, encoding=enc)
            return [(rec.record.as_dict(), rec.shape.__geo_interface__) for rec in sf.iterShapeRecords()]
        except UnicodeDecodeError:
            continue
    raise ValueError("encodage du fichier illisible")


def depuis_datagouv():
    r = requests.get(SHP_DATAGOUV, headers=ENTETES, timeout=180, allow_redirects=True)
    r.raise_for_status()
    with tempfile.TemporaryDirectory() as d:
        zipfile.ZipFile(io.BytesIO(r.content)).extractall(d)
        return lire_shp(d)


def depuis_pack():
    import py7zr
    r = requests.get(PACK_7Z, headers=ENTETES, timeout=300)
    r.raise_for_status()
    with tempfile.TemporaryDirectory() as d:
        with py7zr.SevenZipFile(io.BytesIO(r.content)) as z:
            z.extractall(d)
        for racine, _, fs in os.walk(d):          # zip éventuellement imbriqué
            for f in fs:
                if f.lower().endswith(".zip"):
                    zipfile.ZipFile(os.path.join(racine, f)).extractall(racine)
        return lire_shp(d)


# ---------- géométrie ----------
def anneaux(geom):
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon":
        return [c]
    if t == "MultiPolygon":
        return c
    return []


def vers_lonlat(polys):
    """Remet les coordonnées en lon/lat : ordre lat/lon (WFS 2.0) ou Web Mercator (EPSG:3857) détectés et corrigés."""
    pts = [p for poly in polys for ring in poly for p in ring[:50]]
    if not pts:
        return polys
    mx = sum(abs(p[0]) for p in pts) / len(pts)
    my = sum(abs(p[1]) for p in pts) / len(pts)
    if mx > 1000 or my > 1000:               # Web Mercator
        R = 6378137.0
        f = lambda p: [p[0] / R * 180 / math.pi, (2 * math.atan(math.exp(p[1] / R)) - math.pi / 2) * 180 / math.pi]
    elif mx > 30:                            # lat, lon (latitude de la France > 41°, longitude < 11°)
        f = lambda p: [p[1], p[0]]
    else:
        f = lambda p: [p[0], p[1]]
    return [[[f(p) for p in ring] for ring in poly] for poly in polys]


def simplifier(pts, tol):
    """Douglas-Peucker (degrés). Anneau fermé : coupé au point le plus éloigné du départ, chaque moitié simplifiée."""
    if len(pts) < 4:
        return pts
    if pts[0] == pts[-1]:
        k = max(range(len(pts)), key=lambda i: math.hypot(pts[i][0] - pts[0][0], pts[i][1] - pts[0][1]))
        if 0 < k < len(pts) - 1:
            return simplifier_ouvert(pts[:k + 1], tol) + simplifier_ouvert(pts[k:], tol)[1:]
    return simplifier_ouvert(pts, tol)


def simplifier_ouvert(pts, tol):
    if len(pts) < 3:
        return pts
    garde = [False] * len(pts)
    garde[0] = garde[-1] = True
    pile = [(0, len(pts) - 1)]
    while pile:
        a, b = pile.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        n = math.hypot(dx, dy) or 1e-12
        imax, dmax = -1, 0
        for i in range(a + 1, b):
            d = abs(dy * (pts[i][0] - x1) - dx * (pts[i][1] - y1)) / n
            if d > dmax:
                imax, dmax = i, d
        if dmax > tol:
            garde[imax] = True
            pile += [(a, imax), (imax, b)]
    return [p for p, g in zip(pts, garde) if g]


def coder(ring, echelle):
    """Entiers (degrés × échelle) codés en écarts successifs, comme le trait de côte."""
    out, px, py = [], None, None
    for x, y in ring:
        ix, iy = round(x * echelle), round(y * echelle)
        if px is None:
            out += [ix, iy]
        elif (ix, iy) != (px, py):
            out += [ix - px, iy - py]
        px, py = ix, iy
    return out


def sans_accents(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def categorie_texte(txt):
    """Pas de champ CATMPA dans le WFS : type déduit du texte descriptif (« Secteur d'exercices », « Zone de tir »…)."""
    t = sans_accents(txt or "")
    if re.search(r"armes? legeres?|petit calibre", t):
        return 6
    if re.search(r"\btirs?\b|firing|artillerie|missile", t):
        return 4
    if "sous-marin" in t or "sous marin" in t or "submarine" in t:
        return 3
    if "torpill" in t:
        return 2
    if re.search(r"\bmines?\b|mouillage", t):
        return 5
    return None


def categorie(txt):
    for morceau in str(txt).replace(";", ",").split(","):
        try:
            return int(float(morceau.strip()))
        except ValueError:
            continue
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sortie", default="app/zonex.json")
    ap.add_argument("--tolerance", type=float, default=0.0003)
    args = ap.parse_args()

    objets, source = None, None
    for nom, f in (("WFS Shom", depuis_wfs), ("Shapefile data.gouv.fr", depuis_datagouv), ("archive Shom ZONEX_PACK", depuis_pack)):
        print(f"Essai : {nom}", flush=True)
        try:
            objets, source = f(), nom
            print(f"  {len(objets)} objets lus")
            break
        except Exception as e:
            print(f"  échec : {type(e).__name__} : {str(e)[:200]}")
    if not objets:
        sys.exit("Aucune source ZONEX accessible.")

    champs = sorted({k for p, _ in objets for k in p})
    print(f"Champs : {', '.join(champs)}")
    for p, _ in objets[:5]:
        print("  exemple :", {k: v for k, v in p.items() if v not in (None, "")})

    echelle = 10000
    zones, par_cat, textes = [], {}, {}
    for props, geom in objets:
        polys = vers_lonlat(anneaux(geom))
        rings = []
        for poly in polys:
            for ring in poly:
                r = simplifier([[float(x), float(y)] for x, y, *_ in ring], args.tolerance)
                if len(r) >= 3:
                    rings.append(coder(r, echelle))
        if not rings:
            continue
        info = premier(props, CHAMPS_INFO)
        brut = premier(props, CHAMPS_CAT)
        cat = categorie(brut) if brut else (categorie_texte(info) or categorie_texte(premier(props, CHAMPS_NOM)) or 1)
        textes[info] = textes.get(info, 0) + 1
        par_cat[cat] = par_cat.get(cat, 0) + 1
        zones.append({"n": premier(props, CHAMPS_NOM), "c": cat, "i": info[:300], "r": rings})
    zones.sort(key=lambda z: z["n"])
    print(f"{len(zones)} zones retenues ; par catégorie : " +
          ", ".join(f"{CATEGORIES.get(k, k)} {v}" for k, v in sorted(par_cat.items())))
    print(f"Sans nom : {sum(1 for z in zones if not z['n'])}")
    print("Textes descriptifs rencontrés :")
    for t, n in sorted(textes.items(), key=lambda x: -x[1]):
        print(f"  {n:4d} × {t or '(vide)'}  ->  {CATEGORIES.get(categorie_texte(t) or 1)}")

    sortie = {"source": f"Shom, zones d'exercices (cartes ZONEX), Licence Ouverte 2.0 ({source})",
              "genere": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "echelle": echelle,
              "categories": CATEGORIES, "zones": zones}
    with open(args.sortie, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Écrit : {args.sortie} ({os.path.getsize(args.sortie) / 1e3:.0f} ko)")


if __name__ == "__main__":
    main()
