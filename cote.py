"""
Trait de côte fin pour l'appli : polygones de terre OpenStreetMap simplifiés (osmdata.openstreetmap.de, précision
de l'ordre de la centaine de mètres), ou à défaut GSHHG pleine résolution ; découpé sur une emprise (côtes françaises
par défaut), simplifié (Douglas-Peucker) et écrit en JSON compact.

  python cote.py                               # télécharge GSHHG puis écrit app/cote_fr.json
  python cote.py --shp GSHHS_f_L1.shp          # à partir d'un fichier déjà présent

Format de sortie : {"emprise":[sud,nord,ouest,est], "echelle":1e5, "lignes":[[lon0,lat0,dlon1,dlat1,…], …]}
(entiers : coordonnées × échelle, puis écarts successifs).
Données : © contributeurs OpenStreetMap (ODbL) ; GSHHG : P. Wessel & W. Smith (LGPL).
"""
import argparse
import io
import json
import os
import struct
import zipfile

OSM = "https://osmdata.openstreetmap.de/download/simplified-land-polygons-complete-3857.zip"
# répertoires où chercher l'archive GSHHG (le nom exact du zip est lu dans la page), puis liens directs de secours
REPERTOIRES = ["https://www.ngdc.noaa.gov/mgg/shorelines/data/gshhs/latest/",
               "https://www.ngdc.noaa.gov/mgg/shorelines/data/gshhg/latest/",
               "https://www.ngdc.noaa.gov/mgg/shorelines/data/gshhs/oldversions/version2.3.7/",
               "https://www.ngdc.noaa.gov/mgg/shorelines/data/gshhg/oldversions/version2.3.7/",
               "https://www.ngdc.noaa.gov/mgg/shorelines/data/gshhs/oldversions/version2.3.6/",
               "https://www.ngdc.noaa.gov/mgg/shorelines/data/gshhg/oldversions/version2.3.6/"]
URLS = ["http://www.soest.hawaii.edu/pwessel/gshhg/gshhg-shp-2.3.7.zip",
        "https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-shp-2.3.7.zip"]


def candidats():
    import re
    import requests
    for rep in REPERTOIRES:
        try:
            r = requests.get(rep, timeout=60)
            noms = sorted(set(re.findall(r'href="(gshhg-shp-[\d.]+\.zip)"', r.text)), reverse=True) if r.status_code == 200 else []
            print(f"{rep} : HTTP {r.status_code}{' · ' + ', '.join(noms) if noms else ''}", flush=True)
            for n in noms:
                yield rep + n
        except Exception as e:
            print(f"{rep} : {e.__class__.__name__}", flush=True)
    yield from URLS


def lire_shp(donnees):
    """Lecteur minimal de fichier .shp (polylignes 3 et polygones 5) : liste de parties [(lon, lat), …]."""
    parties = []
    pos = 100
    while pos + 8 <= len(donnees):
        _, long_mots = struct.unpack(">ii", donnees[pos:pos + 8])
        contenu = donnees[pos + 8:pos + 8 + 2 * long_mots]
        pos += 8 + 2 * long_mots
        if len(contenu) < 44:
            continue
        typ = struct.unpack("<i", contenu[:4])[0]
        if typ not in (3, 5, 13, 15, 23, 25):
            continue
        nb_parts, nb_pts = struct.unpack("<ii", contenu[36:44])
        idx = list(struct.unpack(f"<{nb_parts}i", contenu[44:44 + 4 * nb_parts])) + [nb_pts]
        base = 44 + 4 * nb_parts
        pts = struct.unpack(f"<{2 * nb_pts}d", contenu[base:base + 16 * nb_pts])
        for a, b in zip(idx[:-1], idx[1:]):
            parties.append([(pts[2 * i], pts[2 * i + 1]) for i in range(a, b)])
    return parties


def decouper(partie, emp, marge=0.2):
    """Morceaux de la ligne situés dans l'emprise (élargie de la marge)."""
    s, n, o, e = emp[0] - marge, emp[1] + marge, emp[2] - marge, emp[3] + marge
    morceaux, cur = [], []
    for x, y in partie:
        if s <= y <= n and o <= x <= e:
            cur.append((x, y))
        else:
            if len(cur) > 1:
                morceaux.append(cur)
            cur = []
    if len(cur) > 1:
        morceaux.append(cur)
    return morceaux


def couper_polygone(poly, emp, marge=0.2):
    """Polygone fermé découpé sur le rectangle de l'emprise (Sutherland-Hodgman) : reste un polygone fermé."""
    s, n, o, e = emp[0] - marge, emp[1] + marge, emp[2] - marge, emp[3] + marge
    bords = [(lambda p: p[0] >= o, lambda a, b: (o, a[1] + (b[1] - a[1]) * (o - a[0]) / (b[0] - a[0]))),
             (lambda p: p[0] <= e, lambda a, b: (e, a[1] + (b[1] - a[1]) * (e - a[0]) / (b[0] - a[0]))),
             (lambda p: p[1] >= s, lambda a, b: (a[0] + (b[0] - a[0]) * (s - a[1]) / (b[1] - a[1]), s)),
             (lambda p: p[1] <= n, lambda a, b: (a[0] + (b[0] - a[0]) * (n - a[1]) / (b[1] - a[1]), n))]
    pts = list(poly)
    for dedans, inter in bords:
        if not pts:
            break
        sortie, prec = [], pts[-1]
        for cur in pts:
            if dedans(cur):
                if not dedans(prec):
                    sortie.append(inter(prec, cur))
                sortie.append(cur)
            elif dedans(prec):
                sortie.append(inter(prec, cur))
            prec = cur
        pts = sortie
    return pts


