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
import math
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
import os

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Response, Page, BrowserContext

load_dotenv(Path(__file__).with_name(".env"))


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
TIME_CODE_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:[.,]\d+)?)$")


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
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        seconds = float(value)
        return seconds if math.isfinite(seconds) else None

    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None

    m = ISO_DURATION_RE.match(value)
    if m:
        return (
            float(m.group("h") or 0) * 3600.0
            + float(m.group("m") or 0) * 60.0
            + float(m.group("s") or 0)
        )

    # Le API Stream restituiscono anche offset come "00:01:23.400";
    # altre versioni usano secondi numerici serializzati come stringhe.
    m = TIME_CODE_RE.match(value.replace(",", "."))
    if m:
        hours, minutes, seconds = m.groups()
        return (
            int(hours or 0) * 3600.0
            + int(minutes) * 60.0
            + float(seconds)
        )

    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


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


def safe_url(value: str) -> str:
    """Rimuove query e frammento dai URL diagnostici, che possono contenere token."""
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path}"


def first_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


def transcript_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("text", "content", "value", "displayText"):
            if key in value:
                text = transcript_text(value[key])
                if text:
                    return text
        return ""
    if isinstance(value, list):
        return clean_text(" ".join(transcript_text(part) for part in value))
    return clean_text(value)


def speaker_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = first_value(value, "displayName", "name", "speakerDisplayName", "id")
    return clean_text(value) or None


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

        speaker = speaker_name(
            first_value(
                item,
                "speakerDisplayName",
                "speakerName",
                "speaker",
                "speakerId",
                "displayName",
            )
        )

        start_offset = first_value(
            item, "startOffset", "startSeconds", "start_seconds", "startTime", "start"
        )
        end_offset = first_value(
            item, "endOffset", "endSeconds", "end_seconds", "endTime", "end"
        )
        text_value = first_value(
            item, "text", "content", "caption", "transcriptText", "displayText"
        )

        entries.append(
            Entry(
                index=idx,
                start_seconds=iso_to_seconds(start_offset),
                end_seconds=iso_to_seconds(end_offset),
                speaker=speaker,
                text=transcript_text(text_value),
                start_offset=str(start_offset) if start_offset is not None else None,
                end_offset=str(end_offset) if end_offset is not None else None,
            )
        )

    # Riordina solo se ogni voce ha un timestamp: con valori mancanti l'ordine
    # originale dell'API contiene più informazione di un ordinamento parziale.
    if entries and all(entry.start_seconds is not None for entry in entries):
        entries.sort(key=lambda entry: (entry.start_seconds, entry.index))

    # Reindicizza.
    for i, entry in enumerate(entries):
        entry.index = i

    # Se mancano gli endOffset, usa l'inizio della voce successiva.
    # Per l'ultima voce usa +2s come durata minima di visualizzazione.
    for i, entry in enumerate(entries):
        if entry.end_seconds is None:
            next_start = next(
                (
                    following.start_seconds
                    for following in entries[i + 1 :]
                    if following.start_seconds is not None
                ),
                None,
            )
            if next_start is not None and entry.start_seconds is not None:
                entry.end_seconds = max(
                    entry.start_seconds + 0.25, next_start
                )
            elif entry.start_seconds is not None:
                entry.end_seconds = entry.start_seconds + 2.0

        if entry.start_seconds is not None and entry.start_offset is None:
            entry.start_offset = seconds_to_timestamp(entry.start_seconds)
        if entry.end_seconds is not None and entry.end_offset is None:
            entry.end_offset = seconds_to_timestamp(entry.end_seconds)

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
        parts = value.strip().split()
        if not parts:
            return None
        value = parts[0].replace(",", ".")
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


