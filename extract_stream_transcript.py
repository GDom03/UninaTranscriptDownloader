#!/usr/bin/env python3
"""
Estrae la trascrizione da una registrazione Microsoft Stream/SharePoint.

Output:
  trascrizione.json
  trascrizione.txt
  trascrizione.vtt

Caratteristiche:
- usa Playwright + Chromium con profilo persistente;
- non richiede username/password allo script;
- al primo avvio apre una finestra Chromium e permette il login Microsoft
  manuale, poi riusa la sessione salvata nel profilo locale;
- intercetta la richiesta/risposta HTTP del transcript di Stream;
- se non riesce, legge l'endpoint "temporaryDownloadUrl" presente nella pagina;
- se anche l'API è bloccata, prova il fallback della lista transcript
  virtualizzata nel DOM.

Uso:
  python extract_stream_transcript.py
  python extract_stream_transcript.py --url "https://..."
  python extract_stream_transcript.py --profile-dir ".pw-sharepoint-profile"
  python extract_stream_transcript.py --out-dir "output"

Installazione:
  python -m pip install -r requirements.txt
  python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
import os

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Response, Page, BrowserContext

load_dotenv()


DEFAULT_URL = os.environ.get("STREAM_URL", "")

TRANSCRIPT_PATH_MARKER = "/media/transcripts/"

# ISO-8601 duration, ad esempio PT1H2M3.250S / PT12.4S / PT0S
ISO_DURATION_RE = re.compile(
    r"^PT"
    r"(?:(?P<h>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<m>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)S)?$",
    re.I,
)


@dataclass
class Entry:
    index: int
    start_seconds: Optional[float]
    end_seconds: Optional[float]
    speaker: Optional[str]
    text: str
    start_offset: Optional[str] = None
    end_offset: Optional[str] = None


def iso_to_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if not isinstance(value, str):
        return None

    m = ISO_DURATION_RE.match(value.strip())
    if not m:
        return None

    return (
        float(m.group("h") or 0) * 3600.0
        + float(m.group("m") or 0) * 60.0
        + float(m.group("s") or 0)
    )


def seconds_to_timestamp(seconds: Optional[float]) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)

    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_transcript(
    raw: Any,
    *,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Normalizza il formato Teams transcript.json in:
      { source: {...}, entries: [...] }

    Mantiene comunque il raw object dentro _raw_response per non perdere dati.
    """
    metadata = metadata or {}

    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        raw_entries = raw["entries"]
    elif isinstance(raw, list):
        raw_entries = raw
    else:
        raw_entries = []

    entries: list[Entry] = []

    for idx, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            continue

        speaker = (
            item.get("speakerDisplayName")
            or item.get("speaker")
            or item.get("speakerId")
            or item.get("displayName")
        )

        start_offset = item.get("startOffset")
        end_offset = item.get("endOffset")

        text_value = (
            item.get("text")
            if item.get("text") is not None
            else item.get("content", "")
        )

        entries.append(
            Entry(
                index=idx,
                start_seconds=iso_to_seconds(start_offset),
                end_seconds=iso_to_seconds(end_offset),
                speaker=clean_text(speaker) or None,
                text=clean_text(text_value),
                start_offset=start_offset,
                end_offset=end_offset,
            )
        )

    # Alcune versioni/strutture possono avere entry non ordinate.
    entries.sort(
        key=lambda x: (
            x.start_seconds is None,
            x.start_seconds if x.start_seconds is not None else float("inf"),
            x.index,
        )
    )

    # Reindicizza.
    for i, entry in enumerate(entries):
        entry.index = i

    # Se mancano gli endOffset, usa l'inizio della voce successiva.
    # Per l'ultima voce usa +2s come durata minima di visualizzazione.
    for i, entry in enumerate(entries):
        if entry.end_seconds is None:
            next_start = (
                entries[i + 1].start_seconds if i + 1 < len(entries) else None
            )
            if next_start is not None and entry.start_seconds is not None:
                entry.end_seconds = max(
                    entry.start_seconds + 0.25, next_start
                )
            elif entry.start_seconds is not None:
                entry.end_seconds = entry.start_seconds + 2.0

    return {
        "format": "teams-transcript-normalized",
        "source": metadata,
        "entry_count": len(entries),
        "entries": [asdict(e) for e in entries],
        "_raw_response": raw,
    }


