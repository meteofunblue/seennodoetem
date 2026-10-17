"""
Conversion AROME-IFS (paquets SP1 + SP2, grille 0,025°) en fichiers légers pour l'appli.

Usage :
  python convertir_aromeifs.py --sortie publie            # dernier run disponible
  python convertir_aromeifs.py --sortie publie --forcer   # même s'il est déjà publié
  python convertir_aromeifs.py --sortie publie --grib dossier_local   # sans téléchargement (test)

Produit dans <sortie>/aromeifs/ :
  <zone>.json   un fichier par zone (grille, échéances, paramètres compressés en entiers 16 bits)
  run.json      état : run publié, date de génération, zones, paramètres
"""
import argparse
import base64
import glob
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import numpy as np

# ------------------------------------------------------------------ réglages
GRILLE = "0.025"
PAS = 0.025
GROUPES = ["00H06H", "07H12H", "13H18H", "19H24H", "25H30H", "31H36H", "37H42H", "43H48H", "49H51H"]
PAQUETS = ["SP1", "SP2"]

# Zones : sud, nord, ouest, est (degrés) et facteur de sous-échantillonnage (1 = 0,025° ≈ 2,5 km)
ZONES = {
    "iroise":   {"nom": "Mer d'Iroise",           "s": 47.75, "n": 48.75, "o": -5.6, "e": -4.2, "k": 1},
    "bretagne": {"nom": "Bretagne",               "s": 46.9,  "n": 49.2,  "o": -6.3, "e": -1.8, "k": 3},
    "manche":   {"nom": "Manche",                 "s": 48.4,  "n": 51.2,  "o": -6.0, "e": 1.8,  "k": 4},
    "gascogne": {"nom": "Golfe de Gascogne",      "s": 43.3,  "n": 48.0,  "o": -6.5, "e": -1.0, "k": 4},
    "med":      {"nom": "Méditerranée NW / Corse", "s": 41.2, "n": 43.8,  "o": 2.8,  "e": 10.0, "k": 4},
}

# Paramètres retenus, repérés par (discipline, catégorie, numéro, type de niveau, type de pas).
# nom = nom utilisé par l'appli (mêmes noms et unités que les autres modèles)
# conv = conversion d'unité ; ech = échelle de stockage (valeur = entier × ech + off)
PARAMS = {
    (0, 3, 1, "meanSea", "instant"):               dict(nom="pressure_msl",       conv=lambda v: v / 100,    ech=0.1, off=1000, unite="hPa"),
    (0, 2, 1, "heightAboveGround", "instant"):     dict(nom="wind_speed_10m",     conv=lambda v: v * 1.943844, ech=0.1, off=0, unite="kt"),
    (0, 2, 0, "heightAboveGround", "instant"):     dict(nom="wind_direction_10m", conv=lambda v: v,          ech=1,   off=0, unite="°"),
    (0, 2, 22, "heightAboveGround", "max"):        dict(nom="wind_gusts_10m",     conv=lambda v: v * 1.943844, ech=0.1, off=0, unite="kt", cumul="max"),
    (0, 0, 0, "heightAboveGround", "instant"):     dict(nom="temperature_2m",     conv=lambda v: v - 273.15, ech=0.1, off=0, unite="°C"),
    (0, 0, 6, "heightAboveGround", "instant"):     dict(nom="dew_point_2m",       conv=lambda v: v - 273.15, ech=0.1, off=0, unite="°C"),
    (0, 1, 52, "surface", "accum"):                dict(nom="precipitation",      conv=lambda v: v,          ech=0.01, off=0, unite="mm", cumul="somme"),
    (0, 6, 1, "surface", "instant"):               dict(nom="cloud_cover",        conv=lambda v: v,          ech=1,   off=0, unite="%"),
    (0, 6, 3, "surface", "instant"):               dict(nom="cloud_cover_low",    conv=lambda v: v,          ech=1,   off=0, unite="%"),
    (0, 6, 4, "surface", "instant"):               dict(nom="cloud_cover_mid",    conv=lambda v: v,          ech=1,   off=0, unite="%"),
    (0, 6, 5, "surface", "instant"):               dict(nom="cloud_cover_high",   conv=lambda v: v,          ech=1,   off=0, unite="%"),
    (0, 7, 6, None, "instant"):                    dict(nom="cape",               conv=lambda v: v,          ech=1,   off=0, unite="J/kg"),
}
MANQUANT = -32768


