"""
IFS et WAM du CEP (ECMWF) en données ouvertes (0,25°, licence CC BY 4.0) -> tuiles de 1° au même format que les modèles.

Appelé par convertir.py pour les entrées de modeles.json qui ont "source": "cep". Une entrée (« ifs » ou « wam ») produit
plusieurs modèles publiés, un par domaine (« sorties » : Atlantique-Europe, Antilles, …), à partir d'un seul
téléchargement : on ne récupère que les champs utiles de chaque échéance (fichiers .index + requêtes partielles).

Fichiers : <racine>/<AAAAMMJJ>/<HH>z/ifs/0p25/<flux>/<AAAAMMJJHH>0000-<échéance>h-<flux>-fc.grib2 (et .index)
Échéances : runs de 00 et 12 UTC 0–144 h toutes les 3 h puis 150–240 h toutes les 6 h ; runs de 06 et 18 UTC 0–90 h.
"""
import glob
import json
import os
import time
from datetime import datetime, timedelta, timezone

RACINES = ["https://data.ecmwf.int/forecasts", "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"]
KT = 1.943844
# champs retenus par flux (nom court ECMWF -> rôle) ; les rafales s'appellent 10fg, 10fg3 ou 10fg6 selon l'échéance
CHAMPS = {
    "oper": ["10u", "10v", "2t", "2d", "msl", "tp", "tcc", "mucape"],
    "wave": ["swh", "mwd", "mwp", "pp1d"],
}


def est_rafale(p):
    return p.startswith("10fg") or p in ("fg310", "i10fg")


def echeances(heure, cfg):
    if heure in (0, 12):
        e = list(range(0, 145, 3)) + list(range(150, 241, 6))
    else:
        e = list(range(0, 91, 3))
    return [h for h in e if h <= cfg.get("horizon_h", 240)]


def url(racine, run_dt, flux, step, ext):
    d, h = run_dt.strftime("%Y%m%d"), run_dt.strftime("%H")
    return f"{racine}/{d}/{h}z/ifs/0p25/{flux}/{d}{h}0000-{step}h-{flux}-fc.{ext}"


