"""
Kontrola "nic neblokuje indexaci Googlem".

DŮLEŽITÉ: tohle NEKONTROLUJE, jestli je stránka SKUTEČNĚ v Google indexu
(to jde zjistit jen přes Search Console, ne odsud) - kontroluje jen to,
jestli na stránce/serveru NENÍ něco, co by Googlu bránilo ji zaindexovat:
  - meta robots tag s "noindex"
  - HTTP hlavička X-Robots-Tag s "noindex"
  - robots.txt, který danou cestu zakazuje (Disallow)
  - existence sitemap.xml (informativní - chybějící sitemap není chyba,
    jen menší SEO doporučení)
  - canonical odkaz, jestli náhodou neukazuje na jinou URL (informativní)

Nic z tohohle nekontaktuje Google - jen tvůj vlastní web.
"""
from urllib.parse import urlparse, urljoin

ROBOTS_TIMEOUT_MS = 8000


def _parse_robots_txt(text: str):
    """Jednoduchý parser robots.txt - bere v úvahu jen 'User-agent: *' blok."""
    disallow_paths = []
    sitemaps = []
    in_wildcard_block = False
    applies_to_all = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            in_wildcard_block = value == "*"
        elif key == "disallow" and in_wildcard_block:
            if value == "":
                continue
            disallow_paths.append(value)
            if value == "/":
                applies_to_all = True
        elif key == "sitemap":
            sitemaps.append(value)
    return {"disallow_paths": disallow_paths, "disallow_all": applies_to_all, "sitemaps": sitemaps}


def _path_is_disallowed(path: str, disallow_paths) -> str | None:
    for rule in disallow_paths:
        # zjednodušený match: robots.txt Disallow je prefix (běžný případ);
        # '*' bereme jako "cokoliv" jen na konci pravidla
        rule_clean = rule.rstrip("*")
        if rule_clean and path.startswith(rule_clean):
            return rule
    return None


async def get_robots_txt_rules(context, origin: str, cache: dict):
    if origin in cache:
        return cache[origin]
    robots_url = urljoin(origin, "/robots.txt")
    try:
        resp = await context.request.get(robots_url, timeout=ROBOTS_TIMEOUT_MS)
        if resp.status >= 400:
            result = {"exists": False, "disallow_paths": [], "disallow_all": False, "sitemaps": []}
        else:
            text = await resp.text()
            parsed = _parse_robots_txt(text)
            result = {"exists": True, **parsed}
    except Exception as e:  # noqa: BLE001
        result = {"exists": None, "error": str(e)[:200], "disallow_paths": [], "disallow_all": False, "sitemaps": []}
    cache[origin] = result
    return result


async def check_sitemap(context, origin: str, url: str, cache: dict):
    if origin in cache:
        sitemap_info = cache[origin]
    else:
        sitemap_url = urljoin(origin, "/sitemap.xml")
        try:
            resp = await context.request.get(sitemap_url, timeout=ROBOTS_TIMEOUT_MS)
            if resp.status < 400:
                body = await resp.text()
                sitemap_info = {"exists": True, "body_sample": body[:200000]}
            else:
                sitemap_info = {"exists": False, "body_sample": ""}
        except Exception:
            sitemap_info = {"exists": None, "body_sample": ""}
        cache[origin] = sitemap_info

    url_listed = None
    if sitemap_info.get("exists"):
        url_listed = url in (sitemap_info.get("body_sample") or "")
    return {"sitemap_exists": sitemap_info.get("exists"), "url_listed_in_sitemap": url_listed}


async def check_seo_indexability(page, context, url: str, response, robots_cache: dict, sitemap_cache: dict):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    meta_robots = await page.evaluate(
        """() => {
            const m = document.querySelector('meta[name="robots" i]');
            return m ? m.getAttribute('content') : null;
        }"""
    )
    canonical = await page.evaluate(
        """() => {
            const l = document.querySelector('link[rel="canonical" i]');
            return l ? l.href : null;
        }"""
    )
    x_robots_tag = None
    if response is not None:
        try:
            headers = await response.all_headers()
            x_robots_tag = headers.get("x-robots-tag")
        except Exception:
            x_robots_tag = None

    robots_rules = await get_robots_txt_rules(context, origin, robots_cache)
    disallow_reason = None
    if robots_rules.get("disallow_all"):
        disallow_reason = "robots.txt zakazuje ÚPLNĚ VŠECHNO ('Disallow: /')"
    else:
        matched = _path_is_disallowed(parsed.path or "/", robots_rules.get("disallow_paths", []))
        if matched:
            disallow_reason = f"robots.txt zakazuje cestu '{matched}'"

    sitemap_info = await check_sitemap(context, origin, url, sitemap_cache)

    blocked_reasons = []
    meta_noindex = bool(meta_robots and "noindex" in meta_robots.lower())
    header_noindex = bool(x_robots_tag and "noindex" in x_robots_tag.lower())
    if meta_noindex:
        blocked_reasons.append(f"meta robots obsahuje 'noindex' ({meta_robots})")
    if header_noindex:
        blocked_reasons.append(f"HTTP hlavička X-Robots-Tag obsahuje 'noindex' ({x_robots_tag})")
    if disallow_reason:
        blocked_reasons.append(disallow_reason)

    canonical_mismatch = bool(canonical and canonical.rstrip("/") != url.rstrip("/"))

    return {
        "meta_robots": meta_robots,
        "x_robots_tag": x_robots_tag,
        "canonical_url": canonical,
        "canonical_mismatch": canonical_mismatch,
        "robots_txt_exists": robots_rules.get("exists"),
        "sitemap_exists": sitemap_info.get("sitemap_exists"),
        "url_listed_in_sitemap": sitemap_info.get("url_listed_in_sitemap"),
        "blocked_reasons": blocked_reasons,
        "indexable": len(blocked_reasons) == 0,
    }
