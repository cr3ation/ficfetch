"""AO3 page scraping: search listings, blurb parsing, download-URL resolution."""
import re
import urllib.parse

import httpx
from bs4 import BeautifulSoup

from .ao3_client import AO3Client, AO3Error, RestrictedWorkError
from .config import Settings
from .events import EventBus
from .models import SearchRequest, SearchResponse, TagHit, TagLookup, Work
from .utils import encode_tag, parse_work_ref

WORKS_PER_PAGE = 20
# A listing that redirects more often than this in a row is a loop, not a rename.
MAX_LISTING_REDIRECTS = 3

# Listing base URL -> where AO3 redirected it (non-canonical tag -> canonical).
# Resolving "star wars" alone has taken AO3 30+ s, so a retry skips the lookup.
_listing_redirects: dict[str, str] = {}


def _parse_count(scope, selector: str) -> int | None:
    """Read a numeric stat. `scope` is a blurb <li> or a whole work-page soup."""
    # AO3 omits the whole dd element at zero (kudos/bookmarks), hence None, not 0.
    el = scope.select_one(selector)
    if not el:
        return None
    digits = re.sub(r"[^\d]", "", el.get_text())
    return int(digits) if digits else None


def _text_or_none(scope, selector: str) -> str | None:
    el = scope.select_one(selector)
    return el.get_text(strip=True) if el else None


def _derive_complete(chapters: str | None) -> bool | None:
    if not chapters or "/" not in chapters:
        return None
    _, _, total = chapters.partition("/")
    posted, _, _ = chapters.partition("/")
    if not total.strip().isdigit():
        return False  # "3/?" = WIP
    return posted.strip() == total.strip()


def parse_blurbs(html: str) -> list[Work]:
    soup = BeautifulSoup(html, "lxml")
    works: list[Work] = []
    for li in soup.select("li.work.blurb.group"):
        li_id = li.get("id", "")
        match = re.fullmatch(r"work_(\d+)", li_id)
        if not match:
            continue
        work_id = match.group(1)

        a_title = li.select_one("h4.heading a[href^='/works/']")
        title = a_title.get_text(strip=True) if a_title else f"Work {work_id}"

        authors = [a.get_text(strip=True) for a in li.select("a[rel='author']")]
        if not authors:
            authors = ["Anonymous"]

        word_count = None
        dd_words = li.select_one("dd.words")
        if dd_words:
            digits = re.sub(r"[^\d]", "", dd_words.get_text())
            if digits:
                word_count = int(digits)

        tags = [t.get_text(strip=True) for t in li.select("ul.tags li a.tag")]
        fandoms = [f.get_text(strip=True) for f in li.select("h5.fandoms a.tag")]

        summary_el = li.select_one("blockquote.userstuff.summary")
        summary = summary_el.get_text("\n", strip=True) if summary_el else ""

        series_el = li.select_one("ul.series")
        series = series_el.get_text(" ", strip=True) if series_el else None

        chapters_el = li.select_one("dd.chapters")
        chapters = chapters_el.get_text(strip=True) if chapters_el else None

        rating = None
        rating_el = li.select_one("ul.required-tags span.rating span.text")
        if rating_el:
            rating = rating_el.get_text(strip=True)
        else:
            rating_outer = li.select_one("ul.required-tags span.rating")
            if rating_outer and rating_outer.get("title"):
                rating = rating_outer["title"]

        works.append(
            Work(
                work_id=work_id,
                title=title,
                authors=authors,
                word_count=word_count,
                tags=tags,
                fandoms=fandoms,
                summary=summary,
                series=series,
                kudos=_parse_count(li, "dd.kudos a") or _parse_count(li, "dd.kudos"),
                hits=_parse_count(li, "dd.hits"),
                bookmarks=_parse_count(li, "dd.bookmarks a") or _parse_count(li, "dd.bookmarks"),
                chapters=chapters,
                rating=rating,
                complete=_derive_complete(chapters),
            )
        )
    return works


