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

Die bestehende signierte WhatsApp-Eingangsschnittstelle auf
`127.0.0.1:8873` wird mit einem begrenzten HTTP-Server betrieben:
standardmäßig maximal 64 gleichzeitige Verbindungen, 10 Sekunden
Socket-Timeout und sofortiges HTTP 503 bei voller Kapazität. Die
TonnEstate-Outbox behält abgewiesene Ereignisse für den späteren Replay;
unter Last werden keine zusätzlichen unbegrenzten Python-Threads erzeugt.
CI prüft den echten HTTP-503-Grenzfall mit einer blockierten Verbindung
und die Annahme nach Freigabe des Kapazitätsslots. Ein realer
VPS-Lasttest bleibt erforderlich.

## Zusätzliche Korrekturen des Kernprozesses

Die ausgehende Telefonie erkennt jetzt die eindeutige, vom Host
bestätigte Einwilligungsreferenz als Idempotenznachweis an. Derselbe
Einwilligungsvorgang kann auch bei 30 parallelen Wiederholungen nicht
mehrere Anrufberechtigungen erzeugen. Versucht der Host, dieselbe
Referenz einer anderen Empfänger- oder Agentenidentität desselben
Mandanten zuzuordnen, wird der Vorgang abgelehnt. Unverarbeitete
Freigaben sind standardmäßig auf 64 pro Agent und dauerhaft gespeicherte
Aufträge auf 10.000 pro Agent begrenzt; damit kann eine fehlerhafte
Host-Integration die Warteschlange nicht unbegrenzt auffüllen.

Der Browser-Client erzwingt für externe Call-Control-Server HTTPS,
untersagt Credentials in URL-Bestandteilen und automatische
Weiterleitungen, setzt ein anpassbares HTTP-Timeout (1 bis 30 Sekunden)
und behandelt auch leere 503-Überlastantworten korrekt. Bei unklaren
POST-Ergebnissen wird die ursprüngliche Anruf-ID erneut verwendet,
anstatt versehentlich zwei Einladungen zu erzeugen. Die bestehende
PWA kann die nur für diese Browsersitzung benötigte, nicht geheime
Anruf-ID in sessionStorage aufbewahren; Identitätstoken und
Gesprächsinhalte werden dort nicht abgelegt.

Für die Call-Control-Datenbank ist SQLite-WAL mit voller
Synchronisationssicherheit aktiviert. Das reduziert die Blockierung
gleichzeitiger Leser und Schreiber; die fachlichen
Transaktions-/Autorisierungsgrenzen bleiben unverändert. Das
Reporting gibt seine schreibende Wartungstransaktion vor der eigentlichen
SQL-Tagesaggregation frei. Vorhandene Call-Control-Datenbankdateien
mit öffentlichen Gruppen-/Weltberechtigungen oder Symlink-Umleitungen
werden beim produktiven Serverstart abgelehnt.

Tests prüfen Wiederholung nach Neustart, parallele
Einwilligungsversuche, Queue-Kapazität, Browser-Netzausfälle,
HTTP-503-Antworten, WAL-Lesekonkurrenz und Dateiberechtigungen.
Keiner dieser Quellcode-Tests ersetzt einen echten Anruf mit dem
verbundenen Smartphone und den installierten Sprachmodellen.

## Betrieb: Release-Gate auf dem echten Host

Nach Installation in der bestehenden Python-Umgebung und Überprüfung der
privaten Umgebungsdateien:

    porcelainberryme-release-gate --network

Die Standardausführung liefert selbst bei vollständig bestandener
Konfigurations- und Laufzeitprüfung den Exitcode 3, solange die reale
Produktionsabnahme noch nicht nachgewiesen wurde. Dadurch kann eine
normale CI diesen Check nicht als automatische Freigabe verwenden.
Für ausdrücklich technische Staging-Prüfungen ohne Produktivfreigabe:

    porcelainberryme-release-gate --network --preflight-only

