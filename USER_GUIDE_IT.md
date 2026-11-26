# Meeting Captions — Guida Utente

*Disponibile in: [English](USER_GUIDE.md) | **Italiano***

**Meeting Captions** ascolta le tue riunioni, mostra i sottotitoli in diretta sullo schermo e genera automaticamente verbali chiari e attività operative su **Odoo**. Tutto funziona in locale sul server dell'ufficio.

---

## Cosa fa per te

* **Sottotitoli in diretta sullo schermo:** Vedi ciò che dicono i partecipanti in tempo reale in un piccolo riquadro nell'angolo dello schermo (oppure in una finestra completa del browser).
* **Funziona dal vivo e in videochiamata:** Acquisisce il tuo microfono e può anche sottotitolare chi parla da remoto su Teams, Zoom o Google Meet.
* **Verbali automatici:** A fine riunione riassume la discussione, elenca le decisioni prese ed estrae le attività da svolgere con responsabile e scadenza.
* **Salva direttamente su Odoo:**
  * Il verbale completo viene pubblicato in **Odoo → Knowledge → Meeting Notes** così tutti possono consultarlo.
  * Ogni attività viene creata e assegnata alla persona corretta in **Odoo Progetti**.

---

## Prima configurazione (richiede 1 minuto)

1. Apri **Meeting Captions** dal menu Start di Windows (si avvia anche in automatico all'accesso a Windows).
2. Compila la finestra iniziale:
   * **Il tuo nome (`Your name`):** Inserisci il tuo nome come compare su Odoo (es. nome e cognome oppure cognome) così le attività vengono assegnate a te.
   * **Riunione (`Meeting`):** Lascia il nome automatico con data e ora, oppure scegli/scrivi il nome condiviso della riunione se partecipano anche altri colleghi.
   * **Lingua parlata (`Language you speak`):** **Italiano** è selezionato come predefinito. Cambialo se la riunione è in inglese o in un'altra lingua.
   * **Microfono (`Microphone`):** Scegli le cuffie o il microfono da tavolo, oppure lascia il microfono predefinito di sistema.
   * **Sottotitola anche l'altro lato della chiamata (`Also caption the other side of calls`):** Attivalo **solo** se partecipi a una call (Teams, Zoom, Meet). Se più persone nella stessa stanza sono collegate alla stessa call, deve attivarlo **solo una** persona.
3. Clicca **Start**.

---

## Durante la riunione

### Il riquadro dei sottotitoli
Un piccolo riquadro semitrasparente appare in basso a destra sullo schermo:
* **Spostalo ovunque** trascinandolo con il mouse.
* **Pause / Resume:** Silenzia temporaneamente il microfono verso l'app (utile se ti allontani o fai una telefonata privata).
* **Hide:** Nasconde il riquadro mantenendo i sottotitoli attivi in background.
* **End meeting:** Apre la finestra di conferma per fermare lo strumento e generare il verbale.

### L'icona nella barra di sistema (vicino all'orologio)
Il pallino colorato sull'icona **Meeting Captions** mostra subito lo stato:
* 🟢 **Verde:** Attivo e in ascolto
* 🟡 **Giallo:** In pausa
* ⚪ **Grigio:** Fermo
* 🔴 **Rosso:** Riconnessione al server dell'ufficio in corso

Fai clic destro sull'icona in qualsiasi momento per:
* **Show / Hide captions** — Mostra o nascondi il riquadro.
* **Pause / Resume** oppure **Start / Stop streaming**.
* **Change meeting…** — Scegli una riunione dal calendario Odoo di oggi (o scrivi un nome) e seleziona la lingua della riunione.
* **Open live captions** — Apre la trascrizione in diretta a schermo intero nel browser.
* **Open dashboard** — Apre la pagina web per consultare riunioni passate, trascrizioni e verbali.
* **End meeting & write notes…** — Termina la riunione e genera il verbale.

---

## Terminare la riunione e leggere il verbale

Quando clicchi **End meeting** (o **Quit** mentre una riunione è attiva), appare una finestra di conferma:

> *"Do you want to stop the tool and generate meeting notes?"*

* **Stop & generate** — Ferma l'ascolto e crea il verbale (richiede 1–2 minuti). Al termine compare il pulsante **Open notes** direttamente sul riquadro.
* **Stop only** — Ferma l'ascolto senza generare il verbale.
* **Cancel** — Continua l'ascolto.

> **Promemoria automatici:** Se tutti gli altri partecipanti escono, se nessuno parla per 10 minuti o se l'orario previsto sul calendario Odoo termina, l'app mostra automaticamente questa stessa richiesta per non dimenticare la sessione aperta.

### Dove trovare i verbali
* **Odoo Knowledge:** Apri **Knowledge → Meeting Notes** su Odoo per leggere riepilogo, argomenti trattati, decisioni e tabella delle attività.
* **Odoo Progetti:** Le attività vengono assegnate automaticamente a ciascun responsabile su Odoo.
* **Dashboard del server:** Trascrizione, verbale e registrazione audio restano sempre salvati anche sulla dashboard dell'ufficio.

---

## Consigli rapidi e risoluzione problemi

* **Non compaiono sottotitoli?** Controlla che il pallino sull'icona sia **verde** (non in pausa o grigio) e che il microfono non sia mutato su Windows o sulle cuffie.
* **Un nome non viene abbinato su Odoo?** Usa nome e cognome oppure il cognome nella configurazione (ad esempio se ci sono due colleghi con lo stesso nome di battesimo).
* **Odoo non era raggiungibile a fine riunione?** Non si perde nulla: il verbale resta salvato nella dashboard e un amministratore può cliccare **Publish to Odoo** in un secondo momento con un clic.