def parse_work_page(html: str, work_id: str) -> Work:
    """Build a Work from a single work's page (selectors verified against AO3)."""
    soup = BeautifulSoup(html, "lxml")

    title = _text_or_none(soup, "h2.title.heading") or f"Work {work_id}"
    authors = [a.get_text(strip=True) for a in soup.select("a[rel='author']")] or ["Anonymous"]
    chapters = _text_or_none(soup, "dd.chapters")

    # Mirror the tag set parse_blurbs collects, in AO3's own display order.
    tag_selectors = "dd.warning.tags a.tag, dd.relationship.tags a.tag, dd.character.tags a.tag, dd.freeform.tags a.tag"
    tags = [t.get_text(strip=True) for t in soup.select(tag_selectors)]

    summary_el = soup.select_one("div.summary blockquote.userstuff")

    return Work(
        work_id=work_id,
        title=title,
        authors=authors,
        word_count=_parse_count(soup, "dd.words"),
        tags=tags,
        fandoms=[f.get_text(strip=True) for f in soup.select("dd.fandom.tags a.tag")],
        summary=summary_el.get_text("\n", strip=True) if summary_el else "",
        series=_text_or_none(soup, "dd.series span.position"),
        kudos=_parse_count(soup, "dd.kudos"),
        hits=_parse_count(soup, "dd.hits"),
        bookmarks=_parse_count(soup, "dd.bookmarks"),
        chapters=chapters,
        rating=_text_or_none(soup, "dd.rating.tags a.tag"),
        complete=_derive_complete(chapters),
    )


async def fetch_single_work(
    client: AO3Client, settings: Settings, work_id: str
) -> tuple[list[Work], str | None]:
    """Look up one work by ID, for a pasted work or chapter URL."""
    resp = await client.get(f"{settings.base_url}/works/{work_id}", params={"view_adult": "true"})

    if resp.status_code == 404:
        return [], f"Work {work_id} not found — it may have been deleted or made private."
    if looks_like_login(resp):
        return [], f"Work {work_id} is restricted and requires an AO3 login."
    if resp.status_code != 200:
        raise AO3Error(f"Unexpected status {resp.status_code} fetching work {work_id}.")

    return [parse_work_page(resp.text, work_id)], None


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    next_el = soup.select_one("ol.pagination li.next")
    return bool(next_el and next_el.select_one("a"))


def looks_like_login(resp: httpx.Response) -> bool:
    """True when AO3 redirected us to the login page (restricted work).

    Only the final URL is checked: AO3 renders a small login form in the header
    of *every* page for logged-out visitors, so looking for one in the body
    matches any public page.
    """
    return "/users/login" in str(resp.url)


def build_listing_params(req: SearchRequest) -> dict:
    """Sort + filter params for filtered listings (/users/{u}/works, /tags/{t}/works)."""
    params: dict = {
        "work_search[sort_column]": req.sort_by,
        "work_search[sort_direction]": "desc",
    }
    if req.complete_only:
        params["work_search[complete]"] = "T"
    if req.words_from is not None:
        params["work_search[words_from]"] = req.words_from
    if req.words_to is not None:
        params["work_search[words_to]"] = req.words_to
    if req.exclude_tags:
        params["work_search[excluded_tag_names]"] = ",".join(req.exclude_tags)
    if req.search_type == "tag":
        # AO3's "Other tags to include" filter: works must carry every one.
        extra_tags = split_tags(req.query)[1:]
        if extra_tags:
            params["work_search[other_tag_names]"] = ",".join(extra_tags)
    return params


def split_tags(query: str) -> list[str]:
    """Comma-separated tags (AO3 tag names can't contain commas): the first is
    listed, the rest narrow it down — works must have all of them."""
    return [t.strip() for t in query.split(",") if t.strip()]


def build_generic_search_params(req: SearchRequest) -> dict:
    """Sort + filter params for keyword searches (AO3's general /works/search)."""
    query = req.query
    # /works/search has no excluded_tag_names param; folding -"tag" terms into
    # the query matches tag text rather than canonical tag identity — an approximation.
    for tag in req.exclude_tags:
        query += f' -"{tag}"'
    params: dict = {
        "work_search[query]": query,
        "work_search[sort_column]": req.sort_by,
        "work_search[sort_direction]": "desc",
    }
    if req.complete_only:
        params["work_search[complete]"] = "T"
    # The search form's word_count field uses range syntax; > and < are
    # exclusive, so single-ended bounds shift by one to stay inclusive.
    if req.words_from is not None and req.words_to is not None:
        params["work_search[word_count]"] = f"{req.words_from}-{req.words_to}"
    elif req.words_from is not None and req.words_from > 0:
        params["work_search[word_count]"] = f">{req.words_from - 1}"
    elif req.words_to is not None:
        params["work_search[word_count]"] = f"<{req.words_to + 1}"
    return params


