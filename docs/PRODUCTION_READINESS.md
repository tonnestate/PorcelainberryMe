# PorcelainberryMe: verbindliche Produktionsabnahme

**Status: technisch teilweise freigabefähig, noch nicht produktiv abgenommen.**
Ein grüner GitHub-Testlauf beweist die Quellcode-Integration, jedoch weder
einen angeschlossenen FruitStack-VPS noch einen real empfangenen Anruf.

Dieses Dokument ist das technische Release-Gate, nicht die Behauptung einer
bereits erfolgten Inbetriebnahme. Es erzeugt keine neue SaaS-Instanz und
verändert weder bestehende Geheimnisse noch die Benutzerverwaltung.

## In dieser Version nachgezogen

1. Der Loopback-Call-Control-HTTP-Server verwendet eine begrenzte Zahl
   gleichzeitiger Anfragen (64; konfigurierbare interne Konstruktorgrenze,
   maximal 256) und ein Idle-Socket-Timeout von 10 Sekunden. Volle
   Kapazität wird unmittelbar mit HTTP 503 quittiert, statt unbeschränkt
   neue Python-Threads zu erzeugen. Tests decken echte HTTP-Überlast
   und die Erholung ab.
2. /healthz zeigt weiterhin nur Prozess-Liveness. Das neue GET /readyz
   verwendet eine schreibgeschützte Datenbankverbindung, prüft die
   notwendigen Tabellen und antwortet mit 503, wenn die Call-Control-
   Datenbank nicht verwendbar ist. Eine konfigurierte Medien-URL ist
   dort nur eine Information, **kein** Beleg erfolgreicher Medienzustellung.
3. Reporting aggregiert in SQLite mit getrennten Indizes für
   Mandant/Anrufer/Zeit und Mandant/Empfänger/Zeit. Python erhält nur
   Tageswerte; Gesprächs-IDs, Personenkennungen und Zeitstempel einzelner
   Verbindungen werden nicht massenhaft in den Anwendungsspeicher geladen.
4. Ein separater, vollständig lesender Host-Release-Check ist als
   `porcelainberryme-release-gate` verfügbar; er kontrolliert private
   SQLite-Dateien, Signierschlüsselkonfiguration, vorhandene lokale
   Sprachdateien und optional echte lokale Dienst-/Netzwerkprobes.
5. Es gibt bewusst **nicht installierte** systemd-Vorlagen unter
   `deploy/systemd/` für Call-Control und den unabhängigen
   ausgehenden Sprachworker. Sie verwenden feste Service-Accounts,
   restriktive Filesystem- und Prozessrechte sowie keinen öffentlichen
   HTTP-Listener. Pfade müssen am realen Host bestätigt werden.

## Betrieb: Release-Gate auf dem echten Host

Nach Installation in der bestehenden Python-Umgebung und Überprüfung der
privaten Umgebungsdateien:

    porcelainberryme-release-gate --network

Der Check gibt nur PASS, FAIL oder NOT_TESTED zu benannten technischen
Prüfungen aus. Kein Pfad, Schlüssel, Token, Kunde, Gespräch oder
Caller-ID wird im JSON-Ergebnis ausgegeben. Das Programm tätigt keine
Anrufe und lädt keine Modelle herunter.

Ohne `--network` gibt es ausschließlich lokale Konfigurationsprüfungen
und die Laufzeitpunkte bleiben NOT_TESTED. Mit `--network` werden
ausschließlich folgende Ziele verwendet:

- Call-Control /readyz auf **127.0.0.1:8874**
- Ollama /api/tags auf **127.0.0.1:11434**
- TCP-Verfügbarkeit des explizit konfigurierten LiveKit-Endpunkts
  (kein Sprach- oder WebRTC-Medientest)

Für Installationen, die ausdrücklich keine Web-Push-Anrufzustellung
unterstützen sollen, kann `--no-mobile-push` gesetzt werden. Das
entspricht **nicht** einem produktiven mobilen Fonio-Ersatz.
Bei öffentlicher Telefonie `--require-pstn` setzen; dies markiert
Asterisk-/SIP-Carrier-Abnahmen als zusätzliche offene Nachweise.