def transcript_to_txt(data: dict[str, Any]) -> str:
    lines: list[str] = []

    for e in data.get("entries", []):
        start = seconds_to_timestamp(e.get("start_seconds"))
        speaker = e.get("speaker") or "?"
        text = e.get("text", "")
        lines.append(f"[{start}] [{speaker}] {text}")

    return "\n".join(lines) + ("\n" if lines else "")


def transcript_to_vtt(data: dict[str, Any]) -> str:
    lines = ["WEBVTT", ""]

    for i, e in enumerate(data.get("entries", []), start=1):
        start = seconds_to_timestamp(e.get("start_seconds"))
        end_sec = e.get("end_seconds")

        if end_sec is None:
            start_sec = float(e.get("start_seconds") or 0.0)
            end_sec = start_sec + 2.0

        end = seconds_to_timestamp(end_sec)
        speaker = e.get("speaker")
        text = e.get("text", "")

        if speaker:
            cue_text = f"<v {html.escape(str(speaker), quote=True)}>{html.escape(text)}</v>"
        else:
            cue_text = html.escape(text)

        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(cue_text)
        lines.append("")

    return "\n".join(lines)


def write_outputs(data: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "trascrizione.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    (out_dir / "trascrizione.txt").write_text(
        transcript_to_txt(data),
        encoding="utf-8",
    )

    (out_dir / "trascrizione.vtt").write_text(
        transcript_to_vtt(data),
        encoding="utf-8",
    )


async def extract_metadata(page: Page) -> Optional[dict[str, Any]]:
    """
    Stream inizializza g_extraVideoProperties.Tracks.
    L'oggetto contiene media.transcripts[] e temporaryDownloadUrl.
    """
    script = r"""
    () => {
      try {
        const raw = window.g_extraVideoProperties?.Tracks;
        if (!raw) return null;

        const tracks = typeof raw === "string" ? JSON.parse(raw) : raw;
        const list = tracks?.media?.transcripts;

        if (!Array.isArray(list) || !list.length) return null;

        const t = list.find(x => x.isDefault) || list[0];

        return {
          id: t?.id ?? null,
          displayName: t?.displayName ?? null,
          languageTag: t?.languageTag ?? null,
          transcriptType: t?.transcriptType ?? null,
          isVisible: t?.isVisible ?? null,
          isAutoGenerated: t?.isAutoGenerated ?? null,
          isDefault: t?.isDefault ?? null,
          source: t?.source ?? null,
          temporaryDownloadUrl: t?.temporaryDownloadUrl ?? null
        };
      } catch (e) {
        return null;
      }
    }
    """
    return await page.evaluate(script)


