"""
Convertisseur commun : paquets Météo-France (GRIB2, NetCDF WW3) -> tuiles légères de 1° pour l'appli.

  python convertir.py --modele arome --sortie publie            # dernier run complet
  python convertir.py --modele arome --sortie publie --run 2026-10-17T06:00:00Z
  python convertir.py --modele arome --sortie publie --grib dossier   # test hors ligne

Configuration : modeles.json (sources, domaine et résolution des tuiles).
Produit <sortie>/<modele>/meta.json, run.json et t/<lat>_<lon>.bin.gz (tuiles de 1°, cf. « format » dans meta.json).
"""
import argparse
import base64
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import numpy as np

API = "https://public-api.meteofrance.fr/previnum"
MANQUANT = -32768
KT = 1.943844

# ------------------------------------------------------------------ paramètres retenus
# clé : (discipline, catégorie, numéro, type de niveau ou None, type de pas) ; niveau = filtre facultatif
P_ATMO = [
    dict(cle=(0, 3, 1, "meanSea", "instant"),            nom="pressure_msl",       conv=lambda v: v / 100,  ech=0.01, off=1000, unite="hPa"),   # 0,01 hPa : isobares lisses à 0,025°
    dict(cle=(0, 2, 1, "heightAboveGround", "instant"),  niveau=10, nom="wind_speed_10m",     conv=lambda v: v * KT, ech=0.1, off=0, unite="kt"),
    dict(cle=(0, 2, 0, "heightAboveGround", "instant"),  niveau=10, nom="wind_direction_10m", conv=lambda v: v,      ech=1,   off=0, unite="°"),
    dict(cle=(0, 2, 22, "heightAboveGround", "max"),     niveau=10, nom="wind_gusts_10m",     conv=lambda v: v * KT, ech=0.1, off=0, unite="kt", cumul="max"),
    dict(cle=(0, 0, 0, "heightAboveGround", "instant"),  niveau=2,  nom="temperature_2m",     conv=lambda v: v - 273.15, ech=0.1, off=0, unite="°C"),
    dict(cle=(0, 0, 6, "heightAboveGround", "instant"),  niveau=2,  nom="dew_point_2m",       conv=lambda v: v - 273.15, ech=0.1, off=0, unite="°C"),
    dict(cle=(0, 1, 52, "surface", "accum"),             nom="precipitation",      conv=lambda v: v,        ech=0.01, off=0, unite="mm", cumul="somme"),
    dict(cle=(0, 6, 1, "surface", "instant"),            nom="cloud_cover",        conv=lambda v: v,        ech=1,    off=0, unite="%"),
    dict(cle=(0, 6, 3, "surface", "instant"),            nom="cloud_cover_low",    conv=lambda v: v,        ech=1,    off=0, unite="%"),
    dict(cle=(0, 6, 4, "surface", "instant"),            nom="cloud_cover_mid",    conv=lambda v: v,        ech=1,    off=0, unite="%"),
    dict(cle=(0, 6, 5, "surface", "instant"),            nom="cloud_cover_high",   conv=lambda v: v,        ech=1,    off=0, unite="%"),
    dict(cle=(0, 7, 6, None, "instant"),                 nom="cape",               conv=lambda v: v,        ech=1,    off=0, unite="J/kg"),
]
P_VAGUES = [
    dict(cle=(10, 0, 3, "meanSea", "instant"),  nom="wave_height",          conv=lambda v: v, ech=0.01, off=0, unite="m"),
    dict(cle=(10, 0, 14, "meanSea", "instant"), nom="wave_direction",       conv=lambda v: v, ech=1,    off=0, unite="°"),
    dict(cle=(10, 0, 15, "meanSea", "instant"), nom="wave_period",          conv=lambda v: v, ech=0.1,  off=0, unite="s"),
    dict(cle=(10, 0, 34, "meanSea", "instant"), nom="wave_peak_period",     conv=lambda v: v, ech=0.1,  off=0, unite="s"),
    dict(cle=(10, 0, 5, "meanSea", "instant"),  nom="wind_wave_height",     conv=lambda v: v, ech=0.01, off=0, unite="m"),
    dict(cle=(10, 0, 4, "meanSea", "instant"),  nom="wind_wave_direction",  conv=lambda v: v, ech=1,    off=0, unite="°"),
    dict(cle=(10, 0, 6, "meanSea", "instant"),  nom="wind_wave_period",     conv=lambda v: v, ech=0.1,  off=0, unite="s"),
    dict(cle=(10, 0, 8, "meanSea", "instant"),  nom="swell_wave_height",    conv=lambda v: v, ech=0.01, off=0, unite="m"),
    dict(cle=(10, 0, 7, "meanSea", "instant"),  nom="swell_wave_direction", conv=lambda v: v, ech=1,    off=0, unite="°"),
    dict(cle=(10, 0, 9, "meanSea", "instant"),  nom="swell_wave_period",    conv=lambda v: v, ech=0.1,  off=0, unite="s"),
    dict(cle=(0, 2, 1, "heightAboveGround", "instant"), niveau=10, nom="wind_speed_10m",     conv=lambda v: v * KT, ech=0.1, off=0, unite="kt"),
    dict(cle=(0, 2, 0, "heightAboveGround", "instant"), niveau=10, nom="wind_direction_10m", conv=lambda v: v,      ech=1,   off=0, unite="°"),
]
# WW3 (NetCDF sur maillage triangulaire). Partition 0 = mer du vent, 1 = houle primaire, 2 = houle secondaire.
# src : variable(s) NetCDF, la première trouvée est prise ; facultatif=True : ignoré si absent du fichier.
# genre : "scalaire", "direction" (interpolée par composantes), "hauteur_part" (0 m là où la mer existe sans cette
# partition), "vent_vitesse" / "vent_direction" (depuis uwnd, vwnd).
P_WW3 = [
    dict(src=["hs"],      genre="scalaire",  nom="wave_height",               ech=0.01, off=0, unite="m"),
    dict(src=["dir"],     genre="direction", nom="wave_direction",            ech=1,    off=0, unite="°"),
    dict(src=["t0m1", "t02", "t01"], genre="scalaire", nom="wave_period",     ech=0.1,  off=0, unite="s", facultatif=True),
    dict(src=["ptp", "fp"], genre="scalaire", nom="wave_peak_period",         ech=0.1,  off=0, unite="s"),
    dict(src=["phs0"],    genre="hauteur_part", nom="wind_wave_height",        ech=0.01, off=0, unite="m"),
    dict(src=["pdir0"],   genre="direction", nom="wind_wave_direction", part="phs0",       ech=1,    off=0, unite="°"),
    dict(src=["pt0m10"],  genre="scalaire",  nom="wind_wave_period", part="phs0",          ech=0.1,  off=0, unite="s"),
    dict(src=["hsts"],    genre="scalaire",  nom="swell_wave_height",         ech=0.01, off=0, unite="m", facultatif=True),
    dict(src=["phs1"],    genre="hauteur_part", nom="primary_swell_height",    ech=0.01, off=0, unite="m"),
    dict(src=["pdir1"],   genre="direction", nom="primary_swell_direction", part="phs1",   ech=1,    off=0, unite="°"),
    dict(src=["pt0m11"],  genre="scalaire",  nom="primary_swell_period", part="phs1",      ech=0.1,  off=0, unite="s"),
    dict(src=["ptp1"],    genre="scalaire",  nom="primary_swell_peak_period", part="phs1", ech=0.1,  off=0, unite="s", facultatif=True),
    dict(src=["phs2"],    genre="hauteur_part", nom="secondary_swell_height",  ech=0.01, off=0, unite="m"),
    dict(src=["pdir2"],   genre="direction", nom="secondary_swell_direction", part="phs2", ech=1,    off=0, unite="°"),
    dict(src=["pt0m12"],  genre="scalaire",  nom="secondary_swell_period", part="phs2",    ech=0.1,  off=0, unite="s"),
    dict(src=["uwnd", "vwnd"], genre="vent_vitesse",   nom="wind_speed_10m",     ech=0.1, off=0, unite="kt", facultatif=True),
    dict(src=["uwnd", "vwnd"], genre="vent_direction", nom="wind_direction_10m", ech=1,   off=0, unite="°",  facultatif=True),
]
JEUX = {"atmo": P_ATMO, "vagues": P_VAGUES, "ww3": P_WW3}


