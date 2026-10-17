"""
Accès à l'API Paquets AROME-IFS de Météo-France (portail-api.meteofrance.fr).

Secrets acceptés (variables d'environnement), par ordre de préférence :
  MF_CLIENT_ID + MF_CLIENT_SECRET  identifiants de l'application (durable, conseillé)
  MF_APP_ID                         les mêmes, déjà encodés en base64 (durable)
  MF_API_KEY                        clé d'API (durée choisie sur le portail)
  MF_TOKEN                          jeton OAuth2 généré à la main (1 h, pour test)
Aucune valeur secrète n'est jamais affichée : elles sont masquées dans les journaux GitHub.
"""
import base64
import os
import re
import sys
import time

import requests

API = "https://public-api.meteofrance.fr/previnum/DPPaquetAROMEIFS/v1"
MODELE = "AROMEIFS"
PRODUIT = "productAROIFS"
TOKEN_URL = "https://portail-api.meteofrance.fr/token"


def masquer(valeur):
    if valeur and os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{valeur}", flush=True)


def _propre(v):
    v = (v or "").strip().strip('"').strip("'")
    if v.lower().startswith("bearer "):
        v = v[7:]
    return "".join(v.split())


class Session:
    def __init__(self):
        self.h = {}
        self._jeton_expire = 0
        self._app = None
        cid, csec = _propre(os.environ.get("MF_CLIENT_ID")), _propre(os.environ.get("MF_CLIENT_SECRET"))
        app = _propre(os.environ.get("MF_APP_ID"))
        cle = _propre(os.environ.get("MF_API_KEY"))
        jeton = _propre(os.environ.get("MF_TOKEN"))
        for v in (cid, csec, app, cle, jeton):
            masquer(v)
        if cid and csec:
            app = base64.b64encode(f"{cid}:{csec}".encode()).decode()
            masquer(app)
        if app:
            self._app = app
            self._renouveler()
            print("Authentification : identifiants d'application (jeton renouvelé automatiquement)")
        elif cle:
            self.h = {"apikey": cle}
            print("Authentification : clé d'API")
        elif jeton:
            self.h = {"Authorization": f"Bearer {jeton}"}
            print("Authentification : jeton OAuth2 manuel (valable environ 1 h)")
        else:
            sys.exit("Aucun secret Météo-France trouvé (MF_CLIENT_ID/MF_CLIENT_SECRET, MF_APP_ID, MF_API_KEY ou MF_TOKEN).")

    def _renouveler(self):
        r = requests.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {self._app}"}, timeout=60)
        if r.status_code != 200:
            sys.exit(f"Échec d'obtention du jeton (HTTP {r.status_code}) : vérifiez les identifiants de l'application.")
        j = r.json()
        masquer(j["access_token"])
        self.h = {"Authorization": f"Bearer {j['access_token']}"}
        self._jeton_expire = time.time() + int(j.get("expires_in", 3600)) - 120

    def _verifier(self):
        if self._app and time.time() > self._jeton_expire:
            self._renouveler()

    def get(self, url, accept="text/json", stream=False, params=None, timeout=120):
        """Requête espacée ; nouvelles tentatives si le serveur limite, refuse ou échoue."""
        derniere = None
        for essai in range(4):
            self._verifier()
            time.sleep(2)
            try:
                r = requests.get(url, headers=dict(self.h, accept=accept), params=params,
                                 stream=stream, timeout=timeout)
            except requests.exceptions.RequestException as e:
                derniere = e
                attente = 20 * (essai + 1)
                print(f"  erreur réseau ({e.__class__.__name__}), nouvel essai dans {attente} s", flush=True)
                time.sleep(attente)
                continue
            if r.status_code == 401 and self._app:
                self._renouveler()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                attente = 20 * (essai + 1)
                print(f"  HTTP {r.status_code}, nouvel essai dans {attente} s", flush=True)
                r.close()
                time.sleep(attente)
                continue
            return r
        if derniere:
            raise derniere
        return r

    # ---------- catalogue ----------
    def runs_disponibles(self, grille="0.025", paquet="SP1"):
        r = self.get(f"{API}/models/{MODELE}/grids/{grille}/packages/{paquet}")
        if r.status_code in (401, 403):
            sys.exit(f"Accès refusé par Météo-France (HTTP {r.status_code}) : secret expiré ou invalide, "
                     "ou application non abonnée à l'API Paquet AROME-IFS.")
        r.raise_for_status()
        return sorted(set(re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r.text)))

    # ---------- téléchargement ----------
    def telecharger(self, grille, paquet, run, groupe, chemin):
        """Télécharge un paquet pour un groupe d'échéances. Renvoie la taille en octets, ou 0 si indisponible."""
        url = f"{API}/models/{MODELE}/grids/{grille}/packages/{paquet}/{PRODUIT}"
        params = {"referencetime": run, "time": groupe, "format": "grib2"}
        r = self.get(url, accept="*/*", stream=True, params=params, timeout=900)
        with r:
            if r.status_code != 200:
                print(f"  {paquet} {groupe} : HTTP {r.status_code} {r.text[:200]!r}", flush=True)
                return 0
            taille = 0
            with open(chemin, "wb") as f:
                for bloc in r.iter_content(1 << 20):
                    f.write(bloc)
                    taille += len(bloc)
        return taille
