"""À LANCER D'ABORD, sur la machine qui hébergera le bot (Render : onglet Shell) : python diagnostic.py
Dit si Vinted répond à ce serveur et si les annonces sont lisibles. Option : PROXY_URL=http://user:pass@host:port python diagnostic.py"""
import os, asyncio, aiohttp, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from bs4 import BeautifulSoup
URL = "https://www.vinted.fr/catalog?search_text=carhartt&order=newest_first"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
async def main():
    proxy = os.environ.get("PROXY_URL") or None
    async with aiohttp.ClientSession() as s:
        async with s.get(URL, proxy=proxy, headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "fr-FR,fr;q=0.9"}, timeout=aiohttp.ClientTimeout(total=25)) as r:
            html = await r.text()
            print("Statut HTTP :", r.status, "| taille :", len(html), "octets | proxy :", bool(proxy))
    cards = BeautifulSoup(html, "html.parser").select('[data-testid="grid-item"]')
    print("Annonces lues dans la page :", len(cards))
    if cards: print("Exemple :", re.sub(r"\s+", " ", cards[0].get_text(" "))[:150])
    if not cards: print("Début de la réponse :", re.sub(r"\s+", " ", html)[:300])
    print("\nInterprétation : 200 + annonces > ça marche | 403/429 ou page de vérification > serveur bloqué, il faut un proxy résidentiel | 200 sans annonces > Vinted a changé son affichage, envoie-moi la sortie.")
asyncio.run(main())