async def fetch_transcript_from_page(
    page: Page,
    metadata: dict[str, Any],
) -> tuple[Any, str]:
    """
    Esegue fetch direttamente dal contesto della pagina:
    in questo modo vengono usati cookie/local storage/token della sessione
    già autenticata dal browser.
    """
    url = metadata.get("temporaryDownloadUrl")
    if not url:
        raise RuntimeError("temporaryDownloadUrl non presente nei metadati.")

    result = await page.evaluate(
        """
        async ({ url }) => {
          const candidates = [];
          const u = new URL(url);

          // Variante esplicita JSON usata da alcune versioni di Stream.
          {
            const x = new URL(u.href);
            x.searchParams.set("isformatjson", "true");
            candidates.push(x.href);
          }

          // Endpoint originale.
          candidates.push(u.href);

          const seen = new Set();

          for (const candidate of candidates) {
            if (seen.has(candidate)) continue;
            seen.add(candidate);

            try {
              const r = await fetch(candidate, {
                credentials: "include",
                cache: "no-store",
                headers: {
                  "Accept": "application/json, text/plain, */*"
                }
              });

              const body = await r.text();

              if (r.ok) {
                return {
                  ok: true,
                  status: r.status,
                  contentType: r.headers.get("content-type") || "",
                  url: candidate,
                  body
                };
              }
            } catch (e) {
              // prova la candidata successiva
            }
          }

          return {
            ok: false,
            error: "Nessuna variante dell'endpoint transcript ha restituito HTTP 2xx."
          };
        }
        """,
        {"url": url},
    )

    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Fetch transcript fallito."))

    body = result["body"]
    content_type = (result.get("contentType") or "").lower()

    if "webvtt" in content_type or body.lstrip().upper().startswith("WEBVTT"):
        return parse_vtt_to_raw_json(body), result["url"]

    try:
        return json.loads(body), result["url"]
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "La risposta transcript non è né JSON né WebVTT."
        ) from exc


def parse_vtt_to_raw_json(vtt: str) -> dict[str, Any]:
    """
    Parser VTT minimale sufficiente per transcript Stream.
    """
    lines = vtt.replace("\r", "").split("\n")
    out: list[dict[str, Any]] = []

    def parse_ts(value: str) -> Optional[float]:
        value = value.strip().split()[0]
        parts = value.split(":")
        try:
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
            if len(parts) == 2:
                m, s = parts
                return int(m) * 60 + float(s)
        except ValueError:
            return None
        return None

    i = 0
    while i < len(lines):
        if "-->" not in lines[i]:
            i += 1
            continue

        a, b = [x.strip() for x in lines[i].split("-->", 1)]
        start = parse_ts(a)
        end = parse_ts(b)
        i += 1

        cue_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            cue_lines.append(lines[i])
            i += 1

        text = " ".join(cue_lines).strip()
        speaker = None

        m = re.match(r"<v\s+([^>]+)>(.*?)</v>$", text, re.I)
        if m:
            speaker = html.unescape(m.group(1).strip())
            text = html.unescape(m.group(2).strip())
        else:
            text = html.unescape(re.sub(r"<[^>]+>", "", text))

        out.append(
            {
                "speakerDisplayName": speaker,
                "startOffset": None,
                "endOffset": None,
                "startSeconds": start,
                "endSeconds": end,
                "text": text,
            }
        )

    return {"entries": out, "sourceFormat": "webvtt"}


