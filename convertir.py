"""
Convertisseur commun : paquets GRIB2 Météo-France -> fichiers légers par zone pour l'appli.

  python convertir.py --modele arome --sortie publie            # dernier run complet
  python convertir.py --modele arome --sortie publie --run 2026-10-17T06:00:00Z
  python convertir.py --modele arome --sortie publie --grib dossier   # test hors ligne

Configuration : modeles.json (sources) et zones.json (zones). Produit <sortie>/<modele>/<zone>.json et run.json.
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
    dict(cle=(0, 3, 1, "meanSea", "instant"),            nom="pressure_msl",       conv=lambda v: v / 100,  ech=0.1,  off=1000, unite="hPa"),
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
JEUX = {"atmo": P_ATMO, "vagues": P_VAGUES}


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
    if cfg["type"] == "groupes":
        toks = [t for t in toks if t.count("H") == 2 and "999" not in t]
    else:
        toks = [t for t in toks if t.count("H") == 1]
    return sorted(toks, key=bornes)


def heures_de_sortie(heures, pas):
    return [h for h in heures if h % pas == 0] if pas > 1 else list(heures)


def telecharger_run(ses, base, cfg, run, travail):
    """Télécharge tous les fichiers d'un run. Renvoie la liste des chemins, ou [] si le run est incomplet."""
    liste = echeances_du_run(ses, base, cfg, run)
    if cfg["type"] == "echeances":
        liste = [t for t in liste if bornes(t)[1] % cfg["pas_sortie"] == 0]
        assez = len(liste) >= cfg["min_echeances"]
    else:
        fin = max((bornes(t)[1] for t in liste), default=0)
        assez = len(heures_de_sortie(range(0, fin + 1), cfg["pas_sortie"])) >= cfg["min_echeances"]
    print(f"Run {run} : {len(liste)} fichiers annoncés{'' if assez else ' (incomplet)'}", flush=True)
    if not assez:
        return []
    fichiers = []
    for tok in liste:
        for paquet in cfg["paquets"]:
            chemin = os.path.join(travail, f"{paquet}_{tok}.grib2")
            t0 = time.time()
            n = telecharger_fichier(ses, f"{base}/grids/{cfg['grille']}/packages/{paquet}/{cfg['produit']}",
                                    {"referencetime": run, "time": tok, "format": "grib2"}, chemin, f"{paquet} {tok}")
            if n <= 0:
                print(f"  {paquet} {tok} : indisponible — run {run} abandonné pour l'instant.", flush=True)
                for fch in fichiers:
                    os.remove(fch)
                return []
            if cfg["type"] == "groupes" or len(fichiers) % 10 == 0:
                print(f"  {paquet} {tok} : {n/1e6:.0f} Mo en {time.time()-t0:.0f} s", flush=True)
            fichiers.append(chemin)
    return fichiers


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
    zones = {k: v for k, v in json.load(open("zones.json", encoding="utf-8")).items() if not k.startswith("_")}
    cfg = modeles[args.modele]
    params = JEUX[cfg["parametres"]]
    natif = float(cfg["grille"])
    base = f"{API}/{cfg['api']}/v1/models/{cfg['modele']}"
    travail = tempfile.mkdtemp(prefix=args.modele + "_")
    t_debut = time.time()

    # ---- run et fichiers
    if args.grib:
        run = args.run or "2000-01-01T00:00:00Z"
        fichiers = sorted(glob.glob(os.path.join(args.grib, "*.grib2")))
    else:
        from mf_api import Session
        ses = Session()
        r = ses.get(f"{base}/grids/{cfg['grille']}/packages/{cfg['paquets'][0]}")
        if r.status_code in (401, 403):
            sys.exit(f"Accès refusé (HTTP {r.status_code}) : clé expirée ou API non souscrite pour {cfg['api']}.")
        runs = sorted(set(re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r.text)))
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
                    print(f"  zone {zn} hors du domaine du modèle : ignorée")
                    continue
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

    # ---- assemblage
    t_run = datetime.strptime(run, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    sortie = os.path.join(args.sortie, args.modele)
    os.makedirs(sortie, exist_ok=True)
    resume, noms = {}, [p["nom"] for p in params]
    premiere = next(iter(sel))
    for zn, (lig, col, lats, lons, pas_deg) in sel.items():
        toutes = sorted({cle[1] for m in champs_zone[zn].values() for cle in m})
        sorties = heures_de_sortie(toutes, cfg["pas_sortie"])
        if not sorties:
            continue
        variables = {}
        for p in params:
            morceaux = champs_zone[zn].get(p["nom"], {})
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
            variables[p["nom"]] = {"unite": p["unite"], "ech": p["ech"], "off": p["off"],
                                   "data": encoder(cube, p["ech"], p["off"])}
            if zn == premiere:
                ok = np.isfinite(cube)
                txt = (f"min {np.nanmin(cube):8.1f}  moy {np.nanmean(cube):8.1f}  max {np.nanmax(cube):8.1f}  "
                       f"({100 * ok.mean():.0f} % de valeurs)") if ok.any() else "aucune valeur"
                print(f"    [{zn}] {p['nom']:22s} {p['unite']:5s} {txt}", flush=True)
        doc = {
            "modele": args.modele, "nom_modele": cfg["nom"], "source": "Météo-France (licence Etalab)", "run": run,
            "genere": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "pas_sortie_h": cfg["pas_sortie"],
            "zone": {"id": zn, "nom": zones[zn]["nom"], "lat0": round(float(lats[0]), 4), "lon0": round(float(lons[0]), 4),
                     "dlat": round(pas_deg, 6), "dlon": round(pas_deg, 6), "ny": len(lig), "nx": len(col)},
            "temps": [(t_run + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in sorties],
            "manquant": MANQUANT, "ordre": "temps, lat (sud→nord), lon (ouest→est)", "variables": variables,
        }
        chemin = os.path.join(sortie, f"{zn}.json")
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        resume[zn] = {"nom": zones[zn]["nom"], "points": len(lig) * len(col), "pas_deg": round(pas_deg, 4),
                      "echeances": len(sorties), "taille_ko": round(os.path.getsize(chemin) / 1024)}
        print(f"  zone {zn} : {len(lig)}×{len(col)} points à {pas_deg:.3f}°, {len(sorties)} échéances, "
              f"{len(variables)} paramètres, {resume[zn]['taille_ko']} Ko", flush=True)

    etat = {"modele": args.modele, "nom": cfg["nom"], "run": run, "parametres": cfg["parametres"],
            "vagues": cfg.get("vagues"), "pas_sortie_h": cfg["pas_sortie"],
            "genere": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "zones": resume, "variables": noms}
    with open(os.path.join(sortie, "run.json"), "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1)
    shutil.rmtree(travail, ignore_errors=True)
    print(f"Terminé : {args.modele} run {run} en {time.time()-t_debut:.0f} s.")
    ecrire_sortie(nouveau="oui", run=run)


def ecrire_sortie(**kv):
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as go:
        for k, v in kv.items():
            go.write(f"{k}={v}\n")


if __name__ == "__main__":
    main()