def index_params(liste):
    return {p["cle"]: p for p in liste}


# ------------------------------------------------------------------ décodage GRIB
def lire_grib(chemin, params, champs, grille):
    """Range dans champs[nom][(debut|None, fin)] les tableaux 2D des paramètres retenus."""
    import eccodes
    idx = index_params(params)
    lus = retenus = 0
    with open(chemin, "rb") as f:
        while True:
            try:
                gid = eccodes.codes_grib_new_from_file(f)
            except Exception as e:  # message corrompu : on s'arrête là pour ce fichier
                print(f"    message illisible ({e.__class__.__name__}), suite du fichier ignorée")
                break
            if gid is None:
                break
            try:
                lus += 1
                g = lambda k: eccodes.codes_get(gid, k)
                cle = (g("discipline"), g("parameterCategory"), g("parameterNumber"), g("typeOfLevel"), g("stepType"))
                p = idx.get(cle) or idx.get(cle[:3] + (None, cle[4]))
                if p is None or ("niveau" in p and g("level") != p["niveau"]):
                    continue
                if not grille:
                    grille.update(ni=g("Ni"), nj=g("Nj"), lat1=g("latitudeOfFirstGridPointInDegrees"),
                                  lon1=g("longitudeOfFirstGridPointInDegrees"),
                                  di=g("iDirectionIncrementInDegrees"), dj=g("jDirectionIncrementInDegrees"),
                                  jpos=g("jScansPositively"))
                vals = eccodes.codes_get_values(gid).astype(np.float32)
                if g("bitmapPresent"):
                    vals[vals == g("missingValue")] = np.nan
                arr = vals.reshape(g("Nj"), g("Ni"))
                sr = str(g("stepRange"))
                a, b = ((int(x) for x in sr.split("-")) if "-" in sr else (None, int(sr)))
                if cle[4] == "instant":
                    a = None
                champs.setdefault(p["nom"], {})[(a, b)] = p["conv"](arr)
                retenus += 1
            finally:
                eccodes.codes_release(gid)
    return lus, retenus