async def collect_virtualized_dom_transcript(page: Page) -> list[dict[str, Any]]:
    """
    Fallback: Stream mostra una lista virtualizzata. Scorriamo lo scroller
    e raccogliamo gli elementi già montati nel DOM, identificandoli tramite
    aria-posinset.
    """
    return await page.evaluate(
        """
        async () => {
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const root = document.querySelector("#OneTranscript") || document.body;

          const firstEntry = root.querySelector("[aria-posinset]");
          if (!firstEntry) throw new Error("Nessuna voce transcript nel DOM. Pannello probabilmente chiuso o struttura cambiata.");

          function isScrollable(el) {
            if (!el) return false;
            const cs = getComputedStyle(el);
            const oy = cs.overflowY;
            return (oy === "auto" || oy === "scroll") &&
                   el.scrollHeight > el.clientHeight + 20;
          }

          let scroller = firstEntry;
          while (scroller && scroller.parentElement) {
            if (isScrollable(scroller)) break;
            scroller = scroller.parentElement;
          }

          if (!scroller || !isScrollable(scroller)) {
            scroller = root;
          }

          const found = new Map();

          function collectVisible() {
            root.querySelectorAll("[aria-posinset]").forEach(el => {
              const textEl = el.querySelector("[class*='entryText']") || el;
              if (!textEl) return;

              const text = (textEl.innerText || "").trim();
              if (!text) return;

              const pos = textEl.getAttribute("aria-posinset")
                       || el.querySelector("[aria-posinset]")?.getAttribute("aria-posinset")
                       || el.getAttribute("aria-posinset")
                       || "";

              let label = "";
              let curr = el;
              while (curr && curr !== document.body) {
                  if (curr.hasAttribute("aria-label")) {
                      label = (curr.getAttribute("aria-label") || "").trim();
                      break;
                  }
                  curr = curr.parentElement;
              }

              const fullText = (el.innerText || "").trim();
              const key = pos ? `pos:${pos}` : `raw:${label}\\u001f${text}`;

              found.set(key, { pos, label, text, fullText });
            });
          }

          scroller.scrollTop = 0;
          await sleep(500);

          let lastTop = -1;
          let stuck = 0;

          for (let round = 0; round < 800; round++) {
            collectVisible();

            const maxTop = Math.max(
              0,
              scroller.scrollHeight - scroller.clientHeight
            );

            const currentTop = scroller.scrollTop;
            const step = Math.max(
              220,
              Math.floor((scroller.clientHeight || 500) * 0.65)
            );

            const nextTop = Math.min(maxTop, currentTop + step);
            scroller.scrollTop = nextTop;

            await sleep(160);

            if (Math.abs(nextTop - lastTop) < 1) {
              stuck++;
              if (stuck > 2) await sleep(600);
            } else {
              stuck = 0;
            }

            lastTop = nextTop;

            if (nextTop >= maxTop - 2 && stuck >= 8) {
              break;
            }
          }

          collectVisible();

          function parseLabel(label, fullText) {
            let speaker = null;
            let seconds = null;
            
            if (label) {
                // Formato: PAOLA FESTA 1 ore 2 minuti 23 secondi (o senza ore)
                let m = label.match(/^(.+?)(?:\\s+(\\d+)\\s+ore?)?\\s+(\\d+)\\s+minuti?\\s+(\\d+)\\s+secondi?/i);
                if (m) {
                    speaker = m[1].trim();
                    seconds = Number(m[3]) * 60 + Number(m[4]);
                    if (m[2]) seconds += Number(m[2]) * 3600;
                    return { speaker, seconds };
                }
                // Formato: PAOLA FESTA a 01:23
                m = label.match(/^(.+?)\\s+a\\s+(\\d+):(\\d+)(?::(\\d+))?/i);
                if (m) {
                    speaker = m[1].trim();
                    if (m[4]) {
                        seconds = Number(m[2]) * 3600 + Number(m[3]) * 60 + Number(m[4]);
                    } else {
                        seconds = Number(m[2]) * 60 + Number(m[3]);
                    }
                    return { speaker, seconds };
                }
            }

            if (fullText) {
                const lines = fullText.split('\\n').map(x => x.trim()).filter(x => x);
                for (let i = 0; i < lines.length; i++) {
                    let tsMatch = lines[i].match(/^(\\d+):(\\d+)(?::(\\d+))?$/);
                    if (tsMatch) {
                        if (tsMatch[3]) {
                            seconds = Number(tsMatch[1]) * 3600 + Number(tsMatch[2]) * 60 + Number(tsMatch[3]);
                        } else {
                            seconds = Number(tsMatch[1]) * 60 + Number(tsMatch[2]);
                        }
                        if (i > 0) {
                            speaker = lines[i-1];
                        }
                        break;
                    }
                }
            }

            return { speaker: speaker || (label ? label : null), seconds };
          }

          const rows = Array.from(found.values()).map((x, i) => {
            const parsed = parseLabel(x.label, x.fullText);
            return {
              domIndex: i,
              pos: x.pos ? Number(x.pos) : null,
              speaker: parsed.speaker,
              startSeconds: parsed.seconds,
              text: x.text
            };
          });

          rows.sort((a, b) => {
            if (a.pos != null && b.pos != null) {
              return a.pos - b.pos;
            }
            if (a.startSeconds != null && b.startSeconds != null) {
              return a.startSeconds - b.startSeconds;
            }
            return a.domIndex - b.domIndex;
          });

          return rows.map((x, i) => ({
            index: i,
            start_seconds: x.startSeconds,
            end_seconds: null,
            speaker: x.speaker,
            text: x.text
          }));
        }
        """
    )


