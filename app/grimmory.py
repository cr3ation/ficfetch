"""Grimmory (and BookLore) metadata: configuration held in the database, plus
the rules for splitting AO3's flat subject list into genres and tags.

AO3's own EPUB export puts everything — rating, category, archive warnings,
fandoms, relationships, characters and every freeform tag — into flat
<dc:subject> elements, because EPUB has no notion of "genre" versus "tag".
Grimmory maps dc:subject to categories, which its UI calls Genres, so an
untouched AO3 file arrives with fifty genres and no tags.

Grimmory also reads its own `booklore:` metadata, and removes anything listed
there from the categories it derived from dc:subject. Writing booklore:tags is
therefore purely additive: Grimmory gets a clean split, while calibre and every
other reader still see the full dc:subject list they see today.
"""
from pathlib import Path

from . import db
from .models import GrimmoryConfig, Work

KEYS = ("grimmory.enabled", "grimmory.genres", "grimmory.trim_subjects", "grimmory.map_rating")

GENRE_MODES = ("tag_only", "fandoms", "extended")
DEFAULT_GENRE_MODE = "fandoms"

# AO3's fixed vocabularies. Everything outside these is a relationship, a
# character or a freeform tag — the long tail that belongs under Tags.
AO3_RATINGS = {
    "Not Rated",
    "General Audiences",
    "Teen And Up Audiences",
    "Mature",
    "Explicit",
}
AO3_CATEGORIES = {"F/F", "F/M", "Gen", "M/M", "Multi", "Other"}
AO3_WARNINGS = {
    "Creator Chose Not To Use Archive Warnings",
    "No Archive Warnings Apply",
    "Graphic Depictions Of Violence",
    "Major Character Death",
    "Rape/Non-Con",
    "Underage",
    "Underage Sex",
}
# AO3 stamps this on every export, and it reads as a genre rather than a tag.
BASE_GENRES = {"Fanworks"}

# AO3 rating -> (Grimmory ContentRating enum member, one of its valid age ratings).
# "Not Rated" is deliberately absent: no rating is better than a wrong one.
AO3_RATING_MAP = {
    "General Audiences": ("EVERYONE", 0),
    "Teen And Up Audiences": ("TEEN", 13),
    "Mature": ("MATURE", 16),
    "Explicit": ("EXPLICIT", 18),
}


def load_grimmory_config(db_path: Path) -> GrimmoryConfig:
    """Read at download time, which is what makes the GUI toggle take effect
    without a container restart — the same trick oidc.load_oidc_config uses."""
    values = db.get_settings(db_path, "grimmory.")
    genres = values.get("grimmory.genres", DEFAULT_GENRE_MODE)
    return GrimmoryConfig(
        enabled=values.get("grimmory.enabled", "false") == "true",
        genres=genres if genres in GENRE_MODES else DEFAULT_GENRE_MODE,
        trim_subjects=values.get("grimmory.trim_subjects", "false") == "true",
        map_rating=values.get("grimmory.map_rating", "true") == "true",
    )


def split_subjects(
    subjects: list[str], work: Work, *, epub_tag: str, genre_mode: str
) -> tuple[list[str], list[str]]:
    """Partition an EPUB's dc:subject values into (genres, tags).

    Driven by the file's own subject list rather than by the scraped Work, so
    every subject is classified. An unrecognised one falls through to tags,
    which is the harmless direction — the goal is a Genres list you can browse.

    Matching is exact (bar case), never fuzzy: AO3's export uses the same tag
    names the work page shows, and loosening it would file the character
    "Harry Potter" under the fandom "Harry Potter - Fandom".
    """
    keep = {s.casefold() for s in BASE_GENRES}
    if epub_tag:
        keep.add(epub_tag.casefold())
    if genre_mode in ("fandoms", "extended"):
        keep |= {f.casefold() for f in work.fandoms}
    if genre_mode == "extended":
        keep |= {s.casefold() for s in AO3_RATINGS | AO3_CATEGORIES | AO3_WARNINGS}

    genres, tags = [], []
    for subject in subjects:
        target = genres if subject.casefold() in keep else tags
        if subject not in target:
            target.append(subject)
    return genres, tags


def _sanitize(value: str) -> str:
    """Strip commas out of a tag value.

    Grimmory's parseJsonArrayOrCsv splits on every comma without honouring the
    surrounding quotes, so "Sorry, Not Sorry" would arrive as two mangled tags.
    Dropping the comma keeps the tag readable. The side effect, when subjects
    are left in place, is that the sanitised tag no longer matches its own
    dc:subject and so also lingers as a genre — turn on "remove the moved tags"
    to clear that up.
    """
    return " ".join(value.replace(",", " ").split())


def _json_array(values: list[str]) -> str:
    """Serialise as the JSON array Grimmory's own EpubMetadataWriter emits, so a
    file round-trips unchanged if Grimmory later writes its metadata back."""
    escaped = (_sanitize(v).replace("\\", "\\\\").replace('"', '\\"') for v in values)
    return "[" + ", ".join(f'"{v}"' for v in escaped) + "]"


def booklore_metas(work: Work, tags: list[str], *, map_rating: bool) -> list[tuple[str, str]]:
    """The booklore: meta entries to embed, as (name, value) pairs."""
    metas = []
    if tags:
        metas.append(("booklore:tags", _json_array(tags)))
    if map_rating and work.rating in AO3_RATING_MAP:
        content_rating, age_rating = AO3_RATING_MAP[work.rating]
        metas.append(("booklore:content_rating", content_rating))
        metas.append(("booklore:age_rating", str(age_rating)))
    return metas