def valeur_horaire(p, morceaux, h):
    """Valeur sur l'heure qui se termine à h."""
    mode = p.get("cumul")
    if not mode:
        return morceaux.get((None, h))
    if (h - 1, h) in morceaux:
        return morceaux[(h - 1, h)]
    if mode == "somme" and (0, h) in morceaux:
        if h == 1:
            return morceaux[(0, 1)]
        if (0, h - 1) in morceaux:
            return np.maximum(morceaux[(0, h)] - morceaux[(0, h - 1)], 0)
    if mode == "max" and (0, h) in morceaux:
        return morceaux[(0, h)]
    return None


# ------------------------------------------------------------------ WW3 : maillage triangulaire -> grille régulière
def grille_zone(z):
    """Grille régulière de la zone, alignée sur les multiples de la résolution."""
    res = z["res"]
    lats = np.arange(np.ceil(z["s"] / res - 1e-6), np.floor(z["n"] / res + 1e-6) + 1) * res
    lons = np.arange(np.ceil(z["o"] / res - 1e-6), np.floor(z["e"] / res + 1e-6) + 1) * res
    return np.round(lats, 6), np.round(lons, 6), res


def poids_grille(lon_n, lat_n, tri, lats, lons):
    """Comme poids_barycentriques, mais pour tous les points d'une grille régulière (lats × lons, ordre lat puis lon),
    en ne testant pour chaque triangle que les points de grille compris dans son emprise (rapide sur 100 000 triangles)."""
    ny, nx = len(lats), len(lons)
    dy, dx = lats[1] - lats[0], lons[1] - lons[0]
    noeuds = np.full((ny * nx, 3), -1, dtype=np.int64)
    poids = np.zeros((ny * nx, 3), dtype=np.float64)
    tx, ty = lon_n[tri], lat_n[tri]
    eps = 1e-9
    i0 = np.maximum(np.ceil((ty.min(1) - lats[0]) / dy - eps).astype(np.int64), 0)
    i1 = np.minimum(np.floor((ty.max(1) - lats[0]) / dy + eps).astype(np.int64), ny - 1)
    j0 = np.maximum(np.ceil((tx.min(1) - lons[0]) / dx - eps).astype(np.int64), 0)
    j1 = np.minimum(np.floor((tx.max(1) - lons[0]) / dx + eps).astype(np.int64), nx - 1)
    ci, cj = np.maximum(i1 - i0 + 1, 0), np.maximum(j1 - j0 + 1, 0)
    nb = ci * cj
    garde = np.where(nb > 0)[0]
    if not len(garde):
        return noeuds, poids
    nb = nb[garde]
    k = np.repeat(garde, nb)                                   # triangle candidat
    debut = np.repeat(np.cumsum(nb) - nb, nb)
    o = np.arange(len(k)) - debut
    ii = i0[k] + o // cj[k]
    jj = j0[k] + o % cj[k]
    x, y = lons[jj], lats[ii]
    x1, y1, x2, y2, x3, y3 = tx[k, 0], ty[k, 0], tx[k, 1], ty[k, 1], tx[k, 2], ty[k, 2]
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    ok = det != 0
    det = np.where(ok, det, 1)
    l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / det
    l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / det
    l3 = 1 - l1 - l2
    dedans = ok & (l1 >= -1e-7) & (l2 >= -1e-7) & (l3 >= -1e-7)
    pt = (ii * nx + jj)[dedans]
    pt_u, premier = np.unique(pt, return_index=True)          # un seul triangle par point
    sel = np.where(dedans)[0][premier]
    noeuds[pt_u] = tri[k[sel]]
    poids[pt_u] = np.stack([l1[sel], l2[sel], l3[sel]], 1)
    return noeuds, poids


