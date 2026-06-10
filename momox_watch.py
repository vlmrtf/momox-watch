#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
momox_watch.py
--------------
Surveille une liste d'ISBN sur momox-shop.fr et envoie une alerte Telegram
dès qu'un titre est disponible à un prix inférieur au seuil (7 € par défaut).

Conçu pour tourner gratuitement et en permanence via GitHub Actions
(aucun PC à laisser allumé). L'état est sauvegardé dans state.json pour
éviter de renvoyer plusieurs fois la même alerte.

Usage local :
    python momox_watch.py            # exécution normale
    python momox_watch.py --debug    # sauvegarde le HtML récupéré pour
                                     # vérifier/ajuster les sélecteurs
"""

import json
import os
import re
import sys
import time
import random
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Configuration (modifiable via variables d'environnement / secrets GitHub)
# ----------------------------------------------------------------------------
THRESHOLD_EUR = float(os.environ.get("PRICE_THRESHOLD", "7.0"))
ISBN_FILE = Path(os.environ.get("ISBN_FILE", "isbns.txt"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()

# Chat ID inscrit directement ici (peu sensible). Reste modifiable via la
# variable d'environnement TELEGRAM_CHAT_ID si besoin.
# ⚠️ Ce numéro doit être TON identifiant donné par @getidsbot ("Your user ID"),
# pas l'identifiant du bot. Le TOKEN, lui, reste un secret GitHub.
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8881038521").strip()

DEBUG = "--debug" in sys.argv

# Délai (en secondes) entre deux requêtes vers Momox, pour rester discret.
# Si tu vois des blocages (pages Cloudflare), augmente ces valeurs.
MIN_DELAY, MAX_DELAY = 1.0, 2.0

# Rotation par lots : à chaque exécution, on ne traite qu'un lot de BATCH_SIZE
# ISBN, puis on avance dans la liste (curseur mémorisé dans state.json).
# Avec 135 ISBN et un lot de 25, toute la liste est couverte en ~6 exécutions.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "25"))

# --- Points à AJUSTER si Momox change sa structure (voir le README) ---------
# URL de recherche par ISBN. {isbn} est remplacé par l'ISBN.
MOMOX_SEARCH_URL = "https://www.momox-shop.fr/produkte-suchen/?searchType=&searchString={isbn}"
# Sélecteur CSS du lien vers la fiche produit dans la page de résultats.
PRODUCT_LINK_SELECTOR = "a.product-item-link, a[href*='-M']"
# ----------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ----------------------------------------------------------------------------
# Utilitaires de parsing
# ----------------------------------------------------------------------------
def to_float(val):
    """Convertit '6,99 €', '6.99', 6.99 -> 6.99 (ou None)."""
    if val is None:
        return None
    s = str(val).replace("\xa0", " ").replace("€", "").strip()
    s = s.replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def iter_jsonld(data):
    """Parcourt récursivement une structure JSON-LD (gère @graph et imbrications)."""
    if isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            for node in data["@graph"]:
                yield from iter_jsonld(node)
        yield data
        for v in data.values():
            if isinstance(v, (dict, list)):
                yield from iter_jsonld(v)
    elif isinstance(data, list):
        for node in data:
            yield from iter_jsonld(node)


def extract_price_from_offers(node):
    """Récupère le prix le plus bas d'un noeud schema.org Product/Offer."""
    candidates = []

    def collect(o):
        if isinstance(o, dict):
            for key in ("price", "lowPrice"):
                if o.get(key) is not None:
                    p = to_float(o[key])
                    if p is not None:
                        candidates.append(p)
            if "offers" in o:
                collect(o["offers"])
        elif isinstance(o, list):
            for x in o:
                collect(x)

    if node.get("offers") is not None:
        collect(node["offers"])
    if node.get("price") is not None:
        p = to_float(node["price"])
        if p is not None:
            candidates.append(p)

    return min(candidates) if candidates else None


