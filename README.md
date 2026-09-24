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

Servono Python 3.9 o superiore. Crea ambiente e installa dipendenze.

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

### Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

Su Ubuntu/Debian, se Chromium segnala librerie di sistema mancanti:

```bash
.venv/bin/python -m playwright install --with-deps chromium
```

---

## Configurazione

Crea un file `.env` nella cartella del progetto:

```env
STREAM_URL="https://tuo-istituto.sharepoint.com/sites/.../stream.aspx?id=..."

# Facoltativi: login automatico
MICROSOFT_EMAIL="tua@email.it"
MICROSOFT_PASSWORD="tua password"
```

Lo script carica `.env` dalla propria cartella. In alternativa, passa l'URL con `--url`.

Durante l'accesso, completa eventuali verifiche richieste dall'istituto. Inviti facoltativi con scelta esatta “Non ora”, “Salta per ora” o equivalenti inglesi vengono saltati automaticamente.

---

## Esecuzione

Windows (PowerShell):

```powershell
.\.venv\Scripts\python.exe extract_stream_transcript.py --url "https://..." --out-dir ".\output" --timeout-minutes 10
```

Linux:

```bash
.venv/bin/python extract_stream_transcript.py --url "https://..." --out-dir "./output" --timeout-minutes 10
```

Il browser si apre per il login iniziale; sessione salvata nel profilo `.pw-sharepoint-profile`. Su Linux, primo accesso richiede ambiente grafico. Non avviare due copie insieme: Chromium permette una sola istanza per profilo.

Se l'API non è disponibile, lo script scorre il pannello della trascrizione. Se non riesce a visitare tutte le voci, segnala l'estrazione incompleta e non sovrascrive i file precedenti.

**Output** (nella cartella corrente o in `--out-dir`):

| File | Contenuto |
|---|---|
| `trascrizione.json` | Dati strutturati con speaker e timestamp |
| `trascrizione.txt` | Testo leggibile |
| `trascrizione.vtt` | Sottotitoli WebVTT |

---

## Avvertenze

Usa lo script solo per contenuti a cui sei legalmente autorizzato. Rispetta i termini di servizio di Microsoft e del tuo istituto.
