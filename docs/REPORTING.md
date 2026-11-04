# PorcelainberryMe Reporting – Faktenbasis statt geschätzter Kennzahlen

Status: API, MCP-Werkzeug, PWA-Client und CSV-Tagesexport im Quellcode
implementiert; Testdaten und lokale Netzwerktests erfolgreich. Kein
produktiver FruitStack-Host oder echtes Mobilfunkgespräch verifiziert.

## 1. Verfügbare Betriebskennzahlen

Die einzige autoritative Datenquelle ist die vorhandene SQLite-Datenbank
der PorcelainberryMe-Call-Control-API. Die Daten werden nicht von einem
Sprachmodell errechnet oder aus Gesprächsformulierungen abgeleitet.

| Kennzahl | Technische Definition |
| --- | --- |
| calls | Anzahl gespeicherter Anrufe mit Startzeit im Zeitraum |
| incoming / outgoing | Richtung relativ zur authentifizierten Identität |
| agent_initiated | Vom kontrollierten KI-Agenten gestartete Anrufe |
| accepted_confirmed | Explizite ACTIVE-Transition mit persisted accepted_at |
| ringing / active / ended / declined / timed_out | Aktueller, vom Server bereinigter Status |
| transferred_confirmed | Handoff in den Zustand CONNECTED übergegangen |
| duration_known_calls | Abgeschlossene Gespräche mit explizitem Annahmezeitpunkt |
| control_session_seconds | closed_at minus accepted_at, mindestens 0 Sekunden |
| acceptance_unverified | Historische Altanrufe ohne Annahmezeitpunkt; nicht als ungeantwortet erfinden |

Die Tagesreihe verwendet die STARTZEIT des Anrufs in UTC. Ein Gespräch
wird nicht mehrfach gezählt, auch wenn es mehrere Statusereignisse gibt.
Die Dauer misst die Zeit zwischen Anrufannahme und Beendigung aus dem
Call-Control-System, nicht tatsächlich übertragenes Audiomaterial. Die
Datenbank verwahrt Anrufhistorie maximal 30 Tage und entfernt sie danach
entsprechend den vorhandenen Retention-Regeln.

Alte Installationen werden vorwärtskompatibel um accepted_at erweitert.
Historische Annahmezeitpunkte werden NICHT geschätzt, wenn ihre Ereignisse
bereits gelöscht wurden. Auch das Autoreply/Lease-Claim eines eingehenden
KI-Anrufs speichert jetzt den Annahmezeitpunkt.

## 2. Abfrage

Der bestehende authentifizierte, lokale Call-Control-HTTP-Server bietet:

    GET /v1/ip-calls/report

Standard: letzte 7 Tage. Für explizite Zeitfenster Unix-Sekunden verwenden:

    GET /v1/ip-calls/report?start=1800000000&end=1800003600

Die Startzeit ist inklusive, die Endzeit exklusiv. Maximal 30 Tage
innerhalb der vorhandenen Aufbewahrungsfrist. Authentifizierung:
unveränderter Authorization: Bearer <SIGNED_PARTICIPANT_TOKEN> des bestehenden
Call-Control-Systems.

**Berechtigungsgrenze:** Der Aufruf zeigt ausschließlich Anrufe, an denen
die authentifizierte Person selbst als Anrufer oder Empfänger beteiligt war,
innerhalb ihres eigenen Mandanten. Ein externer Kontakt und ein
Handoff-Mitarbeiter können darüber keine allgemeinen Kunden- oder
Mandantenstatistiken abfragen. Es gibt noch keine zusätzliche administrative
Rolle für mandantenweites Management-Reporting. Eine solche Rolle muss vor
einem vollständigen Kundenportal explizit definiert werden.

Ergebnis: schema porcelainberryme.reporting.calls.v1 mit metrics,
daily, Zeitzone und limitation-Feldern; keine Namen, Rufnummern,
Transkripte, JWTs, Kontaktkennungen oder Call-IDs im Antwortkörper.

