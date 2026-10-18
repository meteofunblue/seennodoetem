"""
Assemble le site GitHub Pages : modèles déjà publiés (repris de la Release) + nouvelles conversions,
appli à la racine, index.json (modèles, runs disponibles, domaines, zones prédéfinies) lu par l'appli.

Organisation : <modele>/<AAAAMMJJHH>/{meta.json, run.json, t/…}  (un dossier par run, les N derniers gardés)
               <modele>/meta.json et run.json = copie du run le plus récent, avec la liste des runs gardés.

  python publier.py --site site --artefacts artefacts --app app/index.html --zones zones.json
"""
import argparse
import glob
import json
import os
import re
import shutil
from datetime import datetime, timezone

GARDER_DEFAUT = 4          # run courant + 3 précédents
LIMITE_MO = 900            # GitHub Pages : 1 Go maximum par site ; au-delà, on retire les runs les plus anciens

ap = argparse.ArgumentParser()
ap.add_argument("--site", required=True)
ap.add_argument("--artefacts", required=True)
ap.add_argument("--app", default="app/index.html")
ap.add_argument("--zones", default="zones.json")
ap.add_argument("--modeles", default="modeles.json")
args = ap.parse_args()
os.makedirs(args.site, exist_ok=True)
cfg = {}
if os.path.exists(args.modeles):
    cfg = {k: v for k, v in json.load(open(args.modeles, encoding="utf-8")).items() if not k.startswith("_")}


def rid(run):
    return re.sub(r"\D", "", run)[:10]          # 2026-10-18T06:00:00Z -> 2026101806


def lire(p):
    return json.load(open(p, encoding="utf-8"))


def taille(d):
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(d) for f in fs)


def runs_de(dm):
    return sorted([d for d in os.listdir(dm) if re.fullmatch(r"\d{10}", d) and os.path.exists(os.path.join(dm, d, "meta.json"))],
                  reverse=True)


# 1. ancienne organisation (un seul run directement dans <modele>/) : rangée dans <modele>/<run>/
for meta in glob.glob(os.path.join(args.site, "*", "meta.json")):
    dm = os.path.dirname(meta)
    if os.path.isdir(os.path.join(dm, "t")):
        r = rid(lire(meta)["run"])
        dst = os.path.join(dm, r)
        if not os.path.exists(dst):
            os.makedirs(dst)
            shutil.move(os.path.join(dm, "t"), os.path.join(dst, "t"))
            shutil.copy(meta, os.path.join(dst, "meta.json"))
            if os.path.exists(os.path.join(dm, "run.json")):
                shutil.copy(os.path.join(dm, "run.json"), os.path.join(dst, "run.json"))
            print(f"Rangé : {os.path.basename(dm)}/{r}")
        else:
            shutil.rmtree(os.path.join(dm, "t"))

# 2. formats plus anciens (zones fixes, sans meta.json ni runs) : retirés
for dm in sorted(glob.glob(os.path.join(args.site, "*", ""))):
    dm = dm.rstrip("/")
    if os.path.exists(os.path.join(dm, "run.json")) and not os.path.exists(os.path.join(dm, "meta.json")) and not runs_de(dm):
        print(f"Ancien format retiré : {os.path.basename(dm)} (relancer sa conversion)")
        shutil.rmtree(dm)

# 3. nouvelles conversions : ajoutées comme un run de plus
mis_a_jour = []
for meta in glob.glob(os.path.join(args.artefacts, "**", "meta.json"), recursive=True):
    src = os.path.dirname(meta)
    m = lire(meta)
    dst = os.path.join(args.site, m["modele"], rid(m["run"]))
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    mis_a_jour.append(m["modele"])
    print(f"Mis à jour : {m['modele']} run {m['run']}")

