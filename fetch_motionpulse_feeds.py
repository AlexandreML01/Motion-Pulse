#!/usr/bin/env python3
"""
MotionPulse AI v2 — Script de veille RSS + enrichissement IA (Claude)
=======================================================================

Ce script :
  1. Récupère les derniers articles de plusieurs flux RSS spécialisés
     en Motion Design / VFX / 3D.
  2. Extrait l'image principale de chaque article depuis les flux RSS.
  3. Envoie chaque article à Claude (claude-haiku-4-5-20251001) pour en
     extraire un résumé, une catégorie, un impact workflow, une palette
     de couleurs et des tags techniques.
  4. Exporte le tout dans un fichier data.json prêt à être consommé par
     l'application web MotionPulse AI v2.

Sécurité :
  La clé API n'est JAMAIS écrite en dur ici. Elle est lue depuis la
  variable d'environnement ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import feedparser
from anthropic import Anthropic, APIError, APIStatusError, RateLimitError

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

MODEL = "claude-haiku-4-5-20251001"  # modèle rapide et économique

FEEDS: dict[str, str] = {
    "Motionographer": "https://motionographer.com/feed/",
    "Stash Media": "https://www.stashmedia.tv/feed/",
    "Lesterbanks": "https://lesterbanks.com/feed/",
    "Catsuka": "https://www.catsuka.com/rss.xml",
    "BlenderNation": "https://www.blendernation.com/feed/",
    "CG Channel": "https://www.cgchannel.com/feed/",
}

# Nombre max d'articles traités par flux
MAX_ARTICLES_PER_FEED = 5

CATEGORIES = ["3D & VFX", "Motion Graphics", "AI Animation", "Plugins & Updates"]
CATEGORY_TO_ID = {
    "3D & VFX": "vfx",
    "Motion Graphics": "motion",
    "AI Animation": "ai",
    "Plugins & Updates": "plugins",
}
STYLES = [
    "2D Frame-by-frame", "3D Photoréaliste", "Kinetic Typography",
    "Cel Animation", "Mixed Media", "UI/UX Motion",
]

OUTPUT_FILE = "data.json"
DELAY_BETWEEN_CALLS_SEC = 0.6  # pause pour respecter le rate-limit


# ---------------------------------------------------------------------------
# STRUCTURES DE DONNÉES
# ---------------------------------------------------------------------------

@dataclass
class RawArticle:
    title: str
    link: str
    source: str
    published: str
    description: str
    image_url: str


@dataclass
class EnrichedArticle:
    id: int
    title: str
    summary: str
    category: str
    categoryId: str
    workflowImpact: str
    colorPalette: list[str]
    source: str
    link: str
    date: str
    tags: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    changelog: bool = False
    thumb: int = 0
    imageUrl: str = ""


# ---------------------------------------------------------------------------
# ÉTAPE 1 — EXTRACTION RSS & IMAGES
# ---------------------------------------------------------------------------

def strip_html(raw_html: str) -> str:
    """Nettoie grossièrement le HTML d'une description RSS pour Claude."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


def extract_image_url(entry: Any) -> str:
    """Extrait la meilleure URL d'image disponible dans une entrée RSS."""
    # 1. Vérification media:content ou media_thumbnail (standard RSS médias)
    if hasattr(entry, "media_content"):
        for media in entry.media_content:
            url = media.get("url")
            if url:
                return url

    if hasattr(entry, "media_thumbnail") and len(entry.media_thumbnail) > 0:
        url = entry.media_thumbnail[0].get("url")
        if url:
            return url

    # 2. Vérification des enclosures (fichiers attachés)
    if hasattr(entry, "enclosures"):
        for enc in entry.enclosures:
            if "type" in enc and "image" in enc["type"] and "href" in enc:
                return enc["href"]

    # 3. Recherche par expression régulière d'une balise <img> dans le HTML de l'article
    html_content = ""
    if hasattr(entry, "content") and entry.content:
        html_content = entry.content[0].value
    elif hasattr(entry, "summary"):
        html_content = entry.summary
    elif hasattr(entry, "description"):
        html_content = entry.description

    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_content)
    if img_match:
        return img_match.group(1)

    # 4. Image de secours par défaut si aucune image n'est trouvée dans le flux
    return "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?q=80&w=1000&auto=format&fit=crop"


def fetch_feed(name: str, url: str, limit: int) -> list[RawArticle]:
    print(f"  → Récupération de {name} ({url})")
    parsed = feedparser.parse(url)

    if parsed.bozo and not parsed.entries:
        print(f"    ⚠️ Impossible de parser ce flux ({parsed.bozo_exception}). Ignoré.")
        return []

    articles: list[RawArticle] = []
    for entry in parsed.entries[:limit]:
        title = entry.get("title", "Sans titre").strip()
        link = entry.get("link", "")
        published = entry.get("published", "") or entry.get("updated", "")
        description = strip_html(entry.get("summary", "") or entry.get("description", ""))
        image_url = extract_image_url(entry)

        articles.append(RawArticle(
            title=title,
            link=link,
            source=name,
            published=published,
            description=description,
            image_url=image_url,
        ))

    print(f"    ✓ {len(articles)} article(s) récupéré(s)")
    return articles


def fetch_all_feeds() -> list[RawArticle]:
    print("📡 Extraction des flux RSS...\n")
    all_articles: list[RawArticle] = []
    for name, url in FEEDS.items():
        try:
            all_articles.extend(fetch_feed(name, url, MAX_ARTICLES_PER_FEED))
        except Exception as exc:
            print(f"    ⚠️ Erreur sur {name} : {exc}. Ignoré.")
    print(f"\n✅ Total : {len(all_articles)} articles récupérés depuis {len(FEEDS)} sources.\n")
    return all_articles


