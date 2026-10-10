#!/usr/bin/env python3
"""
Denní kontrola funkčnosti webů (webzen2026/webtest2026).

Pro každou stránku v sites.json:
  - ověří HTTP status a čas načtení
  - zachytí JS/konzolové chyby
  - zkontroluje odkazy na stránce (rozbité = non-2xx/3xx), s výjimkou
    donate/platba odkazů a nastavených externích domén
  - zkontroluje PWA manifest + registraci service workeru (pokud existuje)
  - provede obecný smoke-test interaktivních prvků (tlačítka/formuláře),
    kromě těch, co vypadají jako donate/platba
  - u vybraných webů spustí hlubší custom modul (viz custom_checks.py)

Výstup: results/latest.json + index.html (report) + history.jsonl (log běhů)
"""
import asyncio
import json
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

from custom_checks import CUSTOM_MODULES, AUDIO_HOOK_INIT_SCRIPT
from seo_check import check_seo_indexability

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "sites.json"
OUT_DIR = ROOT  # index.html se publikuje z rootu repa (GitHub Pages)
RESULTS_DIR = ROOT / "results"
HISTORY_PATH = ROOT / "history.jsonl"

NAV_TIMEOUT_MS = 30000
MAX_LINKS_TO_CHECK = 40
MAX_INTERACTIVE_TO_CLICK = 8
LINK_CHECK_TIMEOUT_MS = 8000

# Tyhle hlášky se v konzoli objevují prakticky VŽDY při automatizovaném
# (headless, bez skutečného uživatele) testování - prohlížeč z principu
# odmítne Storage Access / Vibration API bez "user gesture", i na 100%
# funkčním webu. Nejsou to skutečné chyby webu, tak se nezapočítávají.
CONSOLE_NOISE_KEYWORDS = [
    "requeststorageaccess",
    "storage access",
    "vibrate",
    "vibration api",
]

# Obecná hláška prohlížeče o nedostupném zdroji ("Failed to load resource:
# ... status of 404", "A bad HTTP response code (404) was received when
# fetching the script") - stejnou informaci (a navíc s přesnou URL) teď
# zachytáváme přes sledování síťových odpovědí (viz failed_resources níže),
# takže se tahle obecná verze do konzolových chyb nepočítá (byla by to
# duplicita bez přidané hodnoty).
RESOURCE_FAILURE_MSG_RE = re.compile(
    r"failed to load resource: the server responded with a status of \d+"
    r"|bad http response code \(\d+\) was received when fetching",
    re.IGNORECASE,
)

# Domény třetích stran, co běžně vrací chybu automatizovanému/headless testu
# (anti-bot ochrana, reCAPTCHA ověření, detekce robotů u analytiky) - reálným
# lidským návštěvníkům fungují normálně, takže se nepočítají jako "Pozor".
NOISE_RESOURCE_DOMAIN_KEYWORDS = [
    "firebaseappcheck.googleapis.com",  # Firebase App Check + reCAPTCHA Enterprise ověření "je tohle bot?"
    "goatcounter.com",  # GoatCounter analytika - má vlastní detekci automatizovaných testů
]


def is_noise_console_error(msg: str) -> bool:
    low = (msg or "").lower()
    return any(kw in low for kw in CONSOLE_NOISE_KEYWORDS)


def is_resource_failure_message(msg: str) -> bool:
    return bool(RESOURCE_FAILURE_MSG_RE.search(msg or ""))


def is_noise_resource_url(url: str, skip_domains) -> bool:
    low = (url or "").lower()
    if any(kw in low for kw in NOISE_RESOURCE_DOMAIN_KEYWORDS):
        return True
    try:
        netloc = urlparse(url).netloc
    except Exception:  # noqa: BLE001
        return False
    # znovupoužije stejný seznam domén jako kontrola rozbitých odkazů
    # (external_domains_skip_broken_link_check v sites.json) - jde o stejný
    # princip: nedůvěřovat výsledku automatizovaného testu vůči téhle doméně
    return any(dom in netloc for dom in skip_domains)


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def matches_any_keyword(text: str, keywords) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(kw.lower() in low for kw in keywords)