def parse_tag_search(html: str) -> tuple[int, list[TagHit]]:
    """(total matches, tags on this page) from AO3's /tags/search results."""
    soup = BeautifulSoup(html, "lxml")
    tags: list[TagHit] = []
    for span in soup.select("ol.tag.index > li > span"):
        link = span.select_one("a.tag")
        if not link:
            continue
        # "Character: Anakin Skywalker (53049)" — AO3 puts an &lrm; before the count
        text = span.get_text(" ", strip=True)
        uses = re.search(r"\((\d[\d,]*)\)\s*$", text)
        tags.append(
            TagHit(
                name=link.get_text(strip=True),
                type=text.split(":", 1)[0].strip(),
                uses=int(uses.group(1).replace(",", "")) if uses else 0,
            )
        )
    total = re.search(r"([\d,]+)\s+Found", soup.get_text(" "))
    return (int(total.group(1).replace(",", "")) if total else len(tags)), tags


async def find_tags(
    client: AO3Client, settings: Settings, bus: EventBus, term: str, *, retries: int | None = None
) -> TagLookup:
    """Canonical AO3 tags matching `term`, most used first — the tag picker's list.

    Canonical tags are exactly the ones with a works list, so anything picked
    from here can be searched. One results page (AO3 lists 50) is fetched; the
    picker filters it locally rather than asking AO3 on every keystroke.
    """
    bus.log("info", f"Looking up AO3 tags matching '{term}'...")
    resp = await client.get(
        f"{settings.base_url}/tags/search",
        params={
            "tag_search[name]": term,
            "tag_search[canonical]": "T",
            "tag_search[sort_column]": "uses",
            "tag_search[sort_direction]": "desc",
        },
        follow_redirects=False,
        retries=retries,
    )
    if resp.status_code != 200:
        raise AO3Error(f"Unexpected status {resp.status_code} searching tags for '{term}'")
    total, tags = parse_tag_search(resp.text)
    bus.log("info", f"Found {total:,} AO3 tags matching '{term}'" + (f" (showing the {len(tags)} most used)." if total > len(tags) else "."))
    return TagLookup(query=term, total=total, tags=tags)


async def _tag_lookup_or_none(client: AO3Client, settings: Settings, bus: EventBus, term: str) -> TagLookup | None:
    # Best effort, single attempt: the search has already failed, and a retry
    # wait would hold AO3's rate-limit lock (and every download). The picker
    # offers "Try again" when this comes back empty.
    try:
        return await find_tags(client, settings, bus, term, retries=0)
    except AO3Error as exc:
        bus.log("warning", f"Couldn't look up similar tags on AO3 ({exc}).")
        return None


