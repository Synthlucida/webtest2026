"""
Hlubší (custom) testy pro konkrétní appky, kde obecný smoke-test nestačí.

Aktualizováno podle skutečné struktury tvých stránek (zjištěno z prvního
reálného běhu na GitHub Actions + průzkumu stránek):
  - RelaxPlayer (player.html): play tlačítko je jen symbol "▶" bez textu,
    vedle něj je časovač "00:00 / 00:00" - kromě AudioContext stavu proto
    navíc kontrolujeme, jestli se časovač po kliknutí pohnul (silnější
    důkaz přehrávání, funguje i kdyby web nepoužíval Web Audio API).
  - WEBZENith (webzen-en.html): NEMÁ tlačítko "Generate" - je to formulář
    s živým náhledem (co napíšeš, hned se to promítne do preview) a pak
    tlačítka "Download HTML" / "Download entire site". Test proto vyplní
    pole unikátním textem a ověří, že se objevil v náhledu (případně že
    existuje Download tlačítko).
"""

import re
import time

# Vloží se do KAŽDÉ stránky ještě před tím, než se spustí její vlastní JS.
# "Obalí" AudioContext tak, aby si skript pamatoval všechny vytvořené
# instance - díky tomu jde po kliknutí na Play ověřit, jestli se zvuk
# opravdu spustil (AudioContext.state === "running"), bez znalosti
# vnitřní implementace stránky.
AUDIO_HOOK_INIT_SCRIPT = """
(() => {
  window.__testAudioContexts = [];
  const Orig = window.AudioContext || window.webkitAudioContext;
  if (Orig) {
    const Wrapped = function(...args) {
      const inst = new Orig(...args);
      window.__testAudioContexts.push(inst);
      return inst;
    };
    Wrapped.prototype = Orig.prototype;
    window.AudioContext = Wrapped;
    window.webkitAudioContext = Wrapped;
  }
})();
"""

PLAY_BUTTON_CANDIDATES = [
    "button:has-text('▶')",
    "text=▶",
    "[aria-label*='play' i]",
    "[aria-label*='hrát' i]",
    "[aria-label*='přehrát' i]",
    "[title*='play' i]",
    "button:has-text('Play')",
    "button:has-text('Přehrát')",
    "button:has-text('Start')",
    "#play", "#playBtn", "#play-btn", "#startBtn",
    ".play-button", ".play-btn", ".playBtn",
    "button[class*='play' i]",
    "[id*='play' i]",
    "button[id*='start' i]",
]

TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}\b")


async def _click_first_match(page, selectors, timeout=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=timeout)
                return sel
        except Exception:
            continue
    return None


async def _grab_time_tokens(page):
    try:
        text = await page.inner_text("body")
    except Exception:
        return []
    return TIME_PATTERN.findall(text or "")


async def check_relaxplayer(page, context):
    """Reálně klikne na Play a ověří, že se spustilo přehrávání.

    Dva nezávislé důkazy (stačí jeden): 1) AudioContext přejde do stavu
    'running', 2) časovač na stránce (formát MM:SS) se po pár sekundách
    změní oproti stavu před kliknutím.
    """
    time_tokens_before = await _grab_time_tokens(page)

    clicked = await _click_first_match(page, PLAY_BUTTON_CANDIDATES)
    if not clicked:
        return {
            "tested": False,
            "reason": "Play tlačítko nenalezeno žádným z kandidátních selektorů - "
                      "uprav PLAY_BUTTON_CANDIDATES v custom_checks.py podle skutečného HTML.",
        }

    await page.wait_for_timeout(3500)

    audio_states = await page.evaluate(
        "() => window.__testAudioContexts ? window.__testAudioContexts.map(c => c.state) : []"
    )
    audio_playing = any(s == "running" for s in audio_states)

    time_tokens_after = await _grab_time_tokens(page)
    timer_advanced = bool(time_tokens_before) and time_tokens_after != time_tokens_before

    playing = audio_playing or timer_advanced
    return {
        "tested": True,
        "clicked_selector": clicked,
        "audio_context_states": audio_states,
        "audio_context_running": audio_playing,
        "timer_before": time_tokens_before[:3],
        "timer_after": time_tokens_after[:3],
        "timer_advanced": timer_advanced,
        "success_heuristic": playing,
        "note": None if playing else (
            "Tlačítko se podařilo kliknout, ale nenašel jsem důkaz přehrávání (ani AudioContext "
            "running, ani pohyb časovače). Možné důvody: přehrávání vyžaduje druhé kliknutí kvůli "
            "autoplay policy prohlížeče, nebo stránka nemá žádný viditelný časovač ve formátu MM:SS."
        ),
    }


async def check_webzenith_generator(page, context):
    """WEBZENith nemá tlačítko 'Generate' - má formulář s ŽIVÝM náhledem.

    Vyplní textová pole unikátním textem a ověří, že se objevil v náhledu
    (i uvnitř případného iframe). Jako druhý (nepovinný) důkaz zkusí najít
    tlačítko Download HTML / Download entire site.
    """
    marker = "ClaudeTest" + str(int(time.time()))[-5:]

    inputs = page.locator("input[type='text'], input:not([type]), textarea")
    count = await inputs.count()
    filled = 0
    for i in range(min(count, 8)):
        el = inputs.nth(i)
        try:
            if await el.is_visible():
                value = marker if filled == 0 else f"{marker}-{filled}"
                await el.fill(value)
                filled += 1
                await page.wait_for_timeout(250)  # dá živému náhledu čas se překreslit
        except Exception:
            continue

    if filled == 0:
        return {
            "tested": False,
            "reason": "Nenašel jsem žádné vyplnitelné textové pole na stránce.",
        }

    await page.wait_for_timeout(800)

    marker_found = await page.evaluate(
        """(marker) => {
            if (document.body.innerText.includes(marker)) return true;
            const iframes = Array.from(document.querySelectorAll('iframe'));
            for (const f of iframes) {
                try {
                    if (f.contentDocument && f.contentDocument.body &&
                        f.contentDocument.body.innerText.includes(marker)) return true;
                } catch (e) { /* cross-origin iframe, nelze číst */ }
            }
            return false;
        }""",
        marker,
    )

    download_selectors = [
        "text=Download HTML", "text=Download entire site",
        "button:has-text('Download')", "a:has-text('Download')",
        "[class*=download i]", "[id*=download i]",
    ]
    download_found = False
    download_selector = None
    for sel in download_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                download_found = True
                download_selector = sel
                break
        except Exception:
            continue

    success = marker_found or download_found
    return {
        "tested": True,
        "inputs_filled": filled,
        "marker_text_used": marker,
        "marker_reflected_in_preview": marker_found,
        "download_control_found": download_found,
        "download_selector": download_selector,
        "success_heuristic": success,
        "note": None if success else (
            "Vyplněný text se neobjevil v náhledu a nenašel jsem ani tlačítko Download. "
            "Zkontroluj ručně, jestli živý náhled ve WEBZENithu funguje - heuristika ho "
            "možná hledá na špatném místě (např. uvnitř iframe s jiným originem, který "
            "z bezpečnostních důvodů nejde z JS přečíst)."
        ),
    }


CUSTOM_MODULES = {
    "relaxplayer": check_relaxplayer,
    "webzenith_generator": check_webzenith_generator,
}
