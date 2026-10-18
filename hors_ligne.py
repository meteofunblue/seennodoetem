"""
Version hors ligne de l'appli : un seul fichier HTML autonome (hors-ligne.html) à copier sur un ordinateur ou une clé USB.

Tout ce que la page va normalement chercher sur Internet y est embarqué : bibliothèque de carte (Leaflet), lecture du
trait de côte (topojson), trait de côte mondial Natural Earth et trait de côte fin des côtes françaises. Sans réseau,
la page s'ouvre sur un fond uni et propose d'ouvrir une situation enregistrée (.meteo) ; avec du réseau, elle
fonctionne comme le site (données à jour).

  python hors_ligne.py --app app/index.html --cote app/cote_fr.json --sortie site/hors-ligne.html
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone


def telecharger(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read().decode("utf-8")


def sans_fin_script(txt):
    """Un « </script> » dans le code embarqué fermerait la balise : on le neutralise."""
    return re.sub(r"</(script)", r"<\\/\1", txt, flags=re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="app/index.html")
    ap.add_argument("--cote", default="app/cote_fr.json")
    ap.add_argument("--sortie", default="site/hors-ligne.html")
    args = ap.parse_args()
    html = open(args.app, encoding="utf-8").read()

    # données (trait de côte mondial et trait de côte fin), placées juste avant le script de l'appli
    m = re.search(r'"(https://cdn\.jsdelivr\.net/npm/world-atlas@[^"]+/land-10m\.json)"', html)
    land = telecharger(m.group(1))
    cote = open(args.cote, encoding="utf-8").read() if args.cote else "null"
    genere = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    donnees = ("<script>\nwindow.HORS_LIGNE=true;\nwindow.LAND10M=" + sans_fin_script(land) +
               ";\nwindow.COTE_FR=" + sans_fin_script(cote) + ";\n</script>\n")
    i = html.index("<script>")          # seul script en ligne de l'appli (les bibliothèques sont en src=)
    html = html[:i] + donnees + html[i:]
    # feuille de style Leaflet
    m = re.search(r'<link rel="stylesheet" href="(https://[^"]*leaflet[^"]*\.css)">', html)
    html = html.replace(m.group(0), "<style>\n" + telecharger(m.group(1)) + "\n</style>")
    # polices Google : retirées (polices du système à la place)
    html = re.sub(r'<link rel="preconnect" href="https://fonts\.googleapis\.com">\s*', "", html)
    html = re.sub(r'<link rel="stylesheet" href="https://fonts\.googleapis\.com[^"]*">\s*', "", html)
    # scripts externes (Leaflet, topojson) : contenu embarqué
    for m in re.finditer(r'<script src="(https://[^"]+)"></script>', html):
        html = html.replace(m.group(0), "<script>\n" + sans_fin_script(telecharger(m.group(1))) + "\n</script>")
    html = html.replace("<title>Modèles marine</title>", "<title>Modèles marine · hors ligne</title>", 1)
    html = html.replace("</footer>", f" Version hors ligne générée le {genere}.</footer>", 1)
    with open(args.sortie, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Version hors ligne : {args.sortie} ({len(html.encode()) / 1e6:.1f} Mo)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:   # pas bloquant pour la publication du site
        print(f"Version hors ligne non générée : {e}", file=sys.stderr)
