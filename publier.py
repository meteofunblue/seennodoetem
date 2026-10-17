"""
Fusionne les sorties des conversions dans le contenu de la branche data, sans effacer les autres modèles,
puis régénère index.json (liste des modèles, runs, zones, paramètres) lu par l'appli.

  python publier.py --data data --artefacts artefacts
"""
import argparse
import glob
import json
import os
import shutil
from datetime import datetime, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True)
ap.add_argument("--artefacts", required=True)
args = ap.parse_args()
os.makedirs(args.data, exist_ok=True)

mis_a_jour = []
for etat in glob.glob(os.path.join(args.artefacts, "**", "run.json"), recursive=True):
    src = os.path.dirname(etat)
    modele = json.load(open(etat, encoding="utf-8")).get("modele") or os.path.basename(src)
    dst = os.path.join(args.data, modele)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    mis_a_jour.append(modele)
    print(f"Mis à jour : {modele}")

index = {"genere": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "modeles": {}}
for etat in sorted(glob.glob(os.path.join(args.data, "*", "run.json"))):
    e = json.load(open(etat, encoding="utf-8"))
    modele = os.path.basename(os.path.dirname(etat))
    if "zones" not in e:
        continue
    index["modeles"][modele] = {k: e.get(k) for k in ("nom", "run", "parametres", "vagues", "pas_sortie_h", "genere", "variables")}
    index["modeles"][modele]["zones"] = {z: {"nom": v["nom"], "pas_deg": v.get("pas_deg"), "points": v["points"]}
                                         for z, v in e["zones"].items()}
with open(os.path.join(args.data, "index.json"), "w", encoding="utf-8") as f:
    json.dump(index, f, ensure_ascii=False, indent=1)

open(os.path.join(args.data, ".nojekyll"), "w").close()
with open(os.path.join(args.data, "robots.txt"), "w") as f:
    f.write("User-agent: *\nDisallow: /\n")
with open(os.path.join(args.data, "index.html"), "w", encoding="utf-8") as f:
    f.write('<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">'
            '<title>Données</title><p>Données modèle (Météo-France, licence Etalab).</p>\n')

with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
    f.write(f"maj={' '.join(mis_a_jour)}\n")
print(f"index.json : {len(index['modeles'])} modèles publiés")