# 4. runs gardés par modèle, puis limite de taille du site
modeles = sorted(d for d in os.listdir(args.site) if os.path.isdir(os.path.join(args.site, d)) and runs_de(os.path.join(args.site, d)))
for mo in modeles:
    dm = os.path.join(args.site, mo)
    garder = int(cfg.get(mo, {}).get("runs_gardes", GARDER_DEFAUT))
    for r in runs_de(dm)[garder:]:
        shutil.rmtree(os.path.join(dm, r))
        print(f"Run retiré (plus de {garder} gardés) : {mo}/{r}")
while taille(args.site) / 1e6 > LIMITE_MO:
    cand = [(taille(os.path.join(args.site, mo, runs_de(os.path.join(args.site, mo))[-1])), mo)
            for mo in modeles if len(runs_de(os.path.join(args.site, mo))) > 1]
    if not cand:
        break
    _, mo = max(cand)
    r = runs_de(os.path.join(args.site, mo))[-1]
    shutil.rmtree(os.path.join(args.site, mo, r))
    print(f"Run retiré (site au-delà de {LIMITE_MO} Mo) : {mo}/{r}")

# 5. meta.json / run.json du dernier run + index.json
index = {"genere": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "modeles": {}, "zones": {}}
for mo in modeles:
    dm = os.path.join(args.site, mo)
    rs = runs_de(dm)
    liste = []
    for r in rs:
        m = lire(os.path.join(dm, r, "meta.json"))
        liste.append({"run": m["run"], "chemin": r, "debut": m["temps"][0], "fin": m["temps"][-1], "echeances": len(m["temps"])})
    dernier = lire(os.path.join(dm, rs[0], "meta.json"))
    dernier.update(chemin=rs[0] + "/", runs=liste)
    with open(os.path.join(dm, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(dernier, f, ensure_ascii=False, separators=(",", ":"))
    etat_src = os.path.join(dm, rs[0], "run.json")
    etat = lire(etat_src) if os.path.exists(etat_src) else {"modele": mo, "run": dernier["run"], "tuiles": len(dernier["tuiles"])}
    etat["runs"] = [x["run"] for x in liste]
    with open(os.path.join(dm, "run.json"), "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1)
    index["modeles"][mo] = {
        "nom": dernier["nom"], "type": dernier["type"], "run": dernier["run"], "genere": dernier["genere"], "res": dernier["res"],
        "res_fin": dernier.get("res_fin"),
        "domaine": dernier["domaine"], "vagues": dernier.get("vagues", []), "force_par": dernier.get("force_par"),
        "pas_sortie_h": dernier.get("pas_sortie_h"), "debut": dernier["temps"][0], "fin": dernier["temps"][-1],
        "echeances": len(dernier["temps"]), "tuiles": len(dernier["tuiles"]), "variables": [v["nom"] for v in dernier["variables"]],
        "runs": liste,
    }
if os.path.exists(args.zones):
    index["zones"] = {k: v for k, v in lire(args.zones).items() if not k.startswith("_")}
with open(os.path.join(args.site, "index.json"), "w", encoding="utf-8") as f:
    json.dump(index, f, ensure_ascii=False, indent=1)

# 6. appli à la racine, pas d'indexation par les moteurs de recherche
if os.path.exists(args.app):
    shutil.copy(args.app, os.path.join(args.site, "index.html"))
    # autres fichiers de l'appli (trait de côte fin…)
    for f in glob.glob(os.path.join(os.path.dirname(args.app), "*")):
        if os.path.isfile(f) and os.path.basename(f) != os.path.basename(args.app):
            shutil.copy(f, os.path.join(args.site, os.path.basename(f)))
with open(os.path.join(args.site, "robots.txt"), "w") as f:
    f.write("User-agent: *\nDisallow: /\n")

with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
    f.write(f"maj={' '.join(sorted(set(mis_a_jour)))}\n")
for mo, v in index["modeles"].items():
    print(f"  {mo:12s} runs gardés : {', '.join(x['chemin'] for x in v['runs'])}")
print(f"index.json : {len(index['modeles'])} modèles publiés · site {taille(args.site) / 1e6:.0f} Mo")