# ------------------------------------------------------------------ décodage GRIB
def lire_grib(chemin, champs, grille_info):
    """Lit un fichier GRIB2 multi-messages et range les champs retenus.
    champs[nom][(debut, fin)] = tableau 2D (ou champs[nom][(None, h)] pour un champ instantané)."""
    import eccodes
    n_lus = n_retenus = 0
    with open(chemin, "rb") as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                n_lus += 1
                g = lambda k: eccodes.codes_get(gid, k)
                cle = (g("discipline"), g("parameterCategory"), g("parameterNumber"), g("typeOfLevel"), g("stepType"))
                p = PARAMS.get(cle) or PARAMS.get(cle[:3] + (None, cle[4]))
                if p is None:
                    continue
                if cle[3] == "heightAboveGround" and g("level") not in (2, 10):
                    continue
                if not grille_info:
                    grille_info.update(ni=g("Ni"), nj=g("Nj"),
                                       lat1=g("latitudeOfFirstGridPointInDegrees"),
                                       lon1=g("longitudeOfFirstGridPointInDegrees"),
                                       lat2=g("latitudeOfLastGridPointInDegrees"),
                                       lon2=g("longitudeOfLastGridPointInDegrees"))
                vals = eccodes.codes_get_values(gid).astype(np.float32)
                if g("bitmapPresent"):
                    vals[vals == g("missingValue")] = np.nan
                arr = vals.reshape(g("Nj"), g("Ni"))
                sr = str(g("stepRange"))
                if "-" in sr:
                    a, b = (int(x) for x in sr.split("-"))
                else:
                    a, b = None, int(sr)
                if cle[4] == "instant":
                    a = None
                champs.setdefault(p["nom"], {})[(a, b)] = p["conv"](arr)
                n_retenus += 1
            finally:
                eccodes.codes_release(gid)
    return n_lus, n_retenus


def valeur_horaire(nom, morceaux, h):
    """Valeur sur l'heure qui se termine à h, à partir des intervalles disponibles."""
    p = next(v for v in PARAMS.values() if v["nom"] == nom)
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
        return morceaux[(0, h)]  # maximum depuis le début du run : à défaut de mieux
    return None


# ------------------------------------------------------------------ zones
def indices_zone(z, gi):
    """Indices de lignes / colonnes de la grille globale pour une zone (lignes du nord au sud dans le GRIB)."""
    lon1 = gi["lon1"] - 360 if gi["lon1"] > 180 else gi["lon1"]
    lat1 = gi["lat1"]
    nord_vers_sud = gi["lat1"] > gi["lat2"]
    def j_de(lat):
        return round((lat1 - lat) / PAS) if nord_vers_sud else round((lat - lat1) / PAS)
    def i_de(lon):
        return round((lon - lon1) / PAS)
    i0, i1 = sorted((max(0, i_de(z["o"])), min(gi["ni"] - 1, i_de(z["e"]))))
    j0, j1 = sorted((max(0, j_de(z["n"])), min(gi["nj"] - 1, j_de(z["s"]))))
    k = z["k"]
    cols = np.arange(i0, i1 + 1, k)
    lignes = np.arange(j0, j1 + 1, k)
    lats = lat1 - lignes * PAS if nord_vers_sud else lat1 + lignes * PAS
    ordre = np.argsort(lats)  # du sud vers le nord pour l'appli
    lignes, lats = lignes[ordre], lats[ordre]
    lons = lon1 + cols * PAS
    return lignes, cols, lats, lons


def encoder(cube, ech, off):
    q = np.round((cube - off) / ech)
    q = np.where(np.isfinite(q), np.clip(q, -32767, 32767), MANQUANT).astype("<i2")
    return base64.b64encode(q.tobytes()).decode("ascii")


