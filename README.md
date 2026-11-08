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

## Configurazione

Crea il file `.env` a partire dall'esempio:

```bash
cp .env.example .env
```
oppure

```cmd
copy .env.example .env
```


Modifica `.env`:

```env
STREAM_URL="https://tuo-istituto.sharepoint.com/sites/.../stream.aspx?id=..."

# Facoltativi: login automatico
MICROSOFT_EMAIL="tua@email.it"
MICROSOFT_PASSWORD="tua password"
```

Lo script carica `.env` dalla propria cartella. In alternativa, passa l'URL con `--url`.

Durante l'accesso, completa eventuali verifiche richieste dall'istituto. Inviti facoltativi con scelta esatta “Non ora”, “Salta per ora” o equivalenti inglesi vengono saltati automaticamente.

---

## Installazione

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

---


## Esecuzione

Su Windows, dopo aver configurato `.env`, avvia l'interfaccia grafica con un solo comando:

```cmd
Avvia.bat
```

Al primo avvio il file crea l'ambiente virtuale e installa dipendenze e Chromium. Incolla uno o più link SharePoint e aggiungili alla coda. Ogni riga ha i pulsanti per copiare il link, cambiare il nome della cartella di output o rimuovere la registrazione dalla coda. Scegli la cartella generale da **Impostazioni** e premi **Avvia estrazione**. Ogni registrazione viene salvata in una sottocartella col nome ricavato dall'URL SharePoint (per esempio `Programmazione Lineare Intera`), con i file JSON, TXT e VTT al suo interno. Se l'URL non contiene un nome leggibile, usa i metadati della registrazione. Se quel nome esiste già, viene aggiunto un suffisso numerico per non sovrascrivere la cartella precedente.

Il logo originale è conservato in `assets/logo_source.jfif`; i ritagli `logo_header.png` e `app_icon.png` vengono usati nella GUI e `app_icon.ico` come icona finestra su Windows. Le icone monocromatiche PNG dei pulsanti sono in `assets/icons/`.

In **Impostazioni** puoi cambiare cartella di destinazione, timeout del browser (0 = nessun limite), percorso di Chrome/Chromium e attivare la modalità scura. La scelta dell'aspetto viene ricordata tra gli avvii. Il salvataggio automatico conserva coda e impostazioni nel profilo locale; credenziali e sessione restano rispettivamente in `.env` e nel profilo Chromium persistente.

Per usare ancora la riga di comando: `Avvia.bat --cli --url "https://..." --out-dir output`.

Su Linux rendi eseguibile il launcher una volta e avvialo:

```bash
chmod +x Avvia.sh
./Avvia.sh
```

Anche il launcher Linux prepara automaticamente ambiente, dipendenze e Chromium al primo avvio. Per usare la riga di comando: `./Avvia.sh --cli --url "https://..." --out-dir output`.

La GUI usa CustomTkinter e Pillow; il launcher li installa insieme alle altre dipendenze. Su alcune distribuzioni Linux potrebbe essere necessario installare anche Tk dal gestore pacchetti di sistema (ad esempio `python3-tk`).

```bash
# Con venv attivo (consigliato)
python extract_stream_transcript.py

# Oppure con argomenti da CLI (sovrascrivono .env)
python extract_stream_transcript.py \
  --url "https://..." \
  --out-dir "./output" \
  --timeout-minutes 10
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
