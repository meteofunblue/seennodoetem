"""
Assemble le site GitHub Pages : modèles déjà publiés (repris de la Release) + nouvelles conversions,
appli à la racine, index.json (modèles, runs, domaines, zones prédéfinies) lu par l'appli.

  python publier.py --site site --artefacts artefacts --app app/index.html --zones zones.json
"""
import argparse
import glob
import json
import os
import shutil
from datetime import datetime, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--site", required=True)
ap.add_argument("--artefacts", required=True)
ap.add_argument("--app", default="app/index.html")
ap.add_argument("--zones", default="zones.json")
args = ap.parse_args()
os.makedirs(args.site, exist_ok=True)

# 1. nouvelles conversions : chaque dossier qui contient meta.json remplace le modèle correspondant
mis_a_jour = []
for meta in glob.glob(os.path.join(args.artefacts, "**", "meta.json"), recursive=True):
    src = os.path.dirname(meta)
    modele = json.load(open(meta, encoding="utf-8")).get("modele") or os.path.basename(src)
    dst = os.path.join(args.site, modele)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    mis_a_jour.append(modele)
    print(f"Mis à jour : {modele}")

# 2. anciens formats (zones fixes, sans meta.json) : retirés du site
for d in sorted(glob.glob(os.path.join(args.site, "*", ""))):
    d = d.rstrip("/")
    if os.path.exists(os.path.join(d, "run.json")) and not os.path.exists(os.path.join(d, "meta.json")):
        print(f"Ancien format retiré : {os.path.basename(d)} (relancer sa conversion)")
        shutil.rmtree(d)

# 3. index.json
index = {"genere": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "modeles": {}, "zones": {}}
for meta in sorted(glob.glob(os.path.join(args.site, "*", "meta.json"))):
    m = json.load(open(meta, encoding="utf-8"))
    index["modeles"][m["modele"]] = {
        "nom": m["nom"], "type": m["type"], "run": m["run"], "genere": m["genere"], "res": m["res"],
        "domaine": m["domaine"], "vagues": m.get("vagues", []), "force_par": m.get("force_par"),
        "pas_sortie_h": m.get("pas_sortie_h"), "debut": m["temps"][0], "fin": m["temps"][-1],
        "echeances": len(m["temps"]), "tuiles": len(m["tuiles"]), "variables": [v["nom"] for v in m["variables"]],
    }
if os.path.exists(args.zones):
    index["zones"] = {k: v for k, v in json.load(open(args.zones, encoding="utf-8")).items() if not k.startswith("_")}
with open(os.path.join(args.site, "index.json"), "w", encoding="utf-8") as f:
    json.dump(index, f, ensure_ascii=False, indent=1)

# 4. appli à la racine, pas d'indexation par les moteurs de recherche
if os.path.exists(args.app):
    shutil.copy(args.app, os.path.join(args.site, "index.html"))
with open(os.path.join(args.site, "robots.txt"), "w") as f:
    f.write("User-agent: *\nDisallow: /\n")

with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
    f.write(f"maj={' '.join(mis_a_jour)}\n")
print(f"index.json : {len(index['modeles'])} modèles publiés ({', '.join(index['modeles'])})")