def raw_json_to_entries(data: Any) -> dict[str, Any]:
    # In caso di VTT parsato: converti startSeconds/endSeconds nella struttura comune.
    if (
        isinstance(data, dict)
        and isinstance(data.get("entries"), list)
        and data.get("sourceFormat") == "webvtt"
    ):
        entries: list[dict[str, Any]] = []
        for i, x in enumerate(data["entries"]):
            entries.append(
                {
                    "index": i,
                    "start_seconds": x.get("startSeconds"),
                    "end_seconds": x.get("endSeconds"),
                    "speaker": x.get("speakerDisplayName"),
                    "text": clean_text(x.get("text")),
                    "start_offset": None,
                    "end_offset": None,
                }
            )
        return {
            "format": "teams-transcript-normalized",
            "source": {"sourceFormat": "webvtt"},
            "entry_count": len(entries),
            "entries": entries,
            "_raw_response": data,
        }

    return normalize_transcript(data)


async def auto_login_if_possible(page: Page) -> None:
    email = os.environ.get("MICROSOFT_EMAIL")
    password = os.environ.get("MICROSOFT_PASSWORD")
    if not email or not password:
        return

    try:
        email_input = page.locator('input[type="email"]')
        if await email_input.is_visible():
            val = await email_input.input_value()
            if not val:
                print("[auth] Auto-compilazione email...")
                await email_input.fill(email)
                await page.locator('input[type="submit"], button[type="submit"], #idSIButton9').first.click()
                await page.wait_for_timeout(2000)

        password_input = page.locator('input[type="password"]')
        if await password_input.is_visible():
            val = await password_input.input_value()
            if not val:
                print("[auth] Auto-compilazione password...")
                await password_input.fill(password)
                await page.locator('input[type="submit"], button[type="submit"], #idSIButton9, #submitButton').first.click()
                await page.wait_for_timeout(2000)
                
        stay_signed_in = page.locator('input[type="submit"], button[type="submit"], #idSIButton9')
        if await stay_signed_in.is_visible():
            # Clicca per andare avanti su "Rimani collegato?"
            btn = stay_signed_in.first
            if await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(1000)
    except Exception:
        pass


