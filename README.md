# UninaTranscriptDownloader

Scarica la trascrizione di una lezione registrata su **Microsoft Stream / SharePoint** (es. UniNA) in tre formati: JSON, TXT e VTT.

---

## Come funziona

1. Apre Chromium con un profilo persistente (`.pw-sharepoint-profile`).
2. Se non è già loggato, compila automaticamente email e password dal file `.env`.
3. Apre il pannello "Trascrizione" nella pagina del video.
4. Recupera i dati tramite l'API interna di Microsoft Stream; se non disponibile, legge il DOM come fallback.
5. Arricchisce ogni voce con il nome del relatore e il timestamp, poi salva i file di output.

---

## Installazione

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Configurazione

Crea il file `.env` a partire dall'esempio:

```bash
cp .env.example .env
```

Modifica `.env`:

```env
STREAM_URL="https://tuo-istituto.sharepoint.com/sites/.../stream.aspx?id=..."

# Opzionale: login automatico
MICROSOFT_EMAIL="tua@email.it"
MICROSOFT_PASSWORD="tuapassword"
```

---

## Esecuzione

```bash
# Con venv attivo (consigliato)
.venv/bin/python extract_stream_transcript.py

# Oppure con argomenti da CLI (sovrascrivono .env)
.venv/bin/python extract_stream_transcript.py \
  --url "https://..." \
  --out-dir "./output" \
  --timeout-minutes 10
```

Al **primo avvio**, il browser si apre e chiede il login. Da quel momento la sessione è salvata e non verrà più richiesta.

**Output** (nella cartella corrente o in `--out-dir`):

| File | Contenuto |
|---|---|
| `trascrizione.json` | Dati strutturati con speaker e timestamp |
| `trascrizione.txt` | Testo leggibile |
| `trascrizione.vtt` | Sottotitoli WebVTT |

---

## Avvertenze

Usa lo script solo per contenuti a cui sei legalmente autorizzato. Rispetta i termini di servizio di Microsoft e del tuo istituto.