Ein Status `configuration_passed=true` bzw.
`runtime_checks_passed=true` bedeutet nur, dass die technischen
Vorbedingungen überprüft wurden. `production_accepted` bleibt
absichtlich **false**, bis reale Host-/Sprach-/Telefonieabnahmen und
ein fachlicher Freigabeprozess außerhalb dieses Source-Audits
nachgewiesen sind.

## Verbindliche Abnahmekriterien

| Gate | Nachweis | Derzeit |
| --- | --- | --- |
| A. Codeintegrität | Python 3.11/3.12, Browser, MCP-SDK und Asterisk-WebSocket/ARI CI | Implementiert, nach jeder Änderung erneut prüfen |
| B. Medien-Pipeline | Echter LiveKit-WebRTC-Audiotransport mit synthetischen lokalen Modellen | In GitHub CI nachgewiesen; kein echter Sprachmodell-Nachweis |
| C. Betrieb | Privater DB-Pfad, wiederanlaufbarer Worker, Health/Ready, verifizierte Signierschlüssel | Code vorhanden; reale Serviceinstallation offen |
| D. Sicherheit | Authentisierte Aufnahme, Mandantentrennung, DND, Opt-in, Tokenrotation, Rücknahme | Source-Tests vorhanden; produktiver Penetrationstest offen |
| E. Smartphone | iPhone/Android im Hintergrund, Push → sichtbarer Anruf → Mikrofon → beidseitige Audioantwort | Nicht nachgewiesen |
| F. Sprachqualität | Reale deutsche Äußerungen, Latenz bis zur ersten Antwort, Unterbrechungen, Überlast | Nicht auf realer Hardware gemessen |
| G. CRM-Fachprozess | Nachweisbare Lead-/Termin-/Bestandsaktion mit Originalquittung aus dem autorisierten System | Nicht end-to-end nachgewiesen |
| H. PSTN, falls angefordert | Produktive Asterisk-ARI-/SIP-Trunk-Verbindung und tatsächlich angenommener Anruf | Nicht nachgewiesen; Carrier nicht vorhanden |
| I. Compliance und Recovery | Datenschutz, Löschung/Retention, Audit, Backup/Restore, Freigaben und Störfallprozedur | Betrieblich noch abzunehmen |
| J. ChatGPT-Anruf | Von dieser ChatGPT-Oberfläche verbundene, authentisierte Schreibaktion | Noch keine Connector-Anbindung |

## Betriebs- und Rollback-Regeln

- Der Host richtet das bereits vorhandene Call-Control- und
  LiveKit-Setup ein, bevor neue Anrufe möglich sind.
- Vor einer Migration der Call-Control-Datenbank einen nachweisbar
  wiederherstellbaren Snapshot unter bestehendem Datenschutz-/Backup-
  Regime erzeugen; keine Modelle, Tokens oder personenbezogenen Daten
  in GitHub oder CI-Artefakten speichern.
- Nur eine klar identifizierte Testidentität mit dokumentierter
  Einwilligung für den Pilot zulassen. Danach auf begrenzte reale
  Benutzergruppen ausweiten und Ressourcenverbrauch messen.
- Bei Fehlern zuerst die Outbound-Freigabe und den Worker stoppen, dann
  die bestehende Call-Control-/LiveKit-Verbindung und Datenbank sichern.
  Alte, bereits DISPATCHING gewesene Aufträge niemals erneut automatisch
  wählen; nach Absturz stehen sie auf UNKNOWN, bis sie geklärt sind.
- Bei einem fehlerhaften Upgrade die letzte getestete Version starten;
  der Worker darf wegen eines Rollbacks keine fremden/alten Genehmigungen
  neu ausführen. Schema-Downgrades nicht ohne Datenmigration erzwingen.
- HTTP 503/readyz, unbekannte Anrufzustände, Störungen bei
  Tokenrotation/Medienbereinigung und echte Latenzmessungen gehören in
  das Host-Monitoring. Die Quellcode-Tests sind kein Ersatz für Alarme.

## Kein grünes Etikett ohne realen Nachweis

Produktion bedeutet nachgewiesene Erreichbarkeit, Sicherheit, fachlich
korrektes Verhalten unter Last und klaren Betrieb. Solange der
VPS- und Smartphone-Zugang nicht für die Abnahme verbunden ist, kann
hier ausschließlich das Repository entwickelt und getestet werden.
Keine öffentliche E.164-Telefonie ohne echten Carrier/SIP-Anschluss.