Nur dieses bewusst gewählte Preflight-only-Verfahren kann mit Exitcode 0
abschließen, sofern die automatisierbaren Tests bestanden sind.
Fehlende Konfiguration bzw. Laufzeitdienste liefern Exitcode 2.
Exitcode 0 im Preflight-only-Modus darf nicht als Nachweis für einen
angenommenen realen Telefonanruf verwendet werden.

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
Bei öffentlicher Telefonie `--require-pstn` setzen. Zusätzlich zum
Vorhandensein einer strikt konfigurierten Stasis-App/Dialplan-Route
prüft der Release-Check dann die authentisierte lokale Asterisk-REST-API
und die registrierte Stasis-Anwendung mit zwei lesenden ARI-Abfragen.
Die reale SIP-Trunk-/Carrier-Abnahme bleibt ein getrennter Nachweis.

Ein Status `configuration_passed=true` bzw.
`runtime_checks_passed=true` bedeutet nur, dass die technischen
Vorbedingungen überprüft wurden. `production_accepted` bleibt
absichtlich **false**, bis reale Host-/Sprach-/Telefonieabnahmen und
ein fachlicher Freigabeprozess außerhalb dieses Source-Audits
nachgewiesen sind.

## Sprachlatenz und Betriebstelemetrie

Der vorhandene VoiceTurnEngine sammelt je Gespräch ausschließlich
begrenzte, numerische Laufzeitstichproben (maximal 64 pro Kategorie):

- Zeit für lokale STT-Verarbeitung eines erkannten Gesprächsabschnitts.
- Zeit vom abgeschlossenen gesprochenen Input bis zur ersten vom
  Medienadapter bestätigten Audioausgabe. Dies ist **keine** Garantie,
  dass ein Mensch diese Ausgabe tatsächlich gehört hat.
- Zeit bis zum Ende eines vollständig ausgelieferten Antwortturns.
- Anzahl erfolgreich abgeschlossener Turns, verarbeiteter Fehler und
  Signale einer Eingabeunterbrechung.

Zu jeder Verteilung werden p50, p95 und Maximum in Millisekunden gebildet.
Der Snapshot enthält keine Namen, Telefonnummern, Call-IDs, Originaltexte,
Medieninhalte oder Modell-Prompts. Standardmäßig werden solche Messungen
**nicht** geloggt. Für eine kontrollierte Abnahme kann der Host
`PB_VOICE_METRICS_LOG=1` setzen: LiveKit- und Asterisk-Worker
schreiben dann genau ein strukturiertes, bereinigtes JSON-Ereignis pro
geschlossener Sprachsitzung in den bestehenden Fehlerausgabestrom bzw.
das Systemjournal. Es wird kein Telemetrie-SaaS angebunden.

Die Akzeptanzschwellen (deutsche Verständlichkeit, zulässige
p95-Antwortzeit, CPU/RAM, Überlast und Abbrüche) müssen durch einen
realen Referenztest auf der vorhandenen Hardware mit dem vorgesehenen
Sprachmodell festgelegt und nachgewiesen werden. CI-Fake-Modelle
reichen dafür nicht aus.

## Verbindliche Abnahmekriterien

| Gate | Nachweis | Derzeit |
| --- | --- | --- |
| A. Codeintegrität | Python 3.11/3.12, Browser, MCP-SDK sowie Asterisk-Stasis/ARI/PCM-E2E im lokalen HTTP/WebSocket-Test | Implementiert, nach jeder Änderung erneut prüfen |
| B. Medien-Pipeline | Echter LiveKit-WebRTC-Audiotransport mit synthetischen lokalen Modellen | In GitHub CI nachgewiesen; kein echter Sprachmodell-Nachweis |
| C. Betrieb | Privater DB-Pfad, wiederanlaufbarer Worker, Health/Ready, verifizierte Signierschlüssel | Code vorhanden; reale Serviceinstallation offen |
| D. Sicherheit | Authentisierte Aufnahme, Mandantentrennung, DND, Opt-in, Tokenrotation, Rücknahme | Source-Tests vorhanden; produktiver Penetrationstest offen |
| E. Smartphone | iPhone/Android im Hintergrund, Push → sichtbarer Anruf → Mikrofon → beidseitige Audioantwort | Nicht nachgewiesen |
| F. Sprachqualität | Reale deutsche Äußerungen, Latenz bis zur ersten Antwort, Unterbrechungen, Überlast | Nicht auf realer Hardware gemessen |
| G. CRM-Fachprozess | Nachweisbare Lead-/Termin-/Bestandsaktion mit Originalquittung aus dem autorisierten System | Nicht end-to-end nachgewiesen |
| H. PSTN, falls angefordert | Produktive Asterisk-ARI-/SIP-Trunk-Verbindung und tatsächlich angenommener Anruf | Nicht nachgewiesen; Carrier nicht vorhanden |
| I. Compliance und Recovery | Datenschutz, Löschung/Retention, Audit, Backup/Restore, Freigaben und Störfallprozedur | Betrieblich noch abzunehmen |
| J. ChatGPT-Anruf | Von dieser ChatGPT-Oberfläche verbundene, authentisierte Schreibaktion | Noch keine Connector-Anbindung |