## 3. FruitStack- und Browser-Anbindung

Der vorhandene MCP-Server bietet read-only:

    porcelainberryme_call_report()

Das Werkzeug liefert nur validierte, freigegebene KPI-Felder für die
MCP-Identität und immer das sieben Tage umfassende Standardfenster.
Es akzeptiert keine Mandantenkennung, SQL-Abfrage oder Personensuche.

Der vorhandene Browser-IPCallClient bietet:

    const report = await client.callReport();
    const csv = callReportDailyCsv(report);

Ein individuell autorisierter Zeitraum ist über
client.callReport({start, end}) abrufbar. Die CSV-Exportfunktion enthält
nur UTC-Tageswerte und geprüfte ganze Zahlen, getrennt mit Semikolon;
sie erzeugt keine Tabellenformeln, Kundendaten oder Tracking-Cookies.
Der Aufrufer ist für Datei-/UI-Darstellung in der bereits vorhandenen
FruitStack-Oberfläche verantwortlich; ein neues eigenmächtiges UI-Design
ist absichtlich nicht eingeführt worden.

## 4. Was Fonio bereits weiter anbietet

Fonio zeigt im öffentlichen Analytics-Bereich zusätzlich:
- Konversationszusammenfassungen, Transkripte und optionale Aufzeichnungen;
- erfasste Kundenthemen und strukturierte Gesprächsergebnisse
  (z. B. Termin gebucht, Problem gelöst, Spam, Weiterleitung);
- Preise/Nutzungsminuten, Kontingente und kampagnenbezogene Ergebnisse;
- eine fertige Dashboard- und Filteroberfläche.

Quelle: https://fonio.info/en/get-started/analytics

Diese Funktionen darf PorcelainberryMe nicht als real vorhanden bezeichnen,
solange nur der Call-Control-Lifecycle gemessen wird. Für belastbare
geschäftliche Kennzahlen braucht die Plattform den bestätigten
Terminkalendereintrag, eine autorisierte CRM-Aktionsquittung sowie
konfigurierbare Einwilligung zur Speicherung der Zusammenfassung.

## 5. Reporting-Ausbau für das gesamte FruitStack-Portfolio

- **Technik und Betrieb:** echte STT/TTS/LLM-Latenzen, Zeit bis zum ersten
  hörbaren Audiopaket, Unterbrechungen, Media-Fehler, Abbruchgründe.
  Daten müssen an tatsächlichen Ereignissen instrumentiert werden.
- **Gesprächsqualität:** explizit autorisierte Erkennung von Anliegen,
  freigegebene Zusammenfassungen, Qualität, unerledigte Gespräche und
  Mitarbeiterrückrufe; keine pauschale Aufzeichnung.
- **CRM-Ergebnisse:** bestätigte Termine, neue Leads, erledigte Fälle,
  Übergaben und automatisierte Folgeaufträge in TonnEstate/GreenGrapeMe.
  Quittungen stammen aus dem jeweiligen Fachsystem, nicht aus LLM-Text.
- **Wirtschaftlichkeit:** lokale Rechenkosten als gemessene Ressourcen
  plus tatsächliche SIP-Carrier-Rechnungen; ohne Carrier-Rechnungen kein
  vermeintlich exakter Preis pro Anruf.
- **Management-Dashboard:** erst nach expliziter Mandanten- und
  Rollenberechtigung aggregierte Team-KPIs und längere Historie. Eine
  separate, zweckgebundene, datensparsame Statistik darf nicht durch
  Aufhebung der bestehenden 30-Tage-Retention entstehen.

Die aktuelle Umsetzung ist das erste, technisch verifizierte
Call-Control-Reporting. Produktive End-to-End-Kennzahlen und der
Fonio-Dashboard-Funktionsumfang stehen noch aus.
