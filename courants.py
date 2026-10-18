"""
Courants de surface Copernicus Marine (IBI, Méditerranée, global) -> tuiles de 1° au même format que les modèles.

Appelé par convertir.py pour les entrées de modeles.json qui ont "source": "cmems".
Identifiants : variables d'environnement COPERNICUSMARINE_SERVICE_USERNAME et COPERNICUSMARINE_SERVICE_PASSWORD
(secrets GitHub CMEMS_USER et CMEMS_PASS), jamais écrits dans un fichier.

Variables publiées : current_speed (kt) et current_direction (°, direction VERS laquelle porte le courant,
convention océanographique). Marée incluse dans les jeux horaires IBI, MED et global (merged-uv).
"""
import glob
import json
import os
import time
from datetime import datetime, timedelta, timezone


KT = 1.943844
# composantes cherchées dans le jeu, dans cet ordre (merged-uv : utotal = courant + marée + dérive de Stokes)
CAND_U = ["utotal", "uo", "u"]
CAND_V = ["vtotal", "vo", "v"]


def run_du_jour(cfg, maintenant=None):
    """Bulletin quotidien : aujourd'hui 00 h UTC si l'heure de mise à disposition est passée, sinon hier."""
    t = maintenant or datetime.now(timezone.utc)
    j = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if t.hour < cfg.get("heure_dispo", 15):
        j -= timedelta(days=1)
    return j