def dernier_run(cfg, maintenant=None):
    """Run le plus récent dont la dernière échéance est publiée (« AAAA-MM-JJTHH:00:00Z »), ou None."""
    import requests
    t = (maintenant or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    for _ in range(6):
        if t.hour in cfg.get("heures_runs", [0, 6, 12, 18]):
            dern = echeances(t.hour, cfg)[-1]
            for racine in RACINES:
                try:
                    r = requests.get(url(racine, t, cfg["flux"], dern, "index"), timeout=30, stream=True)
                    r.close()
                    if r.status_code == 200:
                        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    pass
        t -= timedelta(hours=6)
    return None


def telecharger_echeance(ses, run_dt, flux, step, chemin, voulus):
    """Champs voulus d'une échéance, concaténés dans un fichier GRIB. Renvoie le nombre de champs."""
    for racine in RACINES:
        for essai in range(3):
            try:
                r = ses.get(url(racine, run_dt, flux, step, "index"), timeout=60)
                if r.status_code == 404:
                    break
                r.raise_for_status()
                lignes = [json.loads(x) for x in r.text.splitlines() if x.strip()]
                sel = [x for x in lignes if x.get("param") in voulus or est_rafale(x.get("param", ""))]
                sel = [x for x in sel if x.get("levtype", "sfc") == "sfc"]
                morceaux = []
                for x in sorted(sel, key=lambda x: x["_offset"]):
                    a, n = x["_offset"], x["_length"]
                    rr = ses.get(url(racine, run_dt, flux, step, "grib2"), timeout=120,
                                 headers={"Range": f"bytes={a}-{a + n - 1}"})
                    rr.raise_for_status()
                    if len(rr.content) != n:
                        raise IOError(f"morceau incomplet ({len(rr.content)} / {n})")
                    morceaux.append(rr.content)
                with open(chemin, "wb") as f:
                    for m in morceaux:
                        f.write(m)
                return len(morceaux)
            except Exception as e:
                print(f"  {flux} +{step} h ({racine.split('/')[2]}) : {str(e)[:80]}, essai {essai + 1}/3", flush=True)
                time.sleep(10 * (essai + 1))
    return 0


def convertir_cep(args, cfg, conv):
    """conv : module convertir (axes, selection_zone, ecrire_tuiles, ecrire_sortie, P_ATMO, P_VAGUES)."""
    import eccodes
    import numpy as np
    import requests
    from concurrent.futures import ThreadPoolExecutor

    t_debut = time.time()
    flux = cfg["flux"]
    run = args.run or dernier_run(cfg)
    if not run:
        print("Aucun run complet disponible pour le moment.")
        return conv.ecrire_sortie(nouveau="non")
    if args.deja and run <= args.deja and not args.run:
        print(f"Run {run} déjà publié.")
        return conv.ecrire_sortie(nouveau="non")
    run_dt = datetime.strptime(run, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    steps = echeances(run_dt.hour, cfg)
    print(f"Run {run} : {len(steps)} échéances ({steps[0]}–{steps[-1]} h), flux {flux}", flush=True)

    import tempfile
    travail = tempfile.mkdtemp(prefix=args.modele + "_")
    ses = requests.Session()
    voulus = set(CHAMPS[flux])

    def un(step):
        chemin = os.path.join(travail, f"{step:03d}.grib2")
        return step, chemin, telecharger_echeance(ses, run_dt, flux, step, chemin, voulus)

    fichiers = {}
    with ThreadPoolExecutor(max_workers=cfg.get("simultanes", 6)) as pool:
        for k, (step, chemin, n) in enumerate(pool.map(un, steps), 1):
            if n == 0:
                print(f"  +{step} h indisponible : run abandonné pour l'instant.")
                return conv.ecrire_sortie(nouveau="non")
            fichiers[step] = chemin
            if k % 10 == 0 or k == len(steps):
                print(f"  {k}/{len(steps)} échéances ({n} champs chacune), {time.time() - t_debut:.0f} s", flush=True)

    # ---- décodage : chaque champ découpé tout de suite sur chaque domaine de sortie
    sorties = cfg["sorties"]
    grille, sel = {}, {}
    donnees = {o: {} for o in sorties}          # sortie -> nom court -> échéance -> tableau 2D
    for step in steps:
        with open(fichiers[step], "rb") as f:
            while True:
                gid = eccodes.codes_grib_new_from_file(f)
                if gid is None:
                    break
                try:
                    g = lambda k: eccodes.codes_get(gid, k)
                    sn = g("shortName")
                    if est_rafale(sn):
                        sn = "10fg"
                    if not grille:
                        grille.update(ni=g("Ni"), nj=g("Nj"), lat1=g("latitudeOfFirstGridPointInDegrees"),
                                      lon1=g("longitudeOfFirstGridPointInDegrees"),
                                      di=g("iDirectionIncrementInDegrees"), dj=g("jDirectionIncrementInDegrees"),
                                      jpos=g("jScansPositively"))
                        for o, oc in sorties.items():
                            S_, N_, O_, E_ = oc["domaine"]
                            z = {"s": S_, "n": N_ - 0.25, "o": O_, "e": E_ - 0.25, "res": 0.25}
                            s = conv.selection_zone(z, grille, 0.25)
                            if s is None:
                                raise SystemExit(f"Domaine {oc['domaine']} hors de la grille CEP.")
                            sel[o] = s
                    vals = eccodes.codes_get_values(gid).astype(np.float32)
                    if g("bitmapPresent"):
                        vals[vals == g("missingValue")] = np.nan
                    arr = vals.reshape(grille["nj"], grille["ni"])
                    for o, (lig, col, *_r) in sel.items():
                        donnees[o].setdefault(sn, {})[step] = arr[np.ix_(lig, col)].copy()
                finally:
                    eccodes.codes_release(gid)
        os.remove(fichiers[step])

    defs = {p["nom"]: p for p in (conv.P_ATMO + conv.P_VAGUES)}
    genere = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for o, oc in sorties.items():
        lig, col, lats, lons, pas = sel[o]
        d = donnees[o]
        T = len(steps)
        shape = (T, len(lig), len(col))

        def cube(sn, f=lambda a: a):
            c = np.full(shape, np.nan, dtype=np.float32)
            for t, s in enumerate(steps):
                if s in d.get(sn, {}):
                    c[t] = f(d[sn][s])
            return c

        cubes = {}
        if flux == "oper":
            u, v = cube("10u"), cube("10v")
            cubes["wind_speed_10m"] = np.hypot(u, v) * KT
            cubes["wind_direction_10m"] = (np.degrees(np.arctan2(-u, -v)) + 360) % 360
            cubes["wind_gusts_10m"] = cube("10fg") * KT                  # maximum depuis l'échéance précédente
            cubes["temperature_2m"] = cube("2t") - 273.15
            cubes["dew_point_2m"] = cube("2d") - 273.15
            cubes["pressure_msl"] = cube("msl") / 100
            tp = cube("tp") * 1000                                         # cumul depuis le début du run, m -> mm
            pr = np.full(shape, np.nan, dtype=np.float32)
            pr[1:] = np.maximum(tp[1:] - tp[:-1], 0)                       # cumul sur le pas
            cubes["precipitation"] = pr
            cc = cube("tcc")
            cubes["cloud_cover"] = cc * 100 if np.nanmax(cc) <= 1.01 else cc
            cubes["cape"] = cube("mucape")
        else:
            cubes["wave_height"] = cube("swh")
            cubes["wave_direction"] = cube("mwd")
            cubes["wave_period"] = cube("mwp")
            cubes["wave_peak_period"] = cube("pp1d")
        noms = [k for k, c in cubes.items() if np.isfinite(c).any()]
        variables = [{"nom": k, "unite": defs[k]["unite"], "ech": defs[k]["ech"], "off": defs[k]["off"]} for k in noms]
        print(f"  [{o}] " + ", ".join(f"{k} {np.nanmin(cubes[k]):.1f}…{np.nanmax(cubes[k]):.1f}" for k in noms), flush=True)
        sortie = os.path.join(args.sortie, o)
        nb, octets, _ = conv.ecrire_tuiles(sortie, [cubes[k] for k in noms], variables, lats, lons, pas, pas, (),
                                           oc.get("garder"))
        print(f"  [{o}] {nb} tuiles de 1° à 0,25°, {T} échéances, {octets / 1e6:.1f} Mo compressés", flush=True)
        meta = {
            "modele": o, "nom": oc["nom"], "source": "ECMWF open data (CC BY 4.0)", "run": run, "genere": genere,
            "type": "vagues" if flux == "wave" else "atmo", "vagues": oc.get("vagues", []), "force_par": oc.get("force_par"),
            "pas_sortie_h": 3, "res": 0.25, "n": 4, "domaine": oc["domaine"], "res_fin": None, "tuiles_fines": [],
            "temps": [(run_dt + timedelta(hours=s)).strftime("%Y-%m-%dT%H:%M") for s in steps],
            "variables": variables,
            "tuiles": sorted(os.path.basename(f)[:-7] for f in glob.glob(os.path.join(sortie, "t", "*.bin.gz"))),
            "format": "tuile = pour chaque variable (ordre de 'variables') un bloc int16 temps×lat(sud→nord)×lon(ouest→est), "
                      "valeur = entier×ech+off, -32768 = manquant ; octets faibles puis octets forts de tout le fichier, puis gzip",
        }
        with open(os.path.join(sortie, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
        etat = {k: meta[k] for k in ("modele", "nom", "run", "genere", "type", "vagues", "force_par", "pas_sortie_h", "res", "domaine")}
        etat.update(tuiles=nb, echeances=T, taille_mo=round(octets / 1e6, 1), variables=noms)
        with open(os.path.join(sortie, "run.json"), "w", encoding="utf-8") as f:
            json.dump(etat, f, ensure_ascii=False, indent=1)
    print(f"Terminé : {args.modele} run {run} en {time.time() - t_debut:.0f} s.")
    conv.ecrire_sortie(nouveau="oui", run=run)