def parse_price_from_html(html):
    """
    Essaie plusieurs stratégies pour trouver le prix le plus bas disponible.
    1) Données structurées JSON-LD (le plus fiable)
    2) Balises meta (itemprop / og)
    3) Repli regex sur un prix visible
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) JSON-LD schema.org
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for node in iter_jsonld(data):
            if isinstance(node, dict) and node.get("@type") in (
                "Product", "Offer", "AggregateOffer",
            ):
                price = extract_price_from_offers(node)
                if price is not None:
                    return price

    # 2) Balises meta classiques
    for attrs in (
        {"property": "product:price:amount"},
        {"itemprop": "price"},
        {"property": "og:price:amount"},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            p = to_float(meta["content"])
            if p is not None:
                return p

    # 3) Repli : premier motif "X,XX €" trouvé dans la page
    m = re.search(r"(\d{1,3}[.,]\d{2})\s*€", soup.get_text(" "))
    if m:
        return to_float(m.group(1))

    return None


# ----------------------------------------------------------------------------
# Récupération d'une offre pour un ISBN
# ----------------------------------------------------------------------------
def fetch_offer(session, isbn):
    """
    Renvoie le prix le plus bas disponible pour l'ISBN sur momox-shop.fr,
    ou None si introuvable / indisponible.
    """
    search_url = MOMOX_SEARCH_URL.format(isbn=isbn)
    try:
        resp = session.get(search_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [!] Erreur requête recherche pour {isbn} : {e}")
        return None

    if DEBUG:
        Path(f"debug_search_{isbn}.html").write_text(resp.text, encoding="utf-8")
        print(f"  [debug] HTML de recherche écrit dans debug_search_{isbn}.html")

    # La page de recherche contient parfois déjà le prix (JSON-LD).
    price = parse_price_from_html(resp.text)
    if price is not None:
        return price

    # Sinon, on suit le lien vers la fiche produit puis on relit le prix.
    soup = BeautifulSoup(resp.text, "html.parser")
    link = soup.select_one(PRODUCT_LINK_SELECTOR)
    if not link or not link.get("href"):
        return None

    href = link["href"]
    if href.startswith("/"):
        href = "https://www.momox-shop.fr" + href

    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    try:
        resp2 = session.get(href, headers=HEADERS, timeout=30)
        resp2.raise_for_status()
    except Exception as e:
        print(f"  [!] Erreur requête fiche pour {isbn} : {e}")
        return None

    if DEBUG:
        Path(f"debug_product_{isbn}.html").write_text(resp2.text, encoding="utf-8")
        print(f"  [debug] HTML de fiche écrit dans debug_product_{isbn}.html")

    return parse_price_from_html(resp2.text)


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID manquants : "
              "alerte non envoyée (affichage console).")
        print("  >>>", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  [!] Échec envoi Telegram : {e}")


# ----------------------------------------------------------------------------
# État
# ----------------------------------------------------------------------------
def load_state():
    """
    État au format {"cursor": int, "items": {isbn: {"alerted_price": ...}}}.
    Migre automatiquement un ancien état plat ou vide.
    """
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "items" in data and isinstance(data["items"], dict):
                    data.setdefault("cursor", 0)
                    return data
                # Ancien format plat {isbn: {...}} -> migration
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
# Programme principal
# ----------------------------------------------------------------------------
def main():
    all_isbns = load_isbns()
    if not all_isbns:
        print("Aucun ISBN à surveiller. Ajoute-les dans isbns.txt.")
        return

    state = load_state()
    items = state["items"]
    total = len(all_isbns)

    # Curseur de rotation : où reprendre dans la liste.
    cursor = state.get("cursor", 0)
    if cursor >= total:
        cursor = 0

    # Lot courant (avec retour au début si on déborde de la fin).
    end = cursor + BATCH_SIZE
    batch = all_isbns[cursor:end]
    if end > total:
        batch += all_isbns[: end - total]  # complète en repartant du début

    session = requests.Session()
    print(f"Liste : {total} ISBN — seuil = {THRESHOLD_EUR:.2f} €")
    print(f"Lot courant : indices {cursor}..{cursor + len(batch) - 1} "
          f"({len(batch)} ISBN)")

    for i, isbn in enumerate(batch, 1):
        print(f"[{i}/{len(batch)}] ISBN {isbn} ...")
        price = fetch_offer(session, isbn)
        prev_alerted = items.get(isbn, {}).get("alerted_price")

        if price is not None:
            print(f"    prix trouvé : {price:.2f} €")

        if price is not None and price < THRESHOLD_EUR:
            # On alerte si on n'avait jamais alerté, ou si le prix a baissé.
            if prev_alerted is None or price < prev_alerted:
                msg = (
                    f"📚 <b>Nouvelle offre Momox</b>\n"
                    f"ISBN : <code>{isbn}</code>\n"
                    f"Prix : <b>{price:.2f} €</b> (seuil {THRESHOLD_EUR:.2f} €)\n"
                    f"🔗 {MOMOX_SEARCH_URL.format(isbn=isbn)}"
                )
                send_telegram(msg)
                print("    → alerte envoyée ✅")
            else:
                print("    (déjà signalé à ce prix, pas de nouvelle alerte)")
            items[isbn] = {"alerted_price": price}
        else:
            # Plus dispo ou au-dessus du seuil : on réarme pour une future baisse.
            items[isbn] = {"alerted_price": None}

        # Pause polie entre deux ISBN (sauf le dernier du lot).
        if i < len(batch):
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # Avance le curseur pour le prochain lot (retour à 0 en fin de liste).
    state["cursor"] = (cursor + BATCH_SIZE) % total
    state["items"] = items
    save_state(state)
    print(f"Terminé. Prochain lot à partir de l'indice {state['cursor']}.")


if __name__ == "__main__":
    main()