def convertir_courant(args, cfg, ecrire_tuiles, ecrire_sortie):
    import copernicusmarine
    import numpy as np

    t_debut = time.time()
    if not os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME"):
        raise SystemExit("Identifiants Copernicus Marine absents : secrets CMEMS_USER et CMEMS_PASS à créer dans le dépôt.")
    run_dt = run_du_jour(cfg)
    run = run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.deja and run <= args.deja and not args.run:
        print(f"Bulletin {run} déjà publié.")
        return ecrire_sortie(nouveau="non")

    S_, N_, O_, E_ = cfg["tuiles"]["domaine"]
    res = cfg["tuiles"]["res"]
    horizon = cfg.get("horizon_h", 96)
    marge = 0.1
    print(f"Ouverture de {cfg['dataset']} sur {cfg['tuiles']['domaine']}", flush=True)
    ds = copernicusmarine.open_dataset(
        dataset_id=cfg["dataset"],
        minimum_longitude=O_ - marge, maximum_longitude=E_ + marge,
        minimum_latitude=S_ - marge, maximum_latitude=N_ + marge,
        start_datetime=(run_dt - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        end_datetime=(run_dt + timedelta(hours=horizon + 1)).strftime("%Y-%m-%dT%H:%M:%S"),
    )
    print(f"  variables du jeu : {', '.join(ds.data_vars)}", flush=True)
    print(f"  dimensions : {dict(ds.sizes)}", flush=True)
    nu = next((v for v in CAND_U if v in ds.data_vars), None)
    nv = next((v for v in CAND_V if v in ds.data_vars), None)
    if not nu or not nv:
        raise SystemExit(f"Composantes du courant introuvables dans {cfg['dataset']} (cherché {CAND_U} / {CAND_V}).")
    print(f"  composantes retenues : {nu}, {nv}", flush=True)
    ds = ds[[nu, nv]]
    if "depth" in ds.dims:                       # surface : premier niveau
        ds = ds.isel(depth=0)
    lat_n = "latitude" if "latitude" in ds.coords else "lat"
    lon_n = "longitude" if "longitude" in ds.coords else "lon"

    # grille de publication alignée sur les degrés entiers (bornes nord et est exclues)
    lats = np.round(S_ + np.arange(int(round((N_ - S_) / res))) * res, 6)
    lons = np.round(O_ + np.arange(int(round((E_ - O_) / res))) * res, 6)
    # plus proche voisin : la côte reste nette (pas de mélange terre / mer)
    src_lat = ds[lat_n].values
    src_lon = ds[lon_n].values
    pas_src = float(np.median(np.abs(np.diff(src_lat))))
    tol = max(res, pas_src) * 0.75

    def proches(src, cible):
        ordre = np.argsort(src)
        s = src[ordre]
        k = np.clip(np.searchsorted(s, cible), 1, len(s) - 1)
        k = np.where(np.abs(s[k - 1] - cible) <= np.abs(s[k] - cible), k - 1, k)
        return ordre[k], np.abs(s[k] - cible) <= tol

    i_lat, ok_lat = proches(src_lat, lats)
    i_lon, ok_lon = proches(src_lon, lons)
    ds = ds.isel({lat_n: i_lat, lon_n: i_lon})
    hors = ~(ok_lat[:, None] & ok_lon[None, :])          # points de la grille sans donnée source proche

    # heures : moyennes horaires (horodatées à la demi-heure) rattachées à l'heure qui les termine
    temps = ds["time"].values.astype("datetime64[m]").astype(datetime)
    heures = [(t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc) for t in temps]
    garder_t = [i for i, h in enumerate(heures) if run_dt <= h <= run_dt + timedelta(hours=horizon)]
    if len(garder_t) < cfg.get("min_echeances", 24):
        print(f"Bulletin {run} incomplet : {len(garder_t)} échéances horaires disponibles.")
        return ecrire_sortie(nouveau="non")
    ds = ds.isel(time=garder_t)
    heures = [heures[i] for i in garder_t]
    print(f"  {len(heures)} échéances de {heures[0]:%d/%m %H} h à {heures[-1]:%d/%m %H} h UTC · téléchargement…", flush=True)
    u = ds[nu].transpose("time", lat_n, lon_n).values.astype(np.float32)        # (temps, lat, lon), NaN sur terre
    v = ds[nv].transpose("time", lat_n, lon_n).values.astype(np.float32)
    u[:, hors] = np.nan
    v[:, hors] = np.nan
    vit = np.hypot(u, v) * KT
    direc = (np.degrees(np.arctan2(u, v)) + 360) % 360            # vers où porte le courant
    direc = np.where(np.isfinite(vit), direc, np.nan).astype(np.float32)
    for nom, c in (("vitesse", vit), ("direction", direc)):
        ok = np.isfinite(c)
        print(f"    [{nom:10s}] min {np.nanmin(c):6.2f}  moy {np.nanmean(c):6.2f}  max {np.nanmax(c):6.2f}  ({100 * ok.mean():.0f} % de mer)",
              flush=True)

    variables = [{"nom": "current_speed", "unite": "kt", "ech": 0.01, "off": 0},
                 {"nom": "current_direction", "unite": "° vers", "ech": 1, "off": 0}]
    sortie = os.path.join(args.sortie, args.modele)
    nb, octets, _ = ecrire_tuiles(sortie, [vit, direc], variables, lats, lons, res, res, (), cfg["tuiles"].get("garder"))
    print(f"  {nb} tuiles de 1° à {res}°, {len(heures)} échéances, {octets / 1e6:.1f} Mo compressés", flush=True)

    genere = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "modele": args.modele, "nom": cfg["nom"], "source": "Copernicus Marine Service (E.U. Copernicus Marine Service Information)",
        "run": run, "genere": genere, "type": "courant", "vagues": [], "force_par": None, "pas_sortie_h": 1,
        "res": res, "n": int(round(1 / res)), "domaine": cfg["tuiles"]["domaine"], "res_fin": None, "tuiles_fines": [],
        "jeu": cfg["dataset"],
        "temps": [h.strftime("%Y-%m-%dT%H:%M") for h in heures],
        "variables": variables,
        "tuiles": sorted(os.path.basename(f)[:-7] for f in glob.glob(os.path.join(sortie, "t", "*.bin.gz"))),
        "format": "tuile = pour chaque variable (ordre de 'variables') un bloc int16 temps×lat(sud→nord)×lon(ouest→est), "
                  "valeur = entier×ech+off, -32768 = manquant ; octets faibles puis octets forts de tout le fichier, puis gzip",
    }
    with open(os.path.join(sortie, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    etat = {k: meta[k] for k in ("modele", "nom", "run", "genere", "type", "vagues", "force_par", "pas_sortie_h", "res", "domaine")}
    etat.update(tuiles=nb, echeances=len(heures), taille_mo=round(octets / 1e6, 1), variables=[x["nom"] for x in variables])
    with open(os.path.join(sortie, "run.json"), "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1)
    print(f"Terminé : {args.modele} bulletin {run} en {time.time() - t_debut:.0f} s.")
    ecrire_sortie(nouveau="oui", run=run)
