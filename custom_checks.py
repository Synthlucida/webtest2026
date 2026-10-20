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
  // Zapamatuje si každý <audio>/<video> (i vytvořený přes new Audio()), na kterém
  // se zavolalo play() - player nemusí používat Web Audio, ale obyčejný <audio>.
  window.__testMedia = [];
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function(...args) {
    if (window.__testMedia.indexOf(this) === -1) window.__testMedia.push(this);
    return origPlay.apply(this, args);
  };
})();
"""

MEDIA_STATE_JS = """
() => {
  const all = new Set([...(window.__testMedia || []), ...document.querySelectorAll('audio,video')]);
  return [...all].map(m => ({
    paused: m.paused, currentTime: Math.round(m.currentTime * 10) / 10,
    readyState: m.readyState, error: m.error ? m.error.code : null,
    src: (m.currentSrc || m.src || '').slice(-60),
  }));
}
"""

PLAY_BUTTON_CANDIDATES = [
    "#btn-play",  # hlavní Play/Pause tlačítko v player.html (SVG ikona, aria-label="Play")
    "button.btn-ctrl[aria-label='Play' i]",
    "button:has-text('▶')",
    "text=▶",
    # POZOR: ne "*=play" - to trefí i "Show the PLAYing track", "Back to the PLAYing MY PLAYLIST"
    "[aria-label='Play' i]",
    "[aria-label='hrát' i]",
    "[aria-label='přehrát' i]",
    "[title='Play' i]",
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


# Řádky se skladbami v seznamu - player bez vybrané skladby ("SELECT TRACK") nic
# nepřehraje, takže když samotné Play nezabere, test klikne nejdřív na skladbu.
TRACK_CANDIDATES = [
    "[data-src]", "[data-track]", "[data-file]", "[data-index]",
    "[class*='track' i]", "[class*='song' i]",
    "[id*='playlist' i] li", "[id*='playlist' i] > div", "[id*='list' i] li",
    "li[onclick]", "[class*='item' i]",
]


async def _is_safe_to_click(loc):
    """Neklikat na odkazy vedoucí pryč ze stránky (donate, obchod apod.)."""
    try:
        return await loc.evaluate("e => !e.closest('a[href]') && !!(e.offsetWidth || e.offsetHeight)")
    except Exception:
        return False


async def _evidence(page, time_before, label):
    """Počká a zjistí, jestli se opravdu přehrává. Vrací dict s důkazem, nebo None."""
    await page.wait_for_timeout(3500)
    audio_states = await page.evaluate(
        "() => window.__testAudioContexts ? window.__testAudioContexts.map(c => c.state) : []"
    )
    audio_playing = any(s == "running" for s in audio_states)
    media = await page.evaluate(MEDIA_STATE_JS)
    media_playing = any((not m["paused"]) and m["currentTime"] > 0 for m in media)
    time_after = await _grab_time_tokens(page)
    timer_advanced = bool(time_before) and time_after != time_before
    result = {
        "clicked_selector": label,
        "audio_context_states": audio_states,
        "audio_context_running": audio_playing,
        "media_elements": media,
        "media_playing": media_playing,
        "timer_before": time_before[:3],
        "timer_after": time_after[:3],
        "timer_advanced": timer_advanced,
    }
    return (result if (audio_playing or media_playing or timer_advanced) else None), result


async def _try_play_buttons(page, tried):
    for sel in PLAY_BUTTON_CANDIDATES:
        try:
            matches = page.locator(sel)
            n = min(await matches.count(), 5)
        except Exception:
            continue
        for i in range(n):
            loc = matches.nth(i)
            try:
                if not await loc.is_visible():
                    continue
                time_before = await _grab_time_tokens(page)
                await loc.click(timeout=3000)
            except Exception:
                continue
            label = f"{sel} (#{i})"
            tried.append(label)
            ok, last = await _evidence(page, time_before, label)
            if ok:
                return ok, last
    return None, None


async def _dump_candidates(page, selectors, limit=3):
    out = {}
    for sel in selectors:
        try:
            matches = page.locator(sel)
            cnt = await matches.count()
            if cnt:
                out[sel] = {
                    "count": cnt,
                    "first_html": [
                        (await matches.nth(i).evaluate("e => e.outerHTML"))[:220]
                        for i in range(min(cnt, limit))
                    ],
                }
        except Exception:
            continue
    return out


async def check_relaxplayer(page, context):
    """Ověří, že player skutečně přehrává.

    Důkazy (stačí jeden): AudioContext 'running', <audio> element co se přehrává
    (currentTime roste) nebo pohyb časovače MM:SS.
    Postup: 1) zkusí Play tlačítka, 2) když nic nehraje (typicky není vybraná
    skladba - "SELECT TRACK"), klikne na skladbu v seznamu a případně pak znovu
    na Play. Při neúspěchu přidá do reportu HTML kandidátů pro snadné doladění.
    """
    tried = []

    ok, last = await _try_play_buttons(page, tried)
    if ok:
        return {"tested": True, **ok, "success_heuristic": True, "note": None}

    for sel in TRACK_CANDIDATES:
        try:
            matches = page.locator(sel)
            n = min(await matches.count(), 3)
        except Exception:
            continue
        for i in range(n):
            loc = matches.nth(i)
            try:
                if not await _is_safe_to_click(loc):
                    continue
                time_before = await _grab_time_tokens(page)
                await loc.click(timeout=3000)
            except Exception:
                continue
            label = f"skladba: {sel} (#{i})"
            tried.append(label)
            ok, last = await _evidence(page, time_before, label)
            if ok:
                return {"tested": True, **ok, "success_heuristic": True, "note": None}
            # skladba možná jen vybrána - zkusit po ní ještě Play
            ok, last2 = await _try_play_buttons(page, tried)
            if ok:
                ok["clicked_selector"] = f"{label} + {ok['clicked_selector']}"
                return {"tested": True, **ok, "success_heuristic": True, "note": None}
            if last2:
                last = last2

    if not tried:
        return {
            "tested": False,
            "reason": "Play tlačítko ani skladbu nenalezeno žádným z kandidátních selektorů - "
                      "uprav PLAY_BUTTON_CANDIDATES / TRACK_CANDIDATES v custom_checks.py.",
        }
    return {
        "tested": True,
        "tried_selectors": tried,
        "last_attempt": last,
        "diagnostika_play": await _dump_candidates(page, PLAY_BUTTON_CANDIDATES[:8]),
        "diagnostika_skladby": await _dump_candidates(page, TRACK_CANDIDATES),
        "success_heuristic": False,
        "note": "Žádný z pokusů (Play ani výběr skladby) nespustil přehrávání: " + ", ".join(tried),
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
