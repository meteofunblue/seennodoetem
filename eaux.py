"""
Eaux étrangères pour le tracé automatique des transits -> app/eaux.json.

  - Limites des eaux territoriales (12 Nq) : Flanders Marine Institute, Maritime Boundaries Geodatabase,
    « Territorial Seas (12NM) », Marine Regions (www.marineregions.org), licence CC BY 4.0. Pays non français seulement.
  - Terres étrangères : Natural Earth 1:10 M, pays (domaine public). Hors souveraineté française (SOV_A3 = FR1).

Le tracé pénalise (sans l'interdire) la navigation à moins de 3 Nq de la limite des 12 Nq étrangère et à moins de
15 Nq d'une côte étrangère ; les marges sont appliquées dans l'appli, ce fichier ne contient que les contours.

  python eaux.py --sortie app/eaux.json
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

import requests

WFS = "https://geo.vliz.be/geoserver/MarineRegions/wfs"
TYPES = ["MarineRegions:eez_12nm"]
NE_PAYS = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
           "ne_10m_admin_0_countries.geojson")
ENTETES = {"User-Agent": "modeles-marine/1.0 (outil pedagogique)"}

# régions couvertes [sud, nord, ouest, est]
REGIONS = {
    "europe_atlantique": [20, 75, -45, 45],
    "saint_pierre": [40, 52, -70, -45],
    "antilles_guyane": [-5, 25, -75, -45],
    "ocean_indien": [-35, 5, 30, 80],
    "polynesie": [-30, -5, -160, -130],
    "caledonie": [-30, -10, 155, 175],
}
ECH = 1000          # coordonnées codées au millième de degré (≈ 0,06 Nq)


def couper(ring, b):
    """Sutherland-Hodgman : anneau découpé par le rectangle b = [s, n, o, e]."""
    s, n, o, e = b
    bords = [(lambda p: p[0] >= o, lambda p, q: _x(p, q, o)), (lambda p: p[0] <= e, lambda p, q: _x(p, q, e)),
             (lambda p: p[1] >= s, lambda p, q: _y(p, q, s)), (lambda p: p[1] <= n, lambda p, q: _y(p, q, n))]
    pts = ring
    for dedans, inter in bords:
        if not pts:
            break
        out = []
        prec = pts[-1]
        for p in pts:
            if dedans(p):
                if not dedans(prec):
                    out.append(inter(prec, p))
                out.append(p)
            elif dedans(prec):
                out.append(inter(prec, p))
            prec = p
        pts = out
    return pts


def _x(p, q, x):
    t = (x - p[0]) / (q[0] - p[0]) if q[0] != p[0] else 0
    return [x, p[1] + t * (q[1] - p[1])]


def _y(p, q, y):
    t = (y - p[1]) / (q[1] - p[1]) if q[1] != p[1] else 0
    return [p[0] + t * (q[0] - p[0]), y]


def simplifier(pts, tol):
    if len(pts) < 4:
        return pts
    garde = [False] * len(pts)
    garde[0] = garde[-1] = True
    pile = [(0, len(pts) - 1)]
    while pile:
        a, b = pile.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        nn = math.hypot(dx, dy)
        imax, dmax = -1, 0
        for i in range(a + 1, b):
            d = (abs(dy * (pts[i][0] - x1) - dx * (pts[i][1] - y1)) / nn) if nn > 1e-12 else math.hypot(pts[i][0] - x1, pts[i][1] - y1)
            if d > dmax:
                imax, dmax = i, d
        if dmax > tol:
            garde[imax] = True
            pile += [(a, imax), (imax, b)]
    return [p for p, g in zip(pts, garde) if g]


def coder(ring):
    out, px, py = [], None, None
    for x, y in ring:
        ix, iy = round(x * ECH), round(y * ECH)
        if px is None:
            out += [ix, iy]
        elif (ix, iy) != (px, py):
            out += [ix - px, iy - py]
        px, py = ix, iy
    return out


def exterieurs(geom):
    """Anneaux extérieurs seulement : l'intérieur (terre, eaux intérieures) est compté dans la zone."""
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon":
        return [c[0]]
    if t == "MultiPolygon":
        return [p[0] for p in c]
    return []