async def wait_for_auth_and_page(page: Page, timeout_ms: int) -> None:
    """
    Attende che SharePoint sia realmente caricata.
    Al primo avvio può apparire la pagina di login Microsoft.
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
    last_url = ""

    while asyncio.get_event_loop().time() < deadline:
        current_url = page.url
        if current_url != last_url:
            print(f"[browser] URL: {current_url}")
            last_url = current_url

        host = urlparse(current_url).netloc.lower()

        # Microsoft login / AAD / account picker.
        login_like = any(
            x in host
            for x in (
                "login.microsoftonline.com",
                "login.microsoft.com",
                "account.microsoft.com",
            )
        ) or "adfs" in current_url.lower() or "login" in host

        if login_like:
            if os.environ.get("MICROSOFT_EMAIL") and os.environ.get("MICROSOFT_PASSWORD"):
                await auto_login_if_possible(page)
            else:
                print(
                    "[auth] Completa il login Microsoft nella finestra Chromium. "
                    "Se vuoi l'auto-login, inserisci MICROSOFT_EMAIL e MICROSOFT_PASSWORD nel file .env"
                )
        else:
            # Una volta rientrati su SharePoint, prosegui.
            if "sharepoint.com" in host:
                # Lasciamo un po' di tempo a Stream per inizializzare transcript.
                await page.wait_for_timeout(2500)
                return

        await page.wait_for_timeout(750)

    raise TimeoutError(
        "Timeout durante l'autenticazione/caricamento della pagina."
    )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Estrae transcript JSON/TXT/VTT da Microsoft Stream."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="URL Stream della registrazione",
    )
    parser.add_argument(
        "--profile-dir",
        default=".pw-sharepoint-profile",
        help="Directory del profilo Chromium persistente",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="Directory degli output",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=15,
        help="Tempo massimo per login/caricamento (default: 15)",
    )
    args = parser.parse_args()

    profile_dir = Path(args.profile_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()

    print("=" * 72)
    print("Microsoft Stream / SharePoint transcript extractor")
    print("=" * 72)
    print(f"URL        : {args.url}")
    print(f"Profilo    : {profile_dir}")
    print(f"Output     : {out_dir}")
    print()

    if not args.url:
        print("ERRORE: Nessun URL fornito.", file=sys.stderr)
        print("Devi passare un URL tramite --url oppure impostare STREAM_URL nel file .env", file=sys.stderr)
        return 1

    captured: dict[str, Any] = {}
    capture_event = asyncio.Event()

    async def handle_response(response: Response) -> None:
        url = response.url
        if TRANSCRIPT_PATH_MARKER not in url:
            return

        # Evitiamo di catturare endpoint metadata/cached non definitivi se possibile.
        if response.status != 200:
            return

        content_type = (response.headers.get("content-type") or "").lower()
        if (
            "json" not in content_type
            and "text" not in content_type
            and "webvtt" not in content_type
        ):
            return

        try:
            body = await response.text()
        except Exception:
            return

        if not body.strip():
            return

        print(f"[network] Transcript intercettato: HTTP {response.status}")
        print(f"[network] Content-Type: {content_type}")
        print(f"[network] URL: {url.split('?', 1)[0]}...")

        captured["body"] = body
        captured["content_type"] = content_type
        captured["url"] = url
        capture_event.set()

    async with async_playwright() as pw:
        context: BrowserContext = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            locale="it-IT",
            timezone_id="Europe/Rome",
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()
        page.on("response", handle_response)

        try:
            print("[1/6] Apertura Stream...")
            await page.goto(
                args.url,
                wait_until="domcontentloaded",
                timeout=120_000,
            )

            print("[2/6] Attesa sessione autenticata e caricamento...")
            await wait_for_auth_and_page(
                page,
                timeout_ms=args.timeout_minutes * 60 * 1000,
            )

            print("[2.5/6] Apro/riapro il pannello trascrizione per forzare la richiesta API...")
            try:
                btn = page.locator('button[aria-label*="rascriz"], button[aria-label*="ranscript"]').first
                if await btn.is_visible(timeout=5000):
                    # Se già aperto, chiudo e riapro per rilanciare la richiesta API
                    is_list_visible = await page.locator("[aria-posinset]").first.is_visible(timeout=2000)
                    if is_list_visible:
                        await btn.click()           # chiude
                        await page.wait_for_timeout(800)
                    await btn.click()               # apre (e scatena la richiesta API)
                    await page.wait_for_timeout(2000)
            except Exception:
                pass

            # Aspetta la risposta di rete (max 15s)
            try:
                await asyncio.wait_for(capture_event.wait(), timeout=15.0)
            except TimeoutError:
                pass

            raw_data: Any = None
            metadata: dict[str, Any] = {}
            source_url = None

            print("[3/6] Lettura metadati transcript dalla pagina...")
            page_metadata = None
            for _ in range(5):
                page_metadata = await extract_metadata(page)
                if page_metadata:
                    break
                await page.wait_for_timeout(2000)

            if page_metadata:
                metadata = {
                    k: v
                    for k, v in page_metadata.items()
                    if k != "temporaryDownloadUrl"
                }
                print(
                    "[meta]",
                    json.dumps(metadata, ensure_ascii=False),
                )

                if page_metadata.get("temporaryDownloadUrl"):
                    print("[4/6] Recupero transcript JSON (con relatori) tramite API...")
                    try:
                        raw_data, source_url = await fetch_transcript_from_page(
                            page,
                            page_metadata,
                        )
                        print("[api] Transcript JSON recuperato.")
                    except Exception as exc:
                        print(f"[api] Recupero API fallito: {exc}")
                        raw_data = None
                else:
                    print("[!] temporaryDownloadUrl assente: uso transcript intercettato dal network.")

            if raw_data is None and "body" in captured:
                print("[4.5/6] Fallback su transcript intercettato dal network...")
                body = captured["body"]
                source_url = captured.get("url")

                if (
                    "webvtt" in captured.get("content_type", "")
                    or body.lstrip().upper().startswith("WEBVTT")
                ):
                    raw_data = parse_vtt_to_raw_json(body)
                else:
                    try:
                        raw_data = json.loads(body)
                    except json.JSONDecodeError:
                        raw_data = None

            print("[5/6] Raccolta della lista transcript virtualizzata dal DOM per i relatori...")
            try:
                dom_entries = await collect_virtualized_dom_transcript(page)
                print(f"[dom] Raccolte {len(dom_entries)} voci dalla lista virtualizzata.")
            except Exception as exc:
                print(f"[dom] Fallito recupero DOM: {exc}")
                dom_entries = []

            if raw_data is None:
                if not dom_entries:
                    raise RuntimeError(
                        "Nessun transcript trovato né via API né nel DOM."
                    )

                normalized = {
                    "format": "teams-transcript-dom-fallback",
                    "source": metadata,
                    "entry_count": len(dom_entries),
                    "entries": dom_entries,
                    "_raw_response": None,
                }
                raw_data = normalized

                data = normalize_transcript(
                    {
                        "entries": [
                            {
                                "speakerDisplayName": e.get("speaker"),
                                "startOffset": None,
                                "endOffset": None,
                                "text": e.get("text"),
                            }
                            for e in dom_entries
                        ]
                    },
                    metadata=metadata,
                )
                # Mantieni ordine e timestamp del fallback DOM.
                for a, b in zip(data["entries"], dom_entries):
                    a["start_seconds"] = b.get("start_seconds")
                    a["end_seconds"] = b.get("end_seconds")
            else:
                print("[5.5/6] Normalizzazione transcript JSON e merge relatori...")
                data = raw_json_to_entries(raw_data)
                data["source"] = metadata

                if dom_entries:
                    # Ottimizza la ricerca dei relatori
                    for api_e in data.get("entries", []):
                        if api_e.get("speaker"):
                            continue
                        
                        api_text = api_e.get("text", "").lower()
                        api_ts = api_e.get("start_seconds")
                        
                        for dom_e in dom_entries:
                            if not dom_e.get("speaker"):
                                continue
                            
                            dom_ts = dom_e.get("start_seconds")
                            if api_ts is not None and dom_ts is not None:
                                if abs(api_ts - dom_ts) > 6.0:
                                    continue
                            
                            dom_text = dom_e.get("text", "").lower()
                            if dom_text and (dom_text in api_text or api_text in dom_text):
                                api_e["speaker"] = dom_e["speaker"]
                                break

            if source_url:
                data["source"]["transcript_endpoint"] = source_url.split("?", 1)[0]

            data["source"]["page_url"] = page.url

            # Rimuoviamo eventuali campi vuoti troppo rumorosi.
            data["entry_count"] = len(data.get("entries", []))

            if data["entry_count"] == 0:
                raise RuntimeError(
                    "Il transcript è stato recuperato ma contiene 0 entries."
                )

            print("[6/6] Scrittura output...")
            write_outputs(data, out_dir)

            print()
            print("=" * 72)
            print("ESTRAZIONE COMPLETATA")
            print("=" * 72)
            print(f"Voci       : {data['entry_count']}")
            print(f"JSON       : {out_dir / 'trascrizione.json'}")
            print(f"TXT        : {out_dir / 'trascrizione.txt'}")
            print(f"VTT        : {out_dir / 'trascrizione.vtt'}")
            print()

            return 0

        finally:
            await context.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERRORE: {exc}", file=sys.stderr)
        raise SystemExit(1)
