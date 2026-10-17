"""
Reconnaissance multi-modèles des API Paquets de Météo-France.

Pour chaque modèle : grilles, paquets, runs disponibles, échéances proposées (catalogue),
puis téléchargement d'UN exemple par grille (premier paquet, première échéance) pour l'inventaire.
Résultats dans reconnaissance/ : catalogue.json + exemples (inventoriés ensuite par le workflow).
"""
import json
import os
import re
import sys
import time

from mf_api import Session

API = "https://public-api.meteofrance.fr/previnum"
# nom court : (API, modèle, produit, format, télécharger un exemple ?)
MODELES = {
    "aromeifs":   ("DPPaquetAROMEIFS",   "AROMEIFS",       "productAROIFS", "grib2", True),
    "arome":      ("DPPaquetAROME",      "AROME",          "productARO",    "grib2", True),
    "arpege":     ("DPPaquetARPEGE",     "ARPEGE",         "productARP",    "grib2", True),
    "mfwam":      ("DPPaquetWAVESMODELS", "MFWAM",         "productMFWAM",  "grib2", True),
    "ww3_atl":    ("DPPaquetWAVESMODELS", "WW3-WARP",      "productWWARP",  "nc",    True),
    "ww3_med":    ("DPPaquetWAVESMODELS", "WW3-MARP",      "productWMARP",  "nc",    True),
    "ww3_med_aro":("DPPaquetWAVESMODELS", "WW3-MARO",      "productWMARO",  "nc",    True),
    "surcote_atl":("DPPaquetWAVESMODELS", "HYCOM2D-WARP",  "productHWARP",  "grib2", True),
    "surcote_atl_aro": ("DPPaquetWAVESMODELS", "HYCOM2D-WARO", "productHWARO", "grib2", False),
    "surcote_med":("DPPaquetWAVESMODELS", "HYCOM2D-MARP",  "productHMARP",  "grib2", False),
    "surcote_med_aro": ("DPPaquetWAVESMODELS", "HYCOM2D-MARO", "productHMARO", "grib2", False),
    "om_antilles":("DPPaquetAROME-OM",   "AROME-OM-ANTIL", "productOMAN",   "grib2", True),
    "om_guyane":  ("DPPaquetAROME-OM",   "AROME-OM-GUYANE","productOMGU",   "grib2", False),
    "om_indien":  ("DPPaquetAROME-OM",   "AROME-OM-INDIEN","productOMOI",   "grib2", False),
    "om_polynesie":("DPPaquetAROME-OM",  "AROME-OM-POLYN", "productOMPF",   "grib2", False),
    "om_caledonie":("DPPaquetAROME-OM",  "AROME-OM-NCALED","productOMNC",   "grib2", False),
}
# Échéance d'essai si le catalogue n'en annonce pas (formats tirés des fichiers Swagger)
ECH_REPLI = {"aromeifs": ["00H06H"], "arome": ["00H06H"], "arpege": ["000H012H", "000H024H", "000H"],
             "mfwam": ["000H", "001H", "003H"], "ww3_atl": ["000H012H", "000H024H"], "ww3_med": ["000H012H", "000H024H"],
             "ww3_med_aro": ["000H012H", "000H024H"], "surcote_atl": ["000H", "001H"]}
TAILLE_MAX = 400 * 1024 * 1024   # on n'enregistre pas plus de 400 Mo par exemple
OUT = "reconnaissance"
os.makedirs(OUT, exist_ok=True)

choix = [c.strip() for c in os.environ.get("MODELES", "").split(",") if c.strip()] or list(MODELES)
ses = Session()
resultat = {}


def texte(r):
    try:
        return json.dumps(r.json(), ensure_ascii=False)
    except Exception:
        return r.text


def grilles_de(txt):
    """Les grilles sont des nombres (0.025, 0.1, 0.5...) ; on les cherche dans la réponse."""
    vus = re.findall(r'"(?:grid|grille|name|id)"\s*:\s*"?(\d+(?:\.\d+)?)"?', txt) or re.findall(r"\b0\.\d+\b", txt)
    return list(dict.fromkeys(vus))


def paquets_de(txt):
    vus = re.findall(r'"(?:package|paquet|name|id)"\s*:\s*"([A-Za-z0-9_\-]+)"', txt)
    vus = [v for v in vus if not re.fullmatch(r"\d+(\.\d+)?", v)]
    return list(dict.fromkeys(vus)) or re.findall(r"\b[A-Z]{1,4}\d\b", txt)


