#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
momox_watch.py
--------------
Surveille une liste d'ISBN sur momox-shop.fr et envoie une alerte Telegram
dès qu'un titre est DISPONIBLE (en stock) à un prix inférieur au seuil (7 €).

Fonctionnement (validé sur les pages réelles de momox-shop.fr) :
- On interroge la recherche du site avec l'ISBN.
- momox identifie chaque produit par un code « M0 + ISBN sans le 978 »
  (ex. ISBN 9782070335046  ->  code M02070335046).
- La page contient, pour CE produit, ses variantes par état, chacune avec
  son "stock" et son "price". momox affiche un prix même quand le stock est 0,
  donc on ne retient que les variantes réellement en stock (stock > 0), et on
  prend le prix le plus bas parmi celles-ci.

Conçu pour GitHub Actions (rotation par lots, état dans state.json).

Usage local :
    python momox_watch.py
    python momox_watch.py --debug   # enregistre le HTML récupéré
"""

import json
import os
import re
import sys
import time
import random
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# Configuration (modifiable via variables d'environnement / secrets GitHub)
# ----------------------------------------------------------------------------
THRESHOLD_EUR = float(os.environ.get("PRICE_THRESHOLD", "7.0"))
ISBN_FILE = Path(os.environ.get("ISBN_FILE", "isbns.txt"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()

# Chat ID inscrit directement ici (peu sensible). Modifiable via l'environnement.
# Doit être TON identifiant (celui de @getidsbot / "Your user ID"). Le TOKEN, lui,
# reste un secret GitHub.
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8881038521").strip()

DEBUG = "--debug" in sys.argv

# Délai (en secondes) entre deux requêtes vers Momox, pour rester discret.
# Si tu vois des blocages, augmente ces valeurs.
MIN_DELAY, MAX_DELAY = 1.0, 2.0

# Rotation par lots : on ne traite qu'un lot de BATCH_SIZE ISBN par exécution,
# puis on avance dans la liste (curseur mémorisé dans state.json).
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "25"))

# Adresse de recherche de momox-shop.fr (l'ISBN est mis dans searchparam).
MOMOX_SEARCH_URL = (
    "https://www.momox-shop.fr/produits-C0/?fcIsSearch=1&searchparam={isbn}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ----------------------------------------------------------------------------
# Correspondance ISBN -> code produit momox
# ----------------------------------------------------------------------------
def isbn_to_mid(isbn):
    """9782070335046 -> 'M02070335046' (M0 + ISBN sans le préfixe 978/979)."""
    isbn = re.sub(r"[^0-9X]", "", isbn.upper())
    if len(isbn) == 13 and isbn[:3] in ("978", "979"):
        return "M0" + isbn[3:]
    return None


# ----------------------------------------------------------------------------
# Récupération de l'offre disponible la moins chère pour un ISBN
# ----------------------------------------------------------------------------
def fetch_offer(session, isbn):
    """
    Renvoie (prix, url) où prix = prix le plus bas EN STOCK pour l'ISBN,
    ou (None, url) si rien n'est disponible / produit introuvable.
    """
    mid = isbn_to_mid(isbn)
    if not mid:
        print(f"    [!] Format d'ISBN inattendu, ignoré : {isbn}")
        return None, None

    url = MOMOX_SEARCH_URL.format(isbn=isbn)
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [!] Erreur requête pour {isbn} : {e}")
        return None, url

    if DEBUG:
        Path(f"debug_{isbn}.html").write_text(resp.text, encoding="utf-8")

    # Les données sont du JSON « échappé » dans la page ; on retire les
    # antislashs pour pouvoir lire proprement les variantes du bon produit.
    clean = resp.text.replace("\\", "")
    pattern = (
        r'"id":"' + re.escape(mid) +
        r'[^"]*","type":"([^"]*)","stock":\s*(\d+),\s*"price":\s*([0-9.]+)'
    )
    variants = re.findall(pattern, clean)

    # Lien direct vers la fiche produit (pour l'alerte), sinon l'URL de recherche.
    m = re.search(r'href="(/[^"]*' + re.escape(mid) + r'\.html)"', resp.text)
    product_url = ("https://www.momox-shop.fr" + m.group(1)) if m else url

    if not variants:
        print(f"    introuvable dans la recherche (probablement jamais listé)")
        return None, product_url

    # Variantes réellement en stock
    in_stock = [(float(p), typ) for typ, s, p in variants if int(s) > 0]
    nb_total = len(set(variants))
    if in_stock:
        best_price = min(p for p, _ in in_stock)
        print(f"    EN STOCK : {best_price:.2f} € "
              f"({len(set(in_stock))} variante(s) dispo sur {nb_total})")
        return best_price, product_url
    else:
        print(f"    pas en stock ({nb_total} variante(s) listée(s), aucune dispo)")
        return None, product_url


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID manquants : "
              "alerte non envoyée (affichage console).")
        print("  >>>", text)
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(api, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  [!] Échec envoi Telegram : {e}")


# ----------------------------------------------------------------------------
# État (curseur de rotation + suivi par ISBN)
# ----------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "items" in data and isinstance(data["items"], dict):
                    data.setdefault("cursor", 0)
                    return data
                items = {k: v for k, v in data.items() if k != "cursor"}
                return {"cursor": data.get("cursor", 0), "items": items}
        except Exception:
            pass
    return {"cursor": 0, "items": {}}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_isbns():
    if not ISBN_FILE.exists():
        print(f"[!] Fichier {ISBN_FILE} introuvable.")
        return []
    isbns = []
    for line in ISBN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip().replace("-", "").replace(" ", "")
        if line and not line.startswith("#"):
            isbns.append(line)
    return isbns


# ----------------------------------------------------------------------------
# Programme principal (rotation par lots)
# ----------------------------------------------------------------------------
def main():
    all_isbns = load_isbns()
    if not all_isbns:
        print("Aucun ISBN à surveiller. Ajoute-les dans isbns.txt.")
        return

    state = load_state()
    items = state["items"]
    total = len(all_isbns)

    cursor = state.get("cursor", 0)
    if cursor >= total:
        cursor = 0
    end = cursor + BATCH_SIZE
    batch = all_isbns[cursor:end]
    if end > total:
        batch += all_isbns[: end - total]

    session = requests.Session()
    print(f"Liste : {total} ISBN — seuil = {THRESHOLD_EUR:.2f} €")
    print(f"Lot courant : indices {cursor}..{cursor + len(batch) - 1} "
          f"({len(batch)} ISBN)")

    for i, isbn in enumerate(batch, 1):
        print(f"[{i}/{len(batch)}] ISBN {isbn} ...")
        price, url = fetch_offer(session, isbn)
        prev_alerted = items.get(isbn, {}).get("alerted_price")

        if price is not None and price < THRESHOLD_EUR:
            if prev_alerted is None or price < prev_alerted:
                msg = (
                    f"📚 <b>Dispo sur Momox sous {THRESHOLD_EUR:.0f} €</b>\n"
                    f"Prix : <b>{price:.2f} €</b>\n"
                    f"ISBN : <code>{isbn}</code>\n"
                    f"🔗 {url}"
                )
                send_telegram(msg)
                print("    → alerte envoyée ✅")
            else:
                print("    (déjà signalé à ce prix, pas de nouvelle alerte)")
            items[isbn] = {"alerted_price": price}
        else:
            # Indisponible ou au-dessus du seuil : on réarme pour une future baisse.
            items[isbn] = {"alerted_price": None}

        if i < len(batch):
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    state["cursor"] = (cursor + BATCH_SIZE) % total
    state["items"] = items
    save_state(state)
    print(f"Terminé. Prochain lot à partir de l'indice {state['cursor']}.")


if __name__ == "__main__":
    main()