# ------------------------------------------------------------------ principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sortie", default="publie")
    ap.add_argument("--forcer", action="store_true")
    ap.add_argument("--grib", help="dossier contenant des GRIB déjà téléchargés (test, sans API)")
    ap.add_argument("--run", help="run à traiter, ex. 2026-10-17T06:00:00Z (sinon le dernier)")
    ap.add_argument("--deja-publie", help="fichier run.json actuellement publié (pour éviter de refaire le même run)")
    args = ap.parse_args()

    sortie = os.path.join(args.sortie, "aromeifs")
    os.makedirs(sortie, exist_ok=True)
    travail = tempfile.mkdtemp(prefix="aromeifs_")

    # ---- choix du run et téléchargement
    if args.grib:
        run = args.run or "2000-01-01T00:00:00Z"
        fichiers = sorted(glob.glob(os.path.join(args.grib, "*.grib2")))
    else:
        from mf_api import Session
        ses = Session()
        runs = ses.runs_disponibles(GRILLE, "SP1")
        if not runs:
            sys.exit("Aucun run annoncé par le catalogue.")
        run = args.run or runs[-1]
        print(f"Runs disponibles : {', '.join(runs[-4:])}  ->  run choisi : {run}", flush=True)
        if args.deja_publie and os.path.exists(args.deja_publie) and not args.forcer:
            try:
                if json.load(open(args.deja_publie)).get("run") == run:
                    print("Ce run est déjà publié : rien à faire.")
                    with open(os.path.join(os.environ.get("GITHUB_OUTPUT", os.devnull), ), "a") as go:
                        go.write("nouveau=non\n")
                    return
            except Exception:
                pass
        fichiers = []
        for groupe in GROUPES:
            for paquet in PAQUETS:
                chemin = os.path.join(travail, f"{paquet}_{groupe}.grib2")
                t0 = time.time()
                taille = ses.telecharger(GRILLE, paquet, run, groupe, chemin)
                if taille:
                    print(f"  {paquet} {groupe} : {taille/1e6:.0f} Mo en {time.time()-t0:.0f} s", flush=True)
                    fichiers.append(chemin)
        if not fichiers:
            sys.exit("Aucun fichier téléchargé.")

    # ---- décodage, au fur et à mesure (chaque GRIB est supprimé après lecture s'il vient de l'API)
    grille_info, champs_zone = {}, {zn: {} for zn in ZONES}
    idx = None
    for chemin in fichiers:
        champs = {}
        n_lus, n_ret = lire_grib(chemin, champs, grille_info)
        print(f"  {os.path.basename(chemin)} : {n_ret}/{n_lus} messages retenus", flush=True)
        if idx is None and grille_info:
            idx = {zn: indices_zone(z, grille_info) for zn, z in ZONES.items()}
        for nom, morceaux in champs.items():
            for cle, arr in morceaux.items():
                for zn, (lignes, cols, _, _) in idx.items():
                    champs_zone[zn].setdefault(nom, {})[cle] = arr[np.ix_(lignes, cols)].copy()
        if not args.grib:
            os.remove(chemin)

    # ---- assemblage par zone
    t_run = datetime.strptime(run, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    heures = sorted({cle[1] for z in champs_zone.values() for m in z.values() for cle in m})
    if not heures:
        sys.exit("Aucun champ exploitable dans les fichiers.")
    temps = [(t_run + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in heures]
    noms = [p["nom"] for p in PARAMS.values()]
    resume = {}
    for zn, z in ZONES.items():
        lignes, cols, lats, lons = idx[zn]
        ny, nx = len(lignes), len(cols)
        variables = {}
        for nom in noms:
            morceaux = champs_zone[zn].get(nom, {})
            if not morceaux:
                continue
            p = next(v for v in PARAMS.values() if v["nom"] == nom)
            cube = np.full((len(heures), ny, nx), np.nan, dtype=np.float32)
            for t, h in enumerate(heures):
                v = valeur_horaire(nom, morceaux, h)
                if v is not None:
                    cube[t] = v
            variables[nom] = {"unite": p["unite"], "ech": p["ech"], "off": p["off"],
                              "data": encoder(cube, p["ech"], p["off"])}
            if zn == next(iter(ZONES)):  # contrôle de vraisemblance, affiché pour la première zone
                ok = np.isfinite(cube)
                if ok.any():
                    print(f"    {nom:20s} {p['unite']:5s} min {np.nanmin(cube):8.1f}  moy {np.nanmean(cube):8.1f}  "
                          f"max {np.nanmax(cube):8.1f}  ({100 * ok.mean():.0f} % de valeurs)", flush=True)
                else:
                    print(f"    {nom:20s} aucune valeur", flush=True)
        doc = {
            "modele": "AROME-IFS", "source": "Météo-France (licence Etalab)", "run": run,
            "genere": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "zone": {"id": zn, "nom": z["nom"], "lat0": round(float(lats[0]), 4), "lon0": round(float(lons[0]), 4),
                     "dlat": PAS * z["k"], "dlon": PAS * z["k"], "ny": ny, "nx": nx},
            "temps": temps, "manquant": MANQUANT, "ordre": "temps, lat (sud→nord), lon (ouest→est)",
            "variables": variables,
        }
        chemin = os.path.join(sortie, f"{zn}.json")
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        resume[zn] = {"nom": z["nom"], "points": ny * nx, "taille_ko": round(os.path.getsize(chemin) / 1024)}
        print(f"  zone {zn} : {ny}×{nx} points, {len(heures)} échéances, {len(variables)} paramètres, "
              f"{resume[zn]['taille_ko']} Ko", flush=True)

    with open(os.path.join(sortie, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"modele": "AROME-IFS", "run": run, "genere": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "echeances": len(heures), "zones": resume, "parametres": noms}, f, ensure_ascii=False, indent=1)
    shutil.rmtree(travail, ignore_errors=True)
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as go:
        go.write("nouveau=oui\n")
        go.write(f"run={run}\n")
    print(f"Terminé : run {run}, {len(heures)} échéances.")


if __name__ == "__main__":
    main()