for nom in choix:
    if nom not in MODELES:
        print(f"Modèle inconnu ignoré : {nom}")
        continue
    api, mod, produit, fmt, telecharger = MODELES[nom]
    base = f"{API}/{api}/v1/models/{mod}"
    print(f"\n################ {nom} ({mod}) ################", flush=True)
    info = {"api": api, "modele": mod, "produit": produit, "format": fmt, "grilles": {}}
    r = ses.get(base)
    print(f"Description : HTTP {r.status_code}  {texte(r)[:800]}")
    if r.status_code != 200:
        info["erreur"] = f"HTTP {r.status_code}"
        resultat[nom] = info
        continue
    r = ses.get(f"{base}/grids")
    tg = texte(r)
    print(f"Grilles : HTTP {r.status_code}  {tg[:1500]}")
    for g in grilles_de(tg):
        rp = ses.get(f"{base}/grids/{g}/packages")
        tp = texte(rp)
        print(f"  grille {g} · paquets : {tp[:1500]}")
        paquets = paquets_de(tp)
        ig = info["grilles"][g] = {"paquets": {}}
        for p in paquets:
            rd = ses.get(f"{base}/grids/{g}/packages/{p}")
            td = texte(rd)
            runs = sorted(set(re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", td)))
            ip = ig["paquets"][p] = {"runs": runs[-6:], "nb_runs": len(runs)}
            if runs:
                # détail du dernier run : liste des échéances / groupes d'échéances proposés
                rr = ses.get(f"{base}/grids/{g}/packages/{p}", params={"referencetime": runs[-1]})
                tr = texte(rr)
                echs = list(dict.fromkeys(re.findall(r"\b\d{2,3}H(?:\d{2,3}H)?\b", tr)))
                ip["echeances"] = echs
                ip["detail_run"] = tr[:3000]
            print(f"    {p} : {len(runs)} runs (dernier {runs[-1] if runs else '—'}) · "
                  f"échéances {ip.get('echeances', [])[:12]}{' …' if len(ip.get('echeances', [])) > 12 else ''}", flush=True)
        # un exemple par grille : premier paquet, première échéance
        if telecharger and ig["paquets"]:
            # premier paquet qui a effectivement des runs (WW3 annonce un paquet « WW3 » vide)
            p, ip = next(((k, v) for k, v in ig["paquets"].items() if v.get("runs")),
                         next(iter(ig["paquets"].items())))
            essais = (ip.get("echeances") or [])[:1] + ECH_REPLI.get(nom, ["000H", "00H"])
            if ip.get("runs"):
                run = ip["runs"][-1]
                url = f"{base}/grids/{g}/packages/{p}/{produit}"
                for ech in dict.fromkeys(essais):
                    t0 = time.time()
                    r = ses.get(url, accept="*/*", stream=True,
                                params={"referencetime": run, "time": ech, "format": fmt}, timeout=900)
                    if r.status_code == 200:
                        break
                    print(f"    essai échéance {ech} : HTTP {r.status_code} {r.text[:200]!r}")
                    r.close()
                chemin = f"{OUT}/{nom}_{g}_{p}_{ech}.{ 'nc' if fmt == 'nc' else 'grib2'}"
                with r:
                    if r.status_code == 200:
                        n = 0
                        with open(chemin, "wb") as f:
                            for bloc in r.iter_content(1 << 20):
                                f.write(bloc)
                                n += len(bloc)
                                if n > (TAILLE_MAX * 5 if fmt == "nc" else TAILLE_MAX):  # un NetCDF tronqué est illisible
                                    print("    (exemple tronqué à 400 Mo)")
                                    break
                        ig["exemple"] = {"fichier": os.path.basename(chemin), "run": run, "echeance": ech,
                                         "taille_mo": round(n / 1e6, 1), "duree_s": round(time.time() - t0)}
                        print(f"    exemple {p} {ech} : {n/1e6:.1f} Mo en {time.time()-t0:.0f} s", flush=True)
                    else:
                        ig["exemple"] = {"erreur": f"HTTP {r.status_code}", "reponse": r.text[:300]}
                        print(f"    exemple {p} {ech} : HTTP {r.status_code} {r.text[:200]!r}")
    resultat[nom] = info

with open(f"{OUT}/catalogue.json", "w", encoding="utf-8") as f:
    json.dump(resultat, f, indent=1, ensure_ascii=False)
print("\nReconnaissance terminée.")
