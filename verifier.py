"""
Détermine quels modèles ont un run plus récent que celui publié.
Écrit dans GITHUB_OUTPUT : liste=[{"modele": "...", "deja": "..."}, ...]  (vide : [])
Variables : MODELES (liste facultative, séparée par des virgules), FORCER (true/false), GITHUB_REPOSITORY.
"""
import json
import os
import re

import requests

from mf_api import Session

API = "https://public-api.meteofrance.fr/previnum"
modeles = {k: v for k, v in json.load(open("modeles.json", encoding="utf-8")).items() if not k.startswith("_")}
choix = [m.strip() for m in os.environ.get("MODELES", "").split(",") if m.strip()]
forcer = os.environ.get("FORCER", "false") == "true"
depot = os.environ.get("GITHUB_REPOSITORY", "/")
proprio, nom_depot = depot.split("/", 1)
site = f"https://{proprio.lower()}.github.io/{nom_depot}"

ses = Session()
a_traiter = []
for nom, cfg in modeles.items():
    if choix and nom not in choix:
        continue
    if not cfg.get("actif", True) and not choix:
        continue
    if cfg.get("source") == "cmems":
        # courants Copernicus : un bulletin par jour, disponible vers heure_dispo UTC
        from courants import run_du_jour
        runs = [run_du_jour(cfg).strftime("%Y-%m-%dT%H:%M:%SZ")]
        r = type("R", (), {"status_code": 200})()
    else:
      base = f"{API}/{cfg['api']}/v1/models/{cfg['modele']}"
      try:
        r = ses.get(f"{base}/grids/{cfg['grille']}/packages/{cfg['paquets'][0]}")
        runs = sorted(set(re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r.text)))
        if cfg.get("heures_runs"):   # runs retenus seulement à ces heures (UTC)
            runs = [x for x in runs if int(x[11:13]) in cfg["heures_runs"]]
      except Exception as e:
        print(f"{nom:12s} catalogue injoignable ({e.__class__.__name__})")
        continue
    if r.status_code != 200 or not runs:
        print(f"{nom:12s} catalogue : HTTP {r.status_code}, aucun run")
        continue
    deja = ""
    try:   # run publié sur le site (GitHub Pages) ; un ancien format sans tuiles compte comme non publié
        rp = requests.get(f"{site}/{nom}/run.json", timeout=30, headers={"Cache-Control": "no-cache"})
        if rp.status_code == 200 and "tuiles" in rp.json():
            deja = rp.json().get("run", "")
    except Exception:
        pass
    nouveau = runs[-1] > deja or forcer
    print(f"{nom:12s} dernier run {runs[-1]} · publié {deja or '—'} · {'À TRAITER' if nouveau else 'à jour'}")
    if nouveau:
        a_traiter.append({"modele": nom, "deja": "" if forcer else deja})

with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
    f.write(f"liste={json.dumps(a_traiter)}\n")