async def collect_links(page):
    return await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
            href: a.href,
            text: (a.textContent || '').trim().slice(0, 80),
            rel: a.getAttribute('rel') || ''
        }))"""
    )


async def check_broken_links(context, page, links, skip_keywords, skip_domains):
    broken = []
    checked = 0
    seen = set()
    for link in links:
        href = link["href"]
        if href in seen:
            continue
        seen.add(href)
        if checked >= MAX_LINKS_TO_CHECK:
            break
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        parsed = urlparse(href)
        if not parsed.scheme.startswith("http"):
            continue
        if matches_any_keyword(href, skip_keywords) or matches_any_keyword(link["text"], skip_keywords):
            continue
        if any(dom in parsed.netloc for dom in skip_domains):
            continue
        checked += 1
        try:
            resp = await context.request.head(href, timeout=LINK_CHECK_TIMEOUT_MS)
            status = resp.status
            if status == 405 or status >= 400:
                resp = await context.request.get(href, timeout=LINK_CHECK_TIMEOUT_MS)
                status = resp.status
        except Exception as e:  # noqa: BLE001
            broken.append({"href": href, "text": link["text"], "error": str(e)[:200]})
            continue
        if status >= 400:
            broken.append({"href": href, "text": link["text"], "status": status})
    return broken, checked


async def generic_interactive_smoke_test(page, skip_keywords):
    els = await page.evaluate(
        """(skipWords) => {
            const nodes = Array.from(document.querySelectorAll(
                "button, [role=button], input[type=submit], input[type=button]"
            ));
            const isSkippable = (el) => {
                const txt = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') +
                    ' ' + (el.id || '') + ' ' + (el.className || '')).toLowerCase();
                return skipWords.some(w => txt.includes(w.toLowerCase()));
            };
            return nodes
                .filter(el => !isSkippable(el))
                .slice(0, 30)
                .map((el, i) => ({ index: i, text: (el.textContent||'').trim().slice(0,40) }));
        }""",
        skip_keywords,
    )
    tested = []
    all_selectors = "button, [role=button], input[type=submit], input[type=button]"
    locator_all = page.locator(all_selectors)
    total = await locator_all.count()
    clicked_count = 0
    for i in range(total):
        if clicked_count >= MAX_INTERACTIVE_TO_CLICK:
            break
        el = locator_all.nth(i)
        try:
            text = (await el.inner_text()).strip()
        except Exception:
            text = ""
        try:
            aria = await el.get_attribute("aria-label") or ""
        except Exception:
            aria = ""
        combined = f"{text} {aria}"
        if matches_any_keyword(combined, skip_keywords):
            continue
        try:
            if not await el.is_visible():
                continue
            await el.click(timeout=2000, trial=False)
            clicked_count += 1
            tested.append({"text": text[:40] or aria[:40] or f"element#{i}", "clicked": True})
            await page.wait_for_timeout(300)
        except Exception as e:  # noqa: BLE001
            tested.append({"text": text[:40] or f"element#{i}", "clicked": False, "error": str(e)[:120]})
    return tested


async def check_pwa(page):
    manifest_href = await page.evaluate(
        "() => { const l = document.querySelector('link[rel=manifest]'); return l ? l.href : null; }"
    )
    sw_supported = await page.evaluate("() => 'serviceWorker' in navigator")
    sw_registrations = []
    if sw_supported:
        try:
            await page.wait_for_timeout(1000)
            sw_registrations = await page.evaluate(
                """async () => {
                    try {
                        const regs = await navigator.serviceWorker.getRegistrations();
                        return regs.map(r => r.active ? r.active.state : 'no-active-worker');
                    } catch (e) { return ['error:' + e]; }
                }"""
            )
        except Exception:
            sw_registrations = []
    manifest_ok = None
    if manifest_href:
        try:
            resp = await page.request.get(manifest_href, timeout=LINK_CHECK_TIMEOUT_MS)
            manifest_ok = resp.status < 400
        except Exception:
            manifest_ok = False
    return {
        "manifest_url": manifest_href,
        "manifest_ok": manifest_ok,
        "service_worker_supported": sw_supported,
        "service_worker_registrations": sw_registrations,
    }


async def check_one_site(browser, site, skip_keywords, skip_domains, robots_cache, sitemap_cache):
    name = site["name"]
    url = site["url"]
    module = site.get("module", "generic")
    result = {
        "name": name,
        "url": url,
        "module": module,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status_code": None,
        "load_time_ms": None,
        "console_errors": [],
        "failed_resources": [],
        "broken_links": [],
        "links_checked": 0,
        "pwa": None,
        "interactive": [],
        "custom": None,
        "seo": None,
        "overall": "fail",
        "notes": [],
    }
    context = await browser.new_context(ignore_https_errors=True)
    await context.add_init_script(AUDIO_HOOK_INIT_SCRIPT)
    page = await context.new_page()
    console_errors = []
    failed_resources = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: console_errors.append(f"pageerror: {exc}"))

    def _on_response(response):
        # Zachytí URL a status KAŽDÉHO zdroje, co server vrátil s chybou
        # (4xx/5xx) - konzolová hláška typu "Failed to load resource: ...
        # status of 404" sama o sobě neříká KTERÝ soubor to byl, tohle ano.
        try:
            if response.status >= 400:
                failed_resources.append({"url": response.url, "status": response.status})
        except Exception:  # noqa: BLE001
            pass

    page.on("response", _on_response)
    try:
        start = time.monotonic()
        resp = await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        elapsed = int((time.monotonic() - start) * 1000)
        result["status_code"] = resp.status if resp else None
        result["load_time_ms"] = elapsed

        links = await collect_links(page)
        broken, checked = await check_broken_links(context, page, links, skip_keywords, skip_domains)
        result["broken_links"] = broken
        result["links_checked"] = checked

        result["pwa"] = await check_pwa(page)

        try:
            result["seo"] = await check_seo_indexability(
                page, context, url, resp, robots_cache, sitemap_cache
            )
        except Exception as e:  # noqa: BLE001
            result["seo"] = {"error": str(e)[:300], "indexable": None, "blocked_reasons": []}

        result["interactive"] = await generic_interactive_smoke_test(page, skip_keywords)

        custom_fn = CUSTOM_MODULES.get(module)
        if custom_fn:
            try:
                result["custom"] = await custom_fn(page, context)
            except Exception as e:  # noqa: BLE001
                result["custom"] = {"tested": False, "error": str(e)[:300]}

        # rozděl konzolové chyby na: 1) známý šum automatizovaného testu (storage/vibrace),
        # 2) obecné "nedostupný zdroj" hlášky (přesunuté do failed_resources s přesnou URL
        # níže - nemá smysl je počítat dvakrát), 3) skutečné JS chyby (syntax/exceptions)
        noise_console = [e for e in console_errors if is_noise_console_error(e)]
        real_errors = [
            e for e in console_errors
            if not is_noise_console_error(e) and not is_resource_failure_message(e)
        ]
        result["console_errors"] = real_errors[:30]

        # dedupe podle URL, ať se stejný nedostupný zdroj v reportu neopakuje
        seen_urls = set()
        deduped_failed = []
        for fr in failed_resources:
            if fr["url"] in seen_urls:
                continue
            seen_urls.add(fr["url"])
            deduped_failed.append(fr)

        # odfiltruj zdroje třetích stran, co běžně blokují automatizovaný test
        # (reCAPTCHA/App Check ověření bota, analytika s detekcí botů apod.)
        real_failed = [fr for fr in deduped_failed if not is_noise_resource_url(fr["url"], skip_domains)]
        noise_resource_count = len(deduped_failed) - len(real_failed)
        result["failed_resources"] = real_failed[:20]

        if noise_console:
            result["notes"].append(
                f"(mimochodem: {len(noise_console)} hlášek v konzoli ignorováno - běžný šum "
                f"z automatizovaného testu, ne skutečná chyba webu)"
            )
        if noise_resource_count:
            result["notes"].append(
                f"(mimochodem: {noise_resource_count} nedostupných zdrojů ignorováno - "
                f"blokace třetí strany vůči automatizovanému testu (např. Firebase App Check "
                f"nebo GoatCounter analytika s detekcí botů), ne chyba webu)"
            )

        # celkové vyhodnocení - fail má přednost, jinak se sečtou všechny "warn" důvody
        if not resp or resp.status >= 400:
            result["overall"] = "fail"
            result["notes"].append(f"HTTP status {result['status_code']}")
        else:
            warn_reasons = []
            if real_errors:
                warn_reasons.append(f"{len(real_errors)} JS/konzolových chyb")
            if result["failed_resources"]:
                warn_reasons.append(f"{len(result['failed_resources'])} nedostupných zdrojů na stránce")
            if broken:
                warn_reasons.append(f"{len(broken)} rozbitých odkazů")
            pwa = result["pwa"] or {}
            if pwa.get("manifest_url") and pwa.get("manifest_ok") is False:
                warn_reasons.append("PWA manifest se nepodařilo načíst (rozbitá cesta k manifest.json)")
            custom = result["custom"]
            if custom and custom.get("tested") is False:
                warn_reasons.append("custom test nenašel očekávaný prvek (zkontroluj selektory)")
            elif custom and custom.get("tested") and custom.get("success_heuristic") is False:
                warn_reasons.append("custom test proběhl, ale výsledek nevypadá jako úspěch")
            # Pozn.: indexovatelnost (noindex/robots.txt) se NEZAPOČÍTÁVÁ do Pozor stavu -
            # spousta stránek má noindex nastavené úmyslně (interní stránky apod.), takže
            # by to jen dělalo falešné poplachy. Info se pořád zobrazí v detailu karty.

            if warn_reasons:
                result["overall"] = "warn"
                result["notes"].extend(warn_reasons)
            else:
                result["overall"] = "ok"
    except Exception as e:  # noqa: BLE001
        result["overall"] = "fail"
        result["notes"].append(f"chyba při načítání: {e}")
        result["error_trace"] = traceback.format_exc()[-2000:]
    finally:
        await context.close()
    return result


async def run_all():
    config = load_config()
    skip_keywords = config.get("skip_link_keywords", [])
    skip_domains = config.get("external_domains_skip_broken_link_check", [])
    sites = [s for s in config["sites"] if not s.get("skip")]

    results = []
    robots_cache = {}  # cache podle originu (schema://host) - robots.txt se stahuje jen jednou za běh
    sitemap_cache = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for site in sites:
            print(f"-> kontroluji {site['name']} ({site['url']})")
            res = await check_one_site(
                browser, site, skip_keywords, skip_domains, robots_cache, sitemap_cache
            )
            print(f"   výsledek: {res['overall']}")
            results.append(res)
        await browser.close()
    return results


def summarize(results):
    ok = sum(1 for r in results if r["overall"] == "ok")
    warn = sum(1 for r in results if r["overall"] == "warn")
    fail = sum(1 for r in results if r["overall"] == "fail")
    return {"ok": ok, "warn": warn, "fail": fail, "total": len(results)}


def save_results(results):
    RESULTS_DIR.mkdir(exist_ok=True)
    run_time = datetime.now(timezone.utc)
    payload = {
        "run_at": run_time.isoformat(),
        "summary": summarize(results),
        "results": results,
    }
    (RESULTS_DIR / "latest.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Pozn.: schválně se neukládá samostatný soubor pro každý běh (repo by
    # postupem času narostlo) - latest.json vždy jen přepíšeme a lehká
    # historie (jen souhrn, ne celý detail) jde do history.jsonl.
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "run_at": run_time.isoformat(),
            "summary": payload["summary"],
        }, ensure_ascii=False) + "\n")
    return payload


def main():
    results = asyncio.run(run_all())
    payload = save_results(results)
    from report import render_report  # local import, viz report.py
    html = render_report(payload, HISTORY_PATH)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")
    print("\nHotovo:", payload["summary"])
    # exit code != 0 když něco selhalo, ať to jde vidět i v GitHub Actions logu
    if payload["summary"]["fail"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