def preparer(objets, tol, nom):
    sortie, n_pts = [], 0
    for ring in objets:
        # éclaircissement préalable (points plus proches que la moitié de la tolérance) : simplification plus rapide
        brut, ring = ring, []
        for p in brut:
            x, y = float(p[0]), float(p[1])
            if not ring or abs(x - ring[-1][0]) + abs(y - ring[-1][1]) > tol / 2:
                ring.append([x, y])
        if len(ring) < 4:
            continue
        for reg, b in REGIONS.items():
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            if max(xs) < b[2] or min(xs) > b[3] or max(ys) < b[0] or min(ys) > b[1]:
                continue
            r = couper(ring, b)
            if len(r) < 3:
                continue
            if r[0] != r[-1]:
                r.append(r[0])
            k = max(range(len(r)), key=lambda i: math.hypot(r[i][0] - r[0][0], r[i][1] - r[0][1]))
            r = simplifier(r[:k + 1], tol) + simplifier(r[k:], tol)[1:] if 0 < k < len(r) - 1 else simplifier(r, tol)
            if len(r) >= 4:
                sortie.append(coder(r))
                n_pts += len(r)
    print(f"  {nom} : {len(sortie)} anneaux, {n_pts} points après simplification")
    return sortie


def eaux_12nm():
    anneaux, vus, deja = [], 0, set()
    for reg, (s, n, o, e) in REGIONS.items():
        for t in TYPES:
            print(f"  12 Nq · {reg} ({t})…", flush=True)
            r = requests.get(WFS, params={"service": "WFS", "version": "1.0.0", "request": "GetFeature", "typeName": t,
                                          "outputFormat": "application/json", "bbox": f"{o},{s},{e},{n}"},
                             headers=ENTETES, timeout=600)
            r.raise_for_status()
            d = r.json()
            feats = d.get("features") or []
            if vus == 0 and feats:
                print("    champs :", ", ".join(sorted(feats[0].get("properties", {}).keys())))
            vus += len(feats)
            for f in feats:
                p = f.get("properties") or {}
                ident = f.get("id") or p.get("mrgid") or json.dumps(p, sort_keys=True)
                if ident in deja:          # objet à cheval sur deux régions : une seule fois
                    continue
                deja.add(ident)
                if str(p.get("iso_sov1") or "").upper() == "FRA":
                    continue
                anneaux += exterieurs(f.get("geometry"))
            break
    print(f"  {vus} objets 12 Nq lus, {len(anneaux)} anneaux étrangers")
    return anneaux


def terres_etrangeres():
    r = requests.get(NE_PAYS, headers=ENTETES, timeout=300)
    r.raise_for_status()
    d = r.json()
    anneaux, fr = [], []
    for f in d["features"]:
        p = f.get("properties") or {}
        if (p.get("SOV_A3") or p.get("sov_a3")) == "FR1":
            fr.append(p.get("ADMIN") or p.get("admin"))
            continue
        anneaux += exterieurs(f.get("geometry"))
    print(f"  pays français exclus : {', '.join(sorted(set(filter(None, fr))))}")
    return anneaux


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sortie", default="app/eaux.json")
    args = ap.parse_args()
    print("Limites des 12 Nq (Marine Regions)", flush=True)
    try:
        mer = preparer(eaux_12nm(), 0.004, "12 Nq étrangers")
    except Exception as e:
        print(f"  échec : {type(e).__name__} : {str(e)[:300]}")
        print("  -> seule la bande de 15 Nq autour des côtes étrangères sera utilisée")
        mer = []
    print("Terres étrangères (Natural Earth)", flush=True)
    terre = preparer(terres_etrangeres(), 0.01, "terres étrangères")
    sortie = {"source": "Limites 12 Nq : Flanders Marine Institute, Maritime Boundaries Geodatabase (Marine Regions, CC BY 4.0) ; "
                        "terres : Natural Earth 1:10 M (domaine public)",
              "genere": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "echelle": ECH,
              "regions": REGIONS, "mer12": mer, "terres": terre}
    with open(args.sortie, "w", encoding="utf-8") as f:
        json.dump(sortie, f, separators=(",", ":"))
    print(f"Écrit : {args.sortie} ({os.path.getsize(args.sortie) / 1e6:.1f} Mo)")
    if not terre:
        sys.exit("Aucune terre étrangère : fichier inutilisable.")


if __name__ == "__main__":
    main()
