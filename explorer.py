"""
Reconnaissance de l'API Paquets AROME-IFS de Météo-France.

1. Obtient un jeton (identifiant d'application) ou utilise une clé d'API.
2. Trouve l'adresse du catalogue AROME-IFS et l'affiche (modèles, grilles, paquets).
3. Télécharge le premier groupe d'échéances des paquets SP1 et SP2 du dernier run
   pour permettre l'inventaire des messages GRIB (fait ensuite par le workflow).

Rien de secret n'est affiché : le jeton est masqué dans les journaux GitHub.
"""
import json
import os
import re
import sys
import time

import requests

API = "https://public-api.meteofrance.fr/previnum"
TOKEN_URL = "https://portail-api.meteofrance.fr/token"
OUT = "reconnaissance"
os.makedirs(OUT, exist_ok=True)


def masquer(valeur):
    """Demande à GitHub de remplacer cette valeur par *** dans les journaux."""
    if valeur and os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{valeur}")


def entetes():
    """Ordre de préférence : jeton déjà généré (test), clé d'API, identifiants d'application."""
    jeton_direct = os.environ.get("MF_TOKEN", "").strip()
    cle = os.environ.get("MF_API_KEY", "").strip()
    app = os.environ.get("MF_APP_ID", "").strip()
    cid = os.environ.get("MF_CLIENT_ID", "").strip()
    csec = os.environ.get("MF_CLIENT_SECRET", "").strip()
    if not app and cid and csec:
        import base64
        masquer(cid); masquer(csec)
        app = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    if jeton_direct:
        masquer(jeton_direct)
        print("Authentification : jeton OAuth2 fourni (valable environ 1 h, pour test uniquement)")
        return {"Authorization": f"Bearer {jeton_direct}"}
    if cle:
        masquer(cle)
        print("Authentification : clé d'API")
        return {"apikey": cle}
    if app:
        masquer(app)
        r = requests.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {app}"}, timeout=60)
        if r.status_code != 200:
            sys.exit(f"Échec d'obtention du jeton : HTTP {r.status_code}. "
                     "Vérifiez le secret MF_APP_ID (Application ID du portail).")
        jeton = r.json()["access_token"]
        masquer(jeton)
        print("Authentification : jeton obtenu à partir de l'identifiant d'application")
        return {"Authorization": f"Bearer {jeton}"}
    sys.exit("Aucun secret trouvé : renseignez MF_TOKEN (test), MF_API_KEY, MF_APP_ID "
             "ou MF_CLIENT_ID + MF_CLIENT_SECRET dans les secrets du dépôt.")


H = entetes()


def get(url, accept="text/json", **kw):
    h = dict(H, accept=accept)
    for essai in range(3):
        r = requests.get(url, headers=h, timeout=120, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * (essai + 1))
            continue
        return r
    return r


def montrer(titre, r):
    print(f"\n===== {titre} =====")
    print(f"HTTP {r.status_code}  {r.url}")
    txt = r.text
    try:
        txt = json.dumps(r.json(), indent=1, ensure_ascii=False)
    except Exception:
        pass
    print(txt[:6000])
    return txt


# ---------- 1. Trouver le catalogue AROME-IFS ----------
BASES = ["DPPaquetAROMEIFS", "DPPaquetAROME-IFS", "DPPaquetAromeIFS", "DPPaquetAROME_IFS"]
MODELES = ["AROME-IFS", "AROMEIFS", "AROME_IFS", "AromeIFS"]
base = modele = None
for b in BASES:
    r = get(f"{API}/{b}/v1/models")
    print(f"Essai {b}/v1/models -> HTTP {r.status_code}")
    if r.status_code == 200:
        txt = montrer(f"Liste des modèles ({b})", r)
        base = b
        noms = [m for m in MODELES if m in txt]
        trouves = re.findall(r'"(AROME[^"]*)"', txt)
        modele = (noms or trouves or [None])[0]
        break
    if r.status_code in (401, 403):
        print("  -> accès refusé : l'application est-elle abonnée à l'API Paquet AROME-IFS sur le portail ?")
if not base:
    for b in BASES:
        for m in MODELES:
            r = get(f"{API}/{b}/v1/models/{m}/grids")
            print(f"Essai {b}/v1/models/{m}/grids -> HTTP {r.status_code}")
            if r.status_code == 200:
                base, modele = b, m
                break
        if base:
            break
if not base or not modele:
    sys.exit("Catalogue AROME-IFS introuvable. Copiez ce journal à Claude.")
print(f"\n>>> Catalogue trouvé : {API}/{base}/v1  modèle = {modele}")

# ---------- 2. Grilles et paquets ----------
R = f"{API}/{base}/v1/models/{modele}"
txt_grilles = montrer("Grilles", get(f"{R}/grids"))
grilles = re.findall(r"\b0\.\d+\b", txt_grilles) or ["0.025"]
grille = "0.025" if "0.025" in grilles else grilles[0]
print(f"\n>>> Grille retenue : {grille}  (disponibles : {sorted(set(grilles))})")
txt_paquets = montrer(f"Paquets de la grille {grille}", get(f"{R}/grids/{grille}/packages"))

catalogue = {"base": f"{API}/{base}/v1", "modele": modele, "grille": grille, "paquets": {}}
for paquet in ["SP1", "SP2"]:
    txt = montrer(f"Détail du paquet {paquet}", get(f"{R}/grids/{grille}/packages/{paquet}"))
    runs = sorted(set(re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", txt)))
    groupes = list(dict.fromkeys(re.findall(r"\b\d{2,3}H\d{2,3}H\b", txt)))
    produits = list(dict.fromkeys(re.findall(r"product[A-Za-z0-9_]+", txt)))
    catalogue["paquets"][paquet] = {"runs": runs, "groupes": groupes, "produits": produits}
    print(f">>> {paquet} : runs {runs[-4:]}  groupes {groupes}  produits {produits}")

with open(f"{OUT}/catalogue.json", "w", encoding="utf-8") as f:
    json.dump(catalogue, f, indent=1, ensure_ascii=False)

# ---------- 3. Télécharger un groupe d'échéances de SP1 et SP2 ----------
for paquet, info in catalogue["paquets"].items():
    run = info["runs"][-1] if info["runs"] else None
    groupe = info["groupes"][0] if info["groupes"] else "00H06H"
    produit = info["produits"][0] if info["produits"] else "productARO"
    if not run:
        print(f"Pas de run trouvé dans le catalogue pour {paquet}, téléchargement ignoré.")
        continue
    url = f"{R}/grids/{grille}/packages/{paquet}/{produit}"
    params = {"referencetime": run, "time": groupe, "format": "grib2"}
    print(f"\nTéléchargement {paquet} run {run} groupe {groupe} ...")
    t0 = time.time()
    with requests.get(url, headers=dict(H, accept="*/*"), params=params, stream=True, timeout=600) as r:
        print(f"HTTP {r.status_code}  {r.url}")
        if r.status_code != 200:
            print(r.text[:2000])
            continue
        chemin = f"{OUT}/{paquet}_{groupe}.grib2"
        taille = 0
        with open(chemin, "wb") as f:
            for bloc in r.iter_content(1 << 20):
                f.write(bloc)
                taille += len(bloc)
    print(f"  {taille/1e6:.1f} Mo en {time.time()-t0:.0f} s -> {chemin}")
    time.sleep(2)  # téléchargements séquentiels, comme le demande Météo-France

print("\nReconnaissance terminée.")