## Geschützte persistente Eingangs-Datenbank

Der bestehende mehrkanalige `Ledger` verweigert jetzt bereits beim Öffnen
vorhandene Symlinks, mehrfach verlinkte Dateien, fremden Besitz oder für
Gruppe/Andere lesbare bzw. schreibbare SQLite-Dateien. Auch die laufende
Verbindung prüft den Dateistatus vor jeder Datenbankoperation erneut.
Anders als zuvor werden unsichere vorhandene Dateien **nicht**
automatisch durch `chmod 0600` verändert. Diese Verweigerung ist
beabsichtigt: Der Hostoperator muss Eigentümer, Dateipfad und
Berechtigungen prüfen und gegebenenfalls außerhalb des Prozesses
korrigieren, bevor der Listener wieder gestartet wird. Ein Schreibfehler
beim signierten Ingress führt zu HTTP 503, nicht zur stillen Annahme.
CI-Tests prüfen Symlinks, Hardlinks, Rechteänderung und Austausch der
Datei während des Prozesses sowie den nachfolgenden Wiederanlauf.
Das ersetzt keine Bewertung der übergeordneten Host-Verzeichnisrechte
oder einen tatsächlichen Backup-/Restore-Test.

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

## WhatsApp-MCP: separate Produktionsabnahme

Die vorhandene persönliche WhatsApp-Verbindung wird ausschließlich über den
bereits existierenden TonnEstate/Aurora/n8n-Transport verwendet. Die neue
Quellcode-Abnahme enthält signiertes Loopback-Ingress, private Inbox,
MCP-Draft-Erstellung, explizite lokale Eigentümerfreigabe, einmaligen
idempotenten Versand über den Originaladapter, Widerruf, Quarantäne- und
Aufbewahrungsverträge sowie ein unabhängiges, nicht mutierendes
Host-Preflight. Die `tests/test_whatsapp_e2e.py`-Integration benutzt einen
echten lokalen HTTP-Listener und simuliert ausschließlich den externen
Provider-HTTP-Antwortteil. Sie stellt **keine** tatsächlich zugestellte
WhatsApp-Nachricht dar.

Vor einer Live-Freigabe sind weiterhin die echten Host-Prüfungen unter
`docs/WHATSAPP_MCP.md` erforderlich: korrekte 0600-Eigentümerdateien,
MCP-/Call-Control-Identität, TonnEstate-Outbox-/Worker-Heartbeat,
n8n-Wiederholung von HTTP 503, absichtlich unterbrochener und wieder
aufgenommener End-to-End-Eingang, explizite Eigentümerfreigabe eines
Testversands und Bestätigung des bereits vorhandenen WhatsApp-Providers.
Die privaten TonnEstate-Actions-Jobs haben bisher keine Runner-Schritte
ausgeführt; ein grüner PorcelainberryMe-Lauf ersetzt diesen Nachweis nicht.
Die Freigabe eines benutzerdefinierten Remote-MCP-Zugangs in ChatGPT ist
zusätzlich von dessen Plattformberechtigungen abhängig.

Der Preflight `python -m porcelainberryme.whatsapp_preflight --network
--preflight-only` prüft ausschließlich benannte lokale Voraussetzungen
und gibt weder Inhalte noch Credentials aus. Sein Exitcode 0 darf nicht
als `production_accepted=true` interpretiert werden.