# ---------------------------------------------------------------------------
# ÉTAPE 2 — ENRICHISSEMENT PAR CLAUDE
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""Tu es un analyste spécialisé en Motion Design, VFX, 3D et animation, \
au service d'une application de veille pour motion designers professionnels.

Pour l'article fourni, réponds UNIQUEMENT avec un objet JSON valide (aucun texte avant \
ou après, aucun bloc markdown ```), avec exactement ces clés :

{{
  "summary": "résumé clair en 2 phrases maximum, en français",
  "category": "une valeur EXACTE parmi {CATEGORIES}",
  "workflowImpact": "une phrase courte expliquant le bénéfice concret pour le workflow du motion designer",
  "colorPalette": ["#RRGGBB", "#RRGGBB", "#RRGGBB", "#RRGGBB", "#RRGGBB"],
  "tags": ["mot-clé1", "mot-clé2", "mot-clé3"],
  "styles": ["une ou deux valeurs EXACTES parmi {STYLES}"]
}}

Règles :
- "colorPalette" doit être 5 codes hexadécimaux plausibles et esthétiques évoquant le sujet.
- "tags" contient 3 à 4 mots-clés techniques précis (ex: C4D, After Effects, EEVEE, Octane).
- Ne mets jamais de texte hors du JSON. Le JSON doit être strictement valide (guillemets doubles).
"""


def build_user_prompt(article: RawArticle) -> str:
    return (
        f"Titre : {article.title}\n"
        f"Source : {article.source}\n"
        f"Extrait / description : {article.description or '(pas de description disponible)'}\n"
    )


def extract_json(raw_text: str) -> dict[str, Any] | None:
    raw_text = raw_text.strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def enrich_with_claude(client: Anthropic, article: RawArticle, article_id: int) -> EnrichedArticle | None:
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(article)}],
        )
    except RateLimitError:
        print("    ⏳ Rate limit atteint, pause de 10s puis nouvelle tentative...")
        time.sleep(10)
        return enrich_with_claude(client, article, article_id)
    except (APIStatusError, APIError) as exc:
        print(f"    ⚠️ Erreur API Claude sur « {article.title[:50]}... » : {exc}")
        return None

    raw_text = "".join(block.text for block in response.content if block.type == "text")
    data = extract_json(raw_text)

    if data is None:
        print(f"    ⚠️ Réponse JSON invalide pour « {article.title[:50]}... », article ignoré.")
        return None

    category = data.get("category") if data.get("category") in CATEGORIES else CATEGORIES[1]
    styles = [s for s in data.get("styles", []) if s in STYLES] or [STYLES[4]]
    colors = data.get("colorPalette", [])
    if not isinstance(colors, list) or len(colors) < 5:
        colors = (colors + ["#0D0D16", "#00F0FF", "#A855F7", "#FF2E9A", "#FFB84D"])[:5]

    thumb_index = int(hashlib.md5(article.link.encode("utf-8")).hexdigest(), 16) % 6

    return EnrichedArticle(
        id=article_id,
        title=article.title,
        summary=data.get("summary", "").strip(),
        category=category,
        categoryId=CATEGORY_TO_ID[category],
        workflowImpact=data.get("workflowImpact", "").strip(),
        colorPalette=colors[:5],
        source=article.source,
        link=article.link,
        date=normalize_date(article.published),
        tags=data.get("tags", [])[:4],
        styles=styles,
        changelog=(category == "Plugins & Updates"),
        thumb=thumb_index,
        imageUrl=article.image_url,  # Injection de l'image récupérée
    )


def normalize_date(raw_date: str) -> str:
    if raw_date:
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                return datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def enrich_all_articles(articles: list[RawArticle]) -> list[EnrichedArticle]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ La variable d'environnement ANTHROPIC_API_KEY n'est pas définie.")
        sys.exit(1)

    client = Anthropic(api_key=api_key)

    print(f"🧠 Enrichissement de {len(articles)} articles via Claude ({MODEL})...\n")
    enriched: list[EnrichedArticle] = []

    for i, article in enumerate(articles, start=1):
        print(f"  [{i}/{len(articles)}] {article.title[:70]}")
        result = enrich_with_claude(client, article, article_id=i)
        if result:
            enriched.append(result)
        time.sleep(DELAY_BETWEEN_CALLS_SEC)

    print(f"\n✅ {len(enriched)}/{len(articles)} articles enrichis avec succès.\n")
    return enriched


# ---------------------------------------------------------------------------
# ÉTAPE 3 — EXPORT data.json
# ---------------------------------------------------------------------------

def export_to_json(articles: list[EnrichedArticle], path: str = OUTPUT_FILE) -> None:
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "count": len(articles),
        "items": [article.__dict__ for article in articles],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"💾 Fichier « {path} » généré avec {len(articles)} articles.")


def main() -> None:
    raw_articles = fetch_all_feeds()
    if not raw_articles:
        print("❌ Aucun article récupéré, arrêt du script.")
        sys.exit(1)

    enriched_articles = enrich_all_articles(raw_articles)
    if not enriched_articles:
        print("❌ Aucun article n'a pu être enrichi, data.json non généré.")
        sys.exit(1)

    export_to_json(enriched_articles)


if __name__ == "__main__":
    main()