def douglas_peucker(pts, tol):
    if len(pts) < 3:
        return pts
    garde = [False] * len(pts)
    garde[0] = garde[-1] = True
    pile = [(0, len(pts) - 1)]
    while pile:
        a, b = pile.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        dmax, imax = -1.0, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            if l2 == 0:
                d = (x - x1) ** 2 + (y - y1) ** 2
            else:
                t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / l2))
                d = (x1 + t * dx - x) ** 2 + (y1 + t * dy - y) ** 2
            if d > dmax:
                dmax, imax = d, i
        if dmax > tol * tol:
            garde[imax] = True
            pile += [(a, imax), (imax, b)]
    return [p for p, k in zip(pts, garde) if k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shp", help="fichier GSHHS_f_L1.shp déjà téléchargé")
    ap.add_argument("--emprise", default="40,53.5,-9,11", help="sud,nord,ouest,est")
    ap.add_argument("--tolerance", type=float, default=0.0004, help="simplification en degrés (0,0004° ≈ 40 m)")
    ap.add_argument("--sortie", default="app/cote_fr.json")
    args = ap.parse_args()
    emp = [float(x) for x in args.emprise.split(",")]

    source = "fichier local"
    if args.shp:
        donnees = open(args.shp, "rb").read()
    else:
        import requests
        archive, source = None, None
        try:
            print(f"Téléchargement {OSM}", flush=True)
            r = requests.get(OSM, timeout=900)
            print(f"  HTTP {r.status_code}, {len(r.content) / 1e6:.0f} Mo", flush=True)
            if r.status_code == 200:
                archive, source = r.content, "osm"
        except Exception as e:
            print(f"  échec : {e.__class__.__name__}", flush=True)
        for url in ([] if archive else candidats()):
            try:
                print(f"Téléchargement {url}", flush=True)
                r = requests.get(url, timeout=600)
                if r.status_code == 200:
                    archive, source = r.content, "gshhg"
                    break
                print(f"  HTTP {r.status_code}")
            except Exception as e:
                print(f"  échec : {e.__class__.__name__}")
        if archive is None:
            raise SystemExit("GSHHG introuvable.")
        z = zipfile.ZipFile(io.BytesIO(archive))
        if source == "osm":
            nom = next(n for n in z.namelist() if n.endswith(".shp"))
        else:
            nom = next(n for n in z.namelist() if n.endswith("GSHHS_f_L1.shp"))
        donnees = z.read(nom)

    parties = lire_shp(donnees)
    print(f"{len(parties)} contours lus", flush=True)
    # coordonnées en mètres (Web Mercator, EPSG:3857) -> degrés
    if parties and max(abs(parties[0][0][0]), abs(parties[0][0][1])) > 400:
        import math
        R = 6378137.0
        parties = [[(x / R * 180 / math.pi, (2 * math.atan(math.exp(y / R)) - math.pi / 2) * 180 / math.pi) for x, y in p]
                   for p in parties]
        print("  projection Web Mercator convertie en degrés", flush=True)
    ech = 100000
    lignes, nb_pts = [], 0
    polygones, nb_poly = [], 0     # terre en polygones fermés : masque des paramètres marins dans l'appli
    for p in parties:
        xs = [x for x, _ in p]
        ys = [y for _, y in p]
        if max(ys) < emp[0] - 1 or min(ys) > emp[1] + 1 or max(xs) < emp[2] - 1 or min(xs) > emp[3] + 1:
            continue
        pg = couper_polygone(p, emp)
        if len(pg) >= 3:
            pg = douglas_peucker(pg + [pg[0]], args.tolerance)[:-1]
            if len(pg) >= 3:
                q = [(round(x * ech), round(y * ech)) for x, y in pg]
                enc = [q[0][0], q[0][1]]
                for (x0, y0), (x1, y1) in zip(q[:-1], q[1:]):
                    enc += [x1 - x0, y1 - y0]
                polygones.append(enc)
                nb_poly += len(pg)
        for m in decouper(p, emp):
            m = douglas_peucker(m, args.tolerance)
            if len(m) < 2:
                continue
            q = [(round(x * ech), round(y * ech)) for x, y in m]
            enc = [q[0][0], q[0][1]]
            for (x0, y0), (x1, y1) in zip(q[:-1], q[1:]):
                enc += [x1 - x0, y1 - y0]
            lignes.append(enc)
            nb_pts += len(m)
    os.makedirs(os.path.dirname(args.sortie) or ".", exist_ok=True)
    with open(args.sortie, "w") as f:
        json.dump({"source": {"osm": "© contributeurs OpenStreetMap (ODbL)", "gshhg": "GSHHG (Wessel & Smith, LGPL)"}.get(source, source), "emprise": emp, "echelle": ech,
                   "lignes": lignes, "polygones": polygones}, f, separators=(",", ":"))
    print(f"{len(polygones)} polygones de terre ({nb_poly} points) ; {len(lignes)} lignes, {nb_pts} points, {os.path.getsize(args.sortie) / 1e6:.1f} Mo -> {args.sortie}")


if __name__ == "__main__":
    main()