async def collect_virtualized_dom_transcript(page: Page) -> dict[str, Any]:
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

          // Stream/Fluent UI marca lo scroller reale con data-is-scrollable.
          // Il solo overflowY varia tra Windows e Linux.
          let scroller = firstEntry.closest('[data-is-scrollable="true"]');
          if (!scroller) {
            let parent = firstEntry;
            while (parent && parent !== root.parentElement) {
              if (isScrollable(parent)) {
                scroller = parent;
                break;
              }
              parent = parent.parentElement;
            }
          }
          if (!scroller) {
            throw new Error("Scroller del transcript non trovato nel DOM.");
          }

          const found = new Map();
          const observedPositions = new Set();
          let expectedCount = null;

          function collectVisible() {
            root.querySelectorAll("[aria-posinset]").forEach(el => {
              const pos = el.getAttribute("aria-posinset") || "";
              const posNumber = Number(pos);
              if (Number.isInteger(posNumber) && posNumber > 0) {
                observedPositions.add(posNumber);
              }
              const setSize = Number(el.getAttribute("aria-setsize"));
              if (Number.isInteger(setSize) && setSize > 0) {
                expectedCount = Math.max(expectedCount || 0, setSize);
              }

              // eventText indica eventi di sistema, non parlato trascritto.
              const textEl = el.matches("[class*='entryText']")
                ? el
                : el.querySelector("[class*='entryText']");
              if (!textEl) return;
              const text = (textEl.innerText || "").trim();
              if (!text) return;

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

          function hasAllPositions() {
            if (!expectedCount || observedPositions.size < expectedCount) return false;
            for (let pos = 1; pos <= expectedCount; pos++) {
              if (!observedPositions.has(pos)) return false;
            }
            return true;
          }

          let reachedBottom = false;
          for (let pass = 0; pass < 2; pass++) {
            scroller.scrollTop = 0;
            await sleep(350);
            let noProgress = 0;
            let bottomRounds = 0;

            for (let round = 0; round < 800; round++) {
              collectVisible();
              if (hasAllPositions()) {
                break;
              }

              const currentTop = scroller.scrollTop;
              const maxTop = Math.max(
                0, scroller.scrollHeight - scroller.clientHeight
              );
              const step = Math.max(
                1, Math.floor(scroller.clientHeight * (pass === 0 ? 0.4 : 0.25))
              );
              scroller.scrollTop = Math.min(maxTop, currentTop + step);
              await sleep(180);
              collectVisible();

              const newTop = scroller.scrollTop;
              const newMaxTop = Math.max(
                0, scroller.scrollHeight - scroller.clientHeight
              );
              if (maxTop > currentTop + 2 && newTop <= currentTop + 1) {
                noProgress++;
                if (noProgress >= 3) {
                  throw new Error("Lo scroller del transcript non avanza.");
                }
              } else {
                noProgress = 0;
              }

              bottomRounds = newTop >= newMaxTop - 2
                ? bottomRounds + 1
                : 0;
              if (bottomRounds >= 8) {
                reachedBottom = true;
                break;
              }
            }

            if (!expectedCount || hasAllPositions()) break;
          }

          collectVisible();

          function parseLabel(label, fullText) {
            let speaker = null;
            let seconds = null;

            if (label) {
                // Le etichette Stream cambiano lingua in base al tenant/browser.
                let m = label.match(
                    /(?:(\\d+)\\s*(?:hours?|hrs?|or[ae])[,;]?\\s*)?(\\d+)\\s*(?:minutes?|mins?|minut[io])[,;]?\\s*(?:and\\s+)?(\\d+)\\s*(?:seconds?|secs?|second[oi])/i
                );
                if (m) {
                    const timeStart = m.index || 0;
                    speaker = label.slice(0, timeStart).replace(/[,:\\s–-]+$/, "").trim() || null;
                    seconds = Number(m[2]) * 60 + Number(m[3]);
                    if (m[1]) seconds += Number(m[1]) * 3600;
                    return { speaker, seconds };
                }
                // Formato: PAOLA FESTA a 01:23 / Speaker at 01:23.
                m = label.match(/^(.+?)\\s+(?:a|at)\\s+(\\d+):(\\d+)(?::(\\d+))?/i);
                if (m) {
                    speaker = m[1].replace(/[,:\\s–-]+$/, "").trim() || null;
                    if (m[4]) {
                        seconds = Number(m[2]) * 3600 + Number(m[3]) * 60 + Number(m[4]);
                    } else {
                        seconds = Number(m[2]) * 60 + Number(m[3]);
                    }
                    return { speaker, seconds };
                }

                // Alcuni tenant annunciano solo i secondi trascorsi.
                m = label.match(/(\\d+)\\s*(?:seconds?|secs?|second[oi])/i);
                if (m) {
                    speaker = label.slice(0, m.index).replace(/[,:\\s–-]+$/, "").trim() || null;
                    seconds = Number(m[1]);
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

          const entries = rows.map((x, i) => {
            const nextStart = rows
              .slice(i + 1)
              .find(next => next.startSeconds != null)?.startSeconds;
            return {
              index: i,
              start_seconds: x.startSeconds,
              end_seconds: x.startSeconds == null
                ? null
                : (nextStart == null
                    ? x.startSeconds + 2
                    : Math.max(x.startSeconds + 0.25, nextStart)),
              speaker: x.speaker,
              text: x.text
            };
          });

          let missingCount = null;
          if (expectedCount) {
            missingCount = 0;
            for (let pos = 1; pos <= expectedCount; pos++) {
              if (!observedPositions.has(pos)) missingCount++;
            }
          }

          return {
            entries,
            expected_count: expectedCount,
            observed_count: observedPositions.size,
            missing_count: missingCount,
            complete: expectedCount ? hasAllPositions() : reachedBottom
          };
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
                    "start_offset": (
                        seconds_to_timestamp(x["startSeconds"])
                        if x.get("startSeconds") is not None
                        else None
                    ),
                    "end_offset": (
                        seconds_to_timestamp(x["endSeconds"])
                        if x.get("endSeconds") is not None
                        else None
                    ),
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


def merge_dom_entries(
    entries: list[dict[str, Any]], dom_entries: list[dict[str, Any]]
) -> None:
    """Recupera speaker e tempi mancanti quando il testo identifica la voce."""

    def comparable_text(value: Any) -> str:
        return re.sub(r"[^\w]+", " ", clean_text(value).casefold()).strip()

    for entry in entries:
        entry_text = comparable_text(entry.get("text"))
        if not entry_text:
            continue

        matches: list[tuple[int, float, dict[str, Any]]] = []
        entry_start = entry.get("start_seconds")
        for dom_entry in dom_entries:
            speaker = clean_text(dom_entry.get("speaker"))
            dom_text = comparable_text(dom_entry.get("text"))
            if not dom_text:
                continue

            if entry_text == dom_text:
                text_score = 0
            elif min(len(entry_text), len(dom_text)) >= 16 and (
                entry_text in dom_text or dom_text in entry_text
            ):
                text_score = 1
            else:
                continue

            dom_start = dom_entry.get("start_seconds")
            time_distance = 0.0
            if entry_start is not None and dom_start is not None:
                time_distance = abs(float(entry_start) - float(dom_start))
                if time_distance > 6.0:
                    continue
            else:
                time_distance = 6.1
            matches.append((text_score, time_distance, dom_entry))

        if matches:
            best_score = min((score, distance) for score, distance, _ in matches)
            best_matches = [
                dom_entry
                for score, distance, dom_entry in matches
                if (score, distance) == best_score
            ]

            # Senza timestamp, testo ripetuto può riferirsi a parlanti diversi.
            if entry_start is None:
                identities = {
                    (
                        clean_text(item.get("speaker")) or entry.get("speaker"),
                        item.get("start_seconds"),
                    )
                    for item in best_matches
                }
                if len(identities) > 1:
                    continue

            match = best_matches[0]
            if not entry.get("speaker") and match.get("speaker"):
                entry["speaker"] = match.get("speaker")
            if entry_start is None and match.get("start_seconds") is not None:
                entry["start_seconds"] = match["start_seconds"]
                entry["start_offset"] = seconds_to_timestamp(match["start_seconds"])
            if (
                entry.get("end_seconds") is None
                and match.get("end_seconds") is not None
            ):
                entry["end_seconds"] = match["end_seconds"]
                entry["end_offset"] = seconds_to_timestamp(match["end_seconds"])


OPTIONAL_AUTH_PROMPT = re.compile(
    r"^(?:not now|skip(?: for now)?|no,? thanks|maybe later|later|"
    r"do this later|remind me later|"
    r"non ora|non adesso|salta(?: per ora)?|ignora(?: per ora)?|"
    r"no grazie|forse più tardi|più tardi)$",
    re.IGNORECASE,
)


async def dismiss_optional_auth_prompt(page: Page) -> bool:
    """Salta inviti facoltativi senza toccare le richieste di autenticazione."""
    for role in ("button", "link"):
        candidates = page.get_by_role(role, name=OPTIONAL_AUTH_PROMPT, exact=True)
        try:
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    label = await candidate.inner_text()
                    print(f"[auth] Salto passaggio facoltativo: {clean_text(label)}")
                    await candidate.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    return True
        except Exception:
            # Microsoft può navigare appena il pulsante viene premuto.
            continue
    return False


async def click_login_submit(page: Page, field: Any) -> None:
    candidates = page.locator(
        '#idSIButton9, button[type="submit"], input[type="submit"]'
    )
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if await candidate.is_visible() and await candidate.is_enabled():
            await candidate.click(timeout=5000)
            return
    await field.press("Enter")


async def auto_login_if_possible(
    page: Page, attempted_steps: set[tuple[str, str]]
) -> None:
    email = os.environ.get("MICROSOFT_EMAIL")
    password = os.environ.get("MICROSOFT_PASSWORD")
    if not email or not password:
        return

    try:
        email_input = page.locator(
            'input[type="email"], input[name="loginfmt"], '
            'input[name="UserName"], #userNameInput, input[autocomplete="username"]'
        )
        password_input = page.locator(
            'input[type="password"], input[name="passwd"], '
            'input[name="Password"], #passwordInput, '
            'input[autocomplete="current-password"]'
        )
        email_field = (
            email_input.first
            if await email_input.count() and await email_input.first.is_visible()
            else None
        )
        password_field = (
            password_input.first
            if await password_input.count() and await password_input.first.is_visible()
            else None
        )

        # ADFS mostra spesso username e password insieme; invia entrambi nello
        # stesso passaggio. Microsoft Online li presenta invece in schermate separate.
        fields = []
        if email_field is not None:
            fields.append((email_field, email))
        if password_field is not None:
            fields.append((password_field, password))

        if fields:
            stage = "credentials" if len(fields) == 2 else (
                "email" if email_field is not None else "password"
            )
            step = (safe_url(page.url), stage)
            if step not in attempted_steps:
                for field, value in fields:
                    if await field.input_value() != value:
                        await field.fill(value)
                attempted_steps.add(step)
                if len(fields) == 2:
                    print("[auth] Invio email e password...")
                else:
                    print(f"[auth] Invio {stage}...")
                await click_login_submit(page, fields[-1][0])
                await page.wait_for_timeout(1200)
            return

        # "Rimani collegato?" ha scelta esplicita; evita click generici su
        # pulsanti di consenso o configurazione MFA.
        stay_signed_in = page.get_by_role(
            "button", name=re.compile(r"^(?:yes|sì|si)$", re.IGNORECASE), exact=True
        )
        if await stay_signed_in.count() and await stay_signed_in.first.is_visible():
            await stay_signed_in.first.click(timeout=3000)
            await page.wait_for_timeout(800)
    except Exception as exc:
        print(f"[auth] Compilazione automatica sospesa: {exc}")


async def wait_for_auth_and_page(page: Page, timeout_ms: int) -> None:
    """
    Attende che SharePoint sia realmente caricata.
    Al primo avvio può apparire la pagina di login Microsoft.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    last_url = ""
    last_auth_notice_url = ""
    attempted_login_steps: set[tuple[str, str]] = set()

    while loop.time() < deadline:
        current_url = page.url
        if current_url != last_url:
            print(f"[browser] URL: {safe_url(current_url)}")
            last_url = current_url

        host = urlparse(current_url).netloc.lower()

        # Microsoft login / AAD / account picker.
        login_like = any(
            x in host
            for x in (
                "login.microsoftonline.com",
                "login.microsoft.com",
                "account.microsoft.com",
                "myaccount.microsoft.com",
                "mysignins.microsoft.com",
                "account.activedirectory.windowsazure.com",
            )
        ) or "adfs" in current_url.lower() or "login" in host

        if login_like:
            if await dismiss_optional_auth_prompt(page):
                continue
            if os.environ.get("MICROSOFT_EMAIL") and os.environ.get("MICROSOFT_PASSWORD"):
                await auto_login_if_possible(page, attempted_login_steps)
            elif current_url != last_auth_notice_url:
                print(
                    "[auth] Completa il login Microsoft nella finestra Chromium. "
                    "Se vuoi l'auto-login, inserisci MICROSOFT_EMAIL e MICROSOFT_PASSWORD nel file .env"
                )
                last_auth_notice_url = current_url
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
    print(f"URL        : {safe_url(args.url)}")
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
        print(f"[network] URL: {url.split('?', 1)[0]}...")

        captured["body"] = body
        captured["content_type"] = content_type
        captured["url"] = url
        capture_event.set()

    async with async_playwright() as pw:
        try:
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
        except Exception as exc:
            error = str(exc).casefold()
            profile_busy = "processsingleton" in error or (
                "profile directory" in error
                and ("already in use" in error or "lock file" in error)
            )
            if profile_busy:
                raise RuntimeError(
                    f"Profilo Chromium già in uso: {profile_dir}. "
                    "Chiudi l'altra istanza dello script o Chromium aperto "
                    "con questo profilo, poi riprova."
                ) from exc
            raise

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
            dom_entries: list[dict[str, Any]] = []
            dom_result: Optional[dict[str, Any]] = None
            dom_error: Optional[Exception] = None
            try:
                dom_result = await collect_virtualized_dom_transcript(page)
                dom_entries = dom_result["entries"]
                print(
                    f"[dom] Raccolte {len(dom_entries)} voci; "
                    f"posizioni visitate {dom_result['observed_count']}/"
                    f"{dom_result['expected_count'] or '?'}: "
                    f"{'completa' if dom_result['complete'] else 'incompleta'}."
                )
            except Exception as exc:
                print(f"[dom] Fallito recupero DOM: {exc}")
                dom_error = exc

            if raw_data is None:
                if not dom_entries:
                    raise RuntimeError(
                        f"Nessun transcript trovato via API o DOM: {dom_error}"
                        if dom_error else "Nessun transcript trovato via API o DOM."
                    ) from dom_error
                if dom_result is not None and not dom_result["complete"]:
                    raise RuntimeError(
                        "Trascrizione DOM incompleta: "
                        f"{dom_result['observed_count']}/"
                        f"{dom_result['expected_count'] or '?'} posizioni visitate."
                    )

                data = normalize_transcript(
                    {
                        "entries": [
                            {
                                "speakerDisplayName": e.get("speaker"),
                                "startSeconds": e.get("start_seconds"),
                                "endSeconds": e.get("end_seconds"),
                                "text": e.get("text"),
                            }
                            for e in dom_entries
                        ]
                    },
                    metadata=metadata,
                )
                data["format"] = "teams-transcript-dom-fallback"
            else:
                print("[5.5/6] Normalizzazione transcript JSON e merge relatori...")
                data = raw_json_to_entries(raw_data)
                data["source"] = metadata

                if dom_entries:
                    merge_dom_entries(data.get("entries", []), dom_entries)

            if source_url:
                data["source"]["transcript_endpoint"] = source_url.split("?", 1)[0]

            data["source"]["page_url"] = page.url

            # Rimuoviamo eventuali campi vuoti troppo rumorosi.
            data["entry_count"] = len(data.get("entries", []))

            if data["entry_count"] == 0:
                raise RuntimeError(
                    "Il transcript è stato recuperato ma contiene 0 entries."
                )

            missing_start = sum(
                entry.get("start_seconds") is None for entry in data["entries"]
            )
            missing_speaker = sum(
                not entry.get("speaker") for entry in data["entries"]
            )
            if missing_start or missing_speaker:
                print(
                    "[!] Campi ancora assenti dopo API e DOM: "
                    f"timestamp iniziali {missing_start}, relatori {missing_speaker}."
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