async def search(
    client: AO3Client,
    settings: Settings,
    bus: EventBus,
    req: SearchRequest,
    max_results: int,
) -> SearchResponse:
    """Fetch listing pages until max_results works are collected or pages run out.

    A tag search stops rather than guess when AO3 has no works list for the
    tag; the response then carries tag_unavailable and suggested tags.
    """
    query = req.query

    # A pasted work/chapter URL identifies exactly one work, so it wins over
    # the search-type toggle and skips listing and pagination entirely.
    work_id = parse_work_ref(query)
    if work_id:
        bus.log("info", f"Recognised a work link — fetching work {work_id} directly.")
        works, message = await fetch_single_work(client, settings, work_id)
        return SearchResponse(works=works, message=message)

    if req.search_type == "keyword":
        base = f"{settings.base_url}/works/search"
        params = build_generic_search_params(req)
    else:
        if req.search_type == "author":
            base = f"{settings.base_url}/users/{urllib.parse.quote(query)}/works"
        else:
            # Several tags: list the first; build_listing_params filters on the rest.
            tag = split_tags(query)[0]
            base = f"{settings.base_url}/tags/{encode_tag(tag)}/works"
        base = _listing_redirects.get(base, base)
        params = build_listing_params(req)

    works: list[Work] = []
    seen_ids: set[str] = set()
    page = 1
    while page <= settings.max_pages and len(works) < max_results:
        request_params = {**params, "page": page, "view_adult": "true"}
        # AO3 redirects non-canonical tags (e.g. "Hermione" -> "Hermione
        # Granger") with a Location that carries no query string at all,
        # silently dropping page/sort/filters. Redirects are therefore not
        # followed: following one would download the whole unfiltered page
        # just to throw it away — on a big fandom a 100 s AO3 request.
        # Point base at the real destination and redo the request instead.
        original_base = base
        for _ in range(MAX_LISTING_REDIRECTS + 1):
            resp = await client.get(base, params=request_params, follow_redirects=False)
            if not resp.is_redirect:
                break
            target = urllib.parse.urljoin(base, resp.headers["location"]).split("?", 1)[0]
            if not target.endswith("/works"):
                # Leads off the works lists: a non-canonical tag with no
                # canonical synonym only has an info page (AO3: "not a common
                # tag yet"), which can't be sorted or filtered. Don't fetch it.
                break
            base = target
            new_label = urllib.parse.unquote(urllib.parse.urlparse(base).path)
            bus.log("info", f"'{query}' redirected to {new_label} — retrying with your search parameters attached.")
        else:
            raise AO3Error(f"Too many redirects fetching {original_base}")
        if base != original_base:
            _listing_redirects[original_base] = base

        if resp.status_code == 404 or resp.is_redirect:
            if page > 1 or req.search_type == "keyword":
                break
            if req.search_type == "author":
                return SearchResponse(works=[], message=f"User '{query}' not found on AO3.")
            # A tag search means "the works AO3 files under this tag". When there
            # is no such list, say so instead of guessing with a general search
            # (a different, much broader result) — and offer tags that do work.
            if resp.status_code == 404:
                message = f"AO3 has no tag called '{tag}'."
            else:
                name = urllib.parse.unquote(urllib.parse.urlparse(target).path.rsplit("/", 1)[-1])
                message = f"'{name}' exists on AO3 but isn't a common tag yet, so AO3 can't list or filter its works."
            return SearchResponse(
                works=[],
                message=message,
                tag_unavailable=True,
                tag_lookup=await _tag_lookup_or_none(client, settings, bus, tag),
            )

        if resp.status_code != 200:
            raise AO3Error(f"Unexpected status {resp.status_code} fetching {resp.url}")

        page_works = parse_blurbs(resp.text)
        if not page_works:
            if page == 1:
                bus.log("info", f"No works found for '{query}'.")
            break

        # AO3's search index can shift slightly between paginated requests,
        # occasionally repeating a work across two "pages" — guard against
        # that (and any other future source of overlap) structurally rather
        # than trusting each page to be disjoint from the last.
        new_works = [w for w in page_works if w.work_id not in seen_ids]
        seen_ids.update(w.work_id for w in new_works)
        works.extend(new_works)
        found = min(len(works), max_results)
        bus.log("info", f"Page {page}: found {len(page_works)} works ({found} total).")
        bus.publish("search_progress", {"current": found, "total": max_results})

        if not has_next_page(resp.text):
            break
        page += 1

    truncated = len(works) > max_results or (len(works) == max_results and page <= settings.max_pages)
    return SearchResponse(works=works[:max_results], truncated=truncated)


def _find_download_href(html: str, ext: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("li.download ul li a"):
        href = a.get("href", "")
        path = urllib.parse.urlparse(href).path
        if path.lower().endswith(f".{ext}"):
            return href
    return None


def _is_ebook_response(resp: httpx.Response, ext: str) -> bool:
    if resp.status_code != 200:
        return False
    if "/downloads/" not in str(resp.url):
        return False  # redirected away (login page, error page)
    content_type = resp.headers.get("content-type", "")
    if ext == "html":
        return True  # HTML downloads legitimately have a text/html content type
    return not content_type.startswith("text/html")


async def download_work(
    client: AO3Client,
    settings: Settings,
    work: Work,
    fmt: str,
) -> bytes:
    """Fetch the e-book bytes for a work using AO3's built-in download endpoints.

    Tries the constructed /downloads/{id}/w.{ext} URL first (the slug segment is
    cosmetic); falls back to parsing the real link from the work page.
    """
    direct = f"{settings.base_url}/downloads/{work.work_id}/w.{fmt}"
    resp = await client.get(direct)
    if _is_ebook_response(resp, fmt):
        return resp.content

    page = await client.get(f"{settings.base_url}/works/{work.work_id}", params={"view_adult": "true"})
    if looks_like_login(page):
        raise RestrictedWorkError(f"Work {work.work_id} is restricted (requires AO3 login).")
    if page.status_code == 404:
        raise AO3Error(f"Work {work.work_id} not found (deleted or hidden).")
    if page.status_code != 200:
        raise AO3Error(f"Unexpected status {page.status_code} fetching work page {work.work_id}.")

    href = _find_download_href(page.text, fmt)
    if not href:
        raise AO3Error(f"No {fmt.upper()} download link found for work {work.work_id}.")

    url = urllib.parse.urljoin(settings.base_url, href)
    resp = await client.get(url)
    if not _is_ebook_response(resp, fmt):
        raise AO3Error(f"Download failed for work {work.work_id} (status {resp.status_code}).")
    return resp.content