def interpoler(valeurs, noeuds, poids):
    """valeurs (temps, nœuds) -> (temps, points). Les nœuds sans valeur sont écartés et les poids renormalisés."""
    dedans = noeuds[:, 0] >= 0
    idx = np.where(dedans[:, None], noeuds, 0)
    v = valeurs[:, idx]                                   # temps, points, 3
    w = np.broadcast_to(poids, v.shape).copy()
    w[~np.isfinite(v)] = 0
    v = np.where(np.isfinite(v), v, 0)
    somme = w.sum(-1)
    out = np.where(somme > 1e-6, (v * w).sum(-1) / np.where(somme > 1e-6, somme, 1), np.nan)
    out[:, ~dedans] = np.nan
    return out.astype(np.float32)


def lire_variable(ds, nom):
    """Variable NetCDF (temps, nœuds) en float avec NaN pour les valeurs manquantes (facteur d'échelle appliqué)."""
    v = ds.variables[nom]
    v.set_auto_maskandscale(True)
    a = v[:]
    return np.ma.filled(np.ma.asarray(a).astype(np.float64), np.nan)


def lire_maillage(chemin, params, zones, modele, run, ouvrir=None, couverture_min=0.8):
    """Lit un fichier WW3 et renvoie (sel, champs_zone) au même format que la lecture GRIB."""
    if ouvrir is None:
        import netCDF4
        ouvrir = lambda c: netCDF4.Dataset(c)
    ds = ouvrir(chemin)
    noms_nc = list(ds.variables)
    print(f"  variables du fichier : {', '.join(noms_nc)}", flush=True)
    lon_n = np.asarray(ds.variables["longitude"][:], dtype=np.float64)
    lat_n = np.asarray(ds.variables["latitude"][:], dtype=np.float64)
    lon_n = (lon_n + 180) % 360 - 180
    tri = np.asarray(ds.variables["tri"][:], dtype=np.int64)
    if tri.min() >= 1:          # numérotation Fortran (à partir de 1)
        tri = tri - 1
    print(f"  maillage : {len(lon_n)} nœuds, {len(tri)} triangles, "
          f"lat {lat_n.min():.2f}…{lat_n.max():.2f}, lon {lon_n.min():.2f}…{lon_n.max():.2f}", flush=True)

    # échéances en heures depuis le run (temps en jours depuis 1990-01-01)
    t_run = datetime.strptime(run, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    origine = datetime(1990, 1, 1, tzinfo=timezone.utc)
    jours = np.asarray(ds.variables["time"][:], dtype=np.float64)
    heures = [int(round((origine + timedelta(days=float(d)) - t_run).total_seconds() / 3600)) for d in jours]
    print(f"  {len(heures)} échéances : {heures[0]} h à {heures[-1]} h", flush=True)

    # zones couvertes par l'emprise du maillage, et poids d'interpolation
    sel, interp = {}, {}
    for zn, z in zones.items():
        if z.get("modeles") and modele not in z["modeles"]:
            continue
        # zone gardée si l'emprise du maillage en couvre au moins 80 % (le reste reste vide)
        dy = max(0.0, min(z["n"], lat_n.max()) - max(z["s"], lat_n.min()))
        dx = max(0.0, min(z["e"], lon_n.max()) - max(z["o"], lon_n.min()))
        couvert = dx * dy / ((z["n"] - z["s"]) * (z["e"] - z["o"]))
        if couvert < couverture_min:
            print(f"  zone {zn} hors du domaine du modèle ({100 * couvert:.0f} % couverts) : ignorée")
            continue
        lats, lons, res = grille_zone(z)
        t0 = time.time()
        noeuds, poids = poids_grille(lon_n, lat_n, tri, lats, lons)
        part = 100 * (noeuds[:, 0] >= 0).mean()
        print(f"  zone {zn} : {len(lats)}×{len(lons)} points, {part:.0f} % en mer ({time.time()-t0:.1f} s)", flush=True)
        if part < 5:
            continue
        sel[zn] = (range(len(lats)), range(len(lons)), lats, lons, res)
        interp[zn] = (noeuds, poids, len(lats), len(lons))

    champs_zone = {zn: {} for zn in sel}
    cache = {}

    def brut(nom):
        if nom not in cache:
            cache[nom] = lire_variable(ds, nom)
        return cache[nom]

    hs = brut("hs") if "hs" in noms_nc else None
    for p in params:
        dispo = [s for s in p["src"] if s in noms_nc]
        if p["genre"].startswith("vent"):
            dispo = p["src"] if all(s in noms_nc for s in p["src"]) else []
        if not dispo:
            if not p.get("facultatif"):
                print(f"  {p['nom']} : variable {'/'.join(p['src'])} absente du fichier")
            continue
        masque = None
        if p.get("part") and p["part"] in noms_nc:
            # partition absente ou négligeable (< 5 cm) : direction et période sans signification
            masque = ~(brut(p["part"]) >= 0.05)
        for zn, (noeuds, poids, ny, nx) in interp.items():
            if p["genre"] == "direction":
                a = np.radians(brut(dispo[0]))
                if masque is not None:
                    a = np.where(masque, np.nan, a)
                cube = np.degrees(np.arctan2(interpoler(np.sin(a), noeuds, poids),
                                             interpoler(np.cos(a), noeuds, poids))) % 360
            elif p["genre"].startswith("vent"):
                u, v = interpoler(brut("uwnd"), noeuds, poids), interpoler(brut("vwnd"), noeuds, poids)
                cube = np.hypot(u, v) * KT if p["genre"] == "vent_vitesse" else np.degrees(np.arctan2(-u, -v)) % 360
            else:
                b = brut(dispo[0])
                if masque is not None:
                    b = np.where(masque, np.nan, b)
                if p["genre"] == "hauteur_part" and hs is not None:
                    b = np.where(np.isfinite(hs) & ~np.isfinite(b), 0.0, b)   # mer présente, partition absente
                cube = interpoler(b, noeuds, poids)
            cube = cube.reshape(len(heures), ny, nx)
            for t, h in enumerate(heures):
                champs_zone[zn].setdefault(p["nom"], {})[(None, h)] = cube[t]
        for s_ in dispo:            # libère la mémoire (hs et le vent servent encore)
            if s_ not in ("hs", "uwnd", "vwnd", "phs0", "phs1", "phs2"):
                cache.pop(s_, None)
    ds.close()
    return sel, champs_zone, heures


# ------------------------------------------------------------------ zones
def axes(grille):
    lats = grille["lat1"] + np.arange(grille["nj"]) * grille["dj"] * (1 if grille["jpos"] else -1)
    lons = (grille["lon1"] + np.arange(grille["ni"]) * grille["di"] + 180) % 360 - 180
    return lats, lons


def selection_zone(z, grille, natif):
    """Indices (lignes du sud au nord, colonnes d'ouest en est) de la zone, ou None si hors domaine."""
    lats, lons = axes(grille)
    eps = natif / 2
    if z["s"] < lats.min() - eps or z["n"] > lats.max() + eps:
        return None
    global_ = grille["ni"] * grille["di"] > 359
    if not global_ and (z["o"] < lons.min() - eps or z["e"] > lons.max() + eps):
        return None
    k = max(1, round(z["res"] / natif))
    lig = np.where((lats >= z["s"] - eps) & (lats <= z["n"] + eps))[0]
    lig = lig[np.argsort(lats[lig])][::k]
    col = np.where((lons >= z["o"] - eps) & (lons <= z["e"] + eps))[0]
    col = col[np.argsort(lons[col])][::k]
    if len(lig) < 2 or len(col) < 2:
        return None
    return lig, col, lats[lig], lons[col], natif * k


def encoder(cube, ech, off):
    q = np.round((cube - off) / ech)
    q = np.where(np.isfinite(q), np.clip(q, -32767, 32767), MANQUANT).astype("<i2")
    return base64.b64encode(q.tobytes()).decode("ascii")


# ------------------------------------------------------------------ catalogue
def bornes(ech):
    n = [int(x) for x in re.findall(r"(\d+)H", ech)]
    return (n[0], n[-1])


def echeances_du_run(ses, base, cfg, run):
    """Liste des groupes (« 00H06H ») ou échéances (« 001H ») annoncés pour ce run."""
    r = ses.get(f"{base}/grids/{cfg['grille']}/packages/{cfg['paquets'][0]}", params={"referencetime": run})
    toks = list(dict.fromkeys(re.findall(r"\b\d{2,3}H(?:\d{2,3}H)?\b", r.text)))
    if cfg["type"] == "fichier":
        toks = [t for t in toks if "999" in t]
    elif cfg["type"] == "groupes":
        toks = [t for t in toks if t.count("H") == 2 and "999" not in t]
    else:
        toks = [t for t in toks if t.count("H") == 1]
    return sorted(toks, key=bornes)


def heures_de_sortie(heures, pas):
    return [h for h in heures if h % pas == 0] if pas > 1 else list(heures)


def telecharger_run(ses, base, cfg, run, travail):
    """Télécharge tous les fichiers d'un run. Renvoie la liste des chemins, ou [] si le run est incomplet."""
    liste = echeances_du_run(ses, base, cfg, run)
    fmt = cfg.get("format", "grib2")
    if cfg["type"] == "fichier":
        assez = bool(liste)
    elif cfg["type"] == "echeances":
        liste = [t for t in liste if bornes(t)[1] % cfg["pas_sortie"] == 0]
        assez = len(liste) >= cfg["min_echeances"]
    else:
        fin = max((bornes(t)[1] for t in liste), default=0)
        assez = len(heures_de_sortie(range(0, fin + 1), cfg["pas_sortie"])) >= cfg["min_echeances"]
    print(f"Run {run} : {len(liste)} fichiers annoncés{'' if assez else ' (incomplet)'}", flush=True)
    if not assez:
        return []
    taches = [(tok, paquet) for tok in liste for paquet in cfg["paquets"]]
    url = f"{base}/grids/{cfg['grille']}/packages/{{}}/{cfg['produit']}"
    t_debut = time.time()

    def un(tache):
        tok, paquet = tache
        chemin = os.path.join(travail, f"{paquet}_{tok}.{'nc' if fmt == 'nc' else 'grib2'}")
        t0 = time.time()
        n = telecharger_fichier(ses, url.format(paquet), {"referencetime": run, "time": tok, "format": fmt},
                                chemin, f"{paquet} {tok}")
        return tache, chemin, n, time.time() - t0

    # plusieurs téléchargements simultanés (3 par défaut), l'ordre des fichiers est conservé
    from concurrent.futures import ThreadPoolExecutor
    resultats, faits, abandon = {}, 0, False
    with ThreadPoolExecutor(max_workers=cfg.get("simultanes", 3)) as pool:
        for (tok, paquet), chemin, n, duree in pool.map(un, taches):
            faits += 1
            resultats[(tok, paquet)] = chemin
            if n <= 0:
                abandon = True
                print(f"  {paquet} {tok} : indisponible", flush=True)
                continue
            if cfg["type"] != "echeances" or faits % 5 == 0 or faits == len(taches):
                print(f"  {paquet} {tok} : {n/1e6:.0f} Mo en {duree:.0f} s ({faits}/{len(taches)}, "
                      f"{time.time()-t_debut:.0f} s écoulées)", flush=True)
    if abandon:
        print(f"  run {run} abandonné pour l'instant.", flush=True)
        for chemin in resultats.values():
            if os.path.exists(chemin):
                os.remove(chemin)
        return []
    return [resultats[t] for t in taches]


def telecharger_fichier(ses, url, params, chemin, etiquette, essais=5):
    """Télécharge un fichier en réessayant si la connexion se coupe en cours de route.
    Renvoie la taille en octets, 0 si le fichier n'existe pas (404), -1 après échecs répétés."""
    import requests
    for essai in range(1, essais + 1):
        try:
            rr = ses.get(url, accept="*/*", stream=True, params=params, timeout=(30, 180))
            with rr:
                if rr.status_code == 404:
                    return 0
                if rr.status_code != 200:
                    raise IOError(f"HTTP {rr.status_code}")
                n = 0
                with open(chemin, "wb") as f:
                    for bloc in rr.iter_content(1 << 20):
                        f.write(bloc)
                        n += len(bloc)
            attendu = int(rr.headers.get("Content-Length") or 0)
            if attendu and n != attendu:
                raise IOError(f"fichier incomplet ({n} / {attendu} octets)")
            return n
        except (requests.exceptions.RequestException, IOError) as e:
            attente = 20 * essai
            print(f"  {etiquette} : coupure ({str(e)[:80]}), essai {essai}/{essais}, nouvelle tentative dans {attente} s",
                  flush=True)
            if os.path.exists(chemin):
                os.remove(chemin)
            time.sleep(attente)
    return -1


# ------------------------------------------------------------------ principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modele", required=True)
    ap.add_argument("--sortie", default="publie")
    ap.add_argument("--run")
    ap.add_argument("--deja", default="", help="run déjà publié : on ne retraite pas un run plus ancien ou identique")
    ap.add_argument("--grib", help="dossier de GRIB déjà téléchargés (test hors ligne)")
    args = ap.parse_args()

    modeles = json.load(open("modeles.json", encoding="utf-8"))
    cfg = modeles[args.modele]
    params = JEUX[cfg["parametres"]]
    natif = float(cfg["grille"])
    # domaine découpé en tuiles de 1° ; bornes nord et est exclues (elles appartiennent aux tuiles voisines)
    S_, N_, O_, E_ = cfg["tuiles"]["domaine"]
    res_gros = cfg["tuiles"]["res"]
    # tuiles fines facultatives (ex. AROME à 0,025° sur les côtes) : le domaine est lu à la résolution fine,
    # les autres tuiles sont ensuite publiées un point sur deux
    res = cfg["tuiles"].get("res_fin", res_gros)
    zones_fines = cfg["tuiles"].get("zones_fines", [])
    zones = {"domaine": {"nom": "domaine", "s": S_, "n": N_ - res, "o": O_, "e": E_ - res, "res": res}}
    base = f"{API}/{cfg['api']}/v1/models/{cfg['modele']}"
    travail = tempfile.mkdtemp(prefix=args.modele + "_")
    t_debut = time.time()

    # ---- run et fichiers
    if args.grib:
        run = args.run or "2000-01-01T00:00:00Z"
        fichiers = sorted(glob.glob(os.path.join(args.grib, "*.grib2")) + glob.glob(os.path.join(args.grib, "*.nc")))
    else:
        from mf_api import Session
        ses = Session()
        r = ses.get(f"{base}/grids/{cfg['grille']}/packages/{cfg['paquets'][0]}")
        if r.status_code in (401, 403):
            sys.exit(f"Accès refusé (HTTP {r.status_code}) : clé expirée ou API non souscrite pour {cfg['api']}.")
        runs = sorted(set(re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r.text)))
        if cfg.get("heures_runs"):   # runs retenus seulement à ces heures (UTC)
            runs = [x for x in runs if int(x[11:13]) in cfg["heures_runs"]]
        candidats = [args.run] if args.run else [c for c in reversed(runs[-2:]) if c > args.deja]
        run, fichiers = None, []
        for c in candidats:   # le plus récent d'abord ; s'il est incomplet, le précédent
            fichiers = telecharger_run(ses, base, cfg, c, travail)
            if fichiers:
                run = c
                break
        if not run:
            print("Aucun run complet disponible pour le moment.")
            return ecrire_sortie(nouveau="non")
        print(f"Téléchargement du run {run} : {len(fichiers)} fichiers en {time.time()-t_debut:.0f} s", flush=True)

    # ---- décodage au fil de l'eau
    grille, sel, champs_zone = {}, None, {}
    if cfg["type"] == "fichier":
        sel, champs_zone, heures = lire_maillage(fichiers[0], params, zones, args.modele, run, couverture_min=0)
        if len(heures) < cfg["min_echeances"]:
            print(f"Run {run} incomplet : {len(heures)} échéances.")
            return ecrire_sortie(nouveau="non")
        if not args.grib:
            os.remove(fichiers[0])
        fichiers = []
    for chemin in fichiers:
        champs = {}
        lus, ret = lire_grib(chemin, params, champs, grille)
        if cfg["type"] == "groupes":
            print(f"  {os.path.basename(chemin)} : {ret}/{lus} messages retenus", flush=True)
        if sel is None and grille:
            sel = {}
            for zn, z in zones.items():
                if z.get("modeles") and args.modele not in z["modeles"]:
                    continue
                s = selection_zone(z, grille, natif)
                if s is None:
                    sys.exit(f"Domaine des tuiles {cfg['tuiles']['domaine']} hors de la grille du modèle : corriger modeles.json.")
                sel[zn] = s
                champs_zone[zn] = {}
        for nom, morceaux in champs.items():
            for cle, arr in morceaux.items():
                for zn, (lig, col, *_r) in sel.items():
                    champs_zone[zn].setdefault(nom, {})[cle] = arr[np.ix_(lig, col)].copy()
        if not args.grib:
            os.remove(chemin)
    if not sel:
        sys.exit("Aucune zone produite (aucun champ lu ou aucune zone dans le domaine).")

    # ---- assemblage : un cube (temps, lat, lon) par paramètre sur tout le domaine
    t_run = datetime.strptime(run, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    lig, col, lats, lons, pas_deg = sel["domaine"]
    champs = champs_zone["domaine"]
    toutes = sorted({cle[1] for m in champs.values() for cle in m})
    sorties = [h for h in heures_de_sortie(toutes, cfg["pas_sortie"]) if h >= 0]
    if not sorties:
        sys.exit("Aucune échéance lue.")
    cubes, variables = [], []
    for p in params:
        morceaux = champs.get(p["nom"], {})
        if not morceaux:
            continue
        cube = np.full((len(sorties), len(lig), len(col)), np.nan, dtype=np.float32)
        for t, h in enumerate(sorties):
            if p.get("cumul") and cfg["pas_sortie"] > 1:
                # cumul (somme) ou maximum sur l'intervalle depuis l'échéance de sortie précédente
                h0 = sorties[t - 1] if t else h - cfg["pas_sortie"]
                vals = [valeur_horaire(p, morceaux, hh) for hh in range(h0 + 1, h + 1)]
                vals = [v for v in vals if v is not None]
                if vals:
                    cube[t] = np.nansum(vals, axis=0) if p["cumul"] == "somme" else np.nanmax(vals, axis=0)
            else:
                v = valeur_horaire(p, morceaux, h)
                if v is not None:
                    cube[t] = v
        ok = np.isfinite(cube)
        txt = (f"min {np.nanmin(cube):8.1f}  moy {np.nanmean(cube):8.1f}  max {np.nanmax(cube):8.1f}  "
               f"({100 * ok.mean():.0f} % de valeurs)") if ok.any() else "aucune valeur"
        print(f"    [{p['nom']:25s}] {p['unite']:5s} {txt}", flush=True)
        cubes.append(cube)
        variables.append({"nom": p["nom"], "unite": p["unite"], "ech": p["ech"], "off": p["off"]})
    del champs_zone, champs
    sortie = os.path.join(args.sortie, args.modele)
    pas_gros = res_gros if zones_fines else pas_deg
    nb_tuiles, octets, fines = ecrire_tuiles(sortie, cubes, variables, lats, lons, pas_deg, pas_gros, zones_fines)
    print(f"  {nb_tuiles} tuiles de 1° ({len(fines)} à {pas_deg:.3f}°, les autres à {pas_gros:.3f}°), {len(sorties)} échéances, "
          f"{len(variables)} paramètres, {octets / 1e6:.1f} Mo compressés", flush=True)

    genere = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "modele": args.modele, "nom": cfg["nom"], "source": "Météo-France (licence Etalab)", "run": run, "genere": genere,
        "type": "vagues" if cfg["parametres"] in ("vagues", "ww3") else "atmo",
        "vagues": cfg.get("vagues", []), "force_par": cfg.get("force_par"), "pas_sortie_h": cfg["pas_sortie"],
        "res": round(pas_gros, 6), "n": int(round(1 / pas_gros)), "domaine": cfg["tuiles"]["domaine"],
        "res_fin": round(pas_deg, 6) if zones_fines else None, "tuiles_fines": fines,
        "temps": [(t_run + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in sorties],
        "variables": variables, "tuiles": sorted(os.path.basename(f)[:-7] for f in glob.glob(os.path.join(sortie, "t", "*.bin.gz"))),
        "format": "tuile = pour chaque variable (ordre de 'variables') un bloc int16 temps×lat(sud→nord)×lon(ouest→est), "
                  "valeur = entier×ech+off, -32768 = manquant ; octets faibles puis octets forts de tout le fichier, puis gzip",
    }
    with open(os.path.join(sortie, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    etat = {k: meta[k] for k in ("modele", "nom", "run", "genere", "type", "vagues", "force_par", "pas_sortie_h", "res", "domaine")}
    etat.update(tuiles=nb_tuiles, echeances=len(sorties), taille_mo=round(octets / 1e6, 1), variables=[v["nom"] for v in variables])
    with open(os.path.join(sortie, "run.json"), "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1)
    shutil.rmtree(travail, ignore_errors=True)
    print(f"Terminé : {args.modele} run {run} en {time.time()-t_debut:.0f} s.")
    ecrire_sortie(nouveau="oui", run=run)


def ecrire_tuiles(sortie, cubes, variables, lats, lons, pas, pas_gros=None, zones_fines=()):
    """Découpe les cubes du domaine en tuiles de 1° (clé « lat_lon » du coin sud-ouest) ; tuiles entièrement vides omises.
    Avec zones_fines ([sud, nord, ouest, est], …) : tuiles à la résolution du cube dans ces zones, un point sur k ailleurs
    (k = pas_gros / pas). La taille d'une tuile (n × n) se déduit de sa longueur à la lecture."""
    import gzip
    m = int(round(1 / pas))
    k = max(1, int(round((pas_gros or pas) / pas)))
    dossier = os.path.join(sortie, "t")
    shutil.rmtree(dossier, ignore_errors=True)
    os.makedirs(dossier)
    q = [None] * len(cubes)
    for kk, (cube, v) in enumerate(zip(cubes, variables)):
        e = np.round((cube - v["off"]) / v["ech"])
        q[kk] = np.where(np.isfinite(e), np.clip(e, -32767, 32767), MANQUANT).astype("<i2")
    nb = octets = 0
    fines = []
    for i in range(0, len(lats) - m + 1, m):
        for j in range(0, len(lons) - m + 1, m):
            la, lo = int(round(lats[i])), int(round(lons[j]))
            fine = k == 1 or any(z[0] <= la < z[1] and z[2] <= lo < z[3] for z in zones_fines)
            pas_t = 1 if fine else k
            blocs = [a[:, i:i + m:pas_t, j:j + m:pas_t] for a in q]
            if all((b == MANQUANT).all() for b in blocs):
                continue
            brut = np.concatenate([b.ravel() for b in blocs]).view(np.uint8).reshape(-1, 2)
            donnees = gzip.compress(np.concatenate([brut[:, 0], brut[:, 1]]).tobytes(), compresslevel=6, mtime=0)
            cle = f"{la}_{lo}"
            with open(os.path.join(dossier, cle + ".bin.gz"), "wb") as f:
                f.write(donnees)
            nb += 1
            octets += len(donnees)
            if fine and k > 1:
                fines.append(cle)
    return nb, octets, sorted(fines)


def ecrire_sortie(**kv):
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as go:
        for k, v in kv.items():
            go.write(f"{k}={v}\n")


if __name__ == "__main__":
    main()
