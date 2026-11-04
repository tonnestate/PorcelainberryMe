# PorcelainberryMe: authentifizierter KI-Rückruf auf die eigene Internet-ID

**Code umgesetzt:** Benutzer klickt in der vorhandenen PWA
„KI soll mich anrufen“. Eine bestehende, signierte HUMAN-Identität
autorisiert die Anrufanforderung nur für die eigene Internet-ID. Der
konfigurierte Agent und der bekannte Mandant sind serverseitig festgelegt.
Die Anrufanforderung wird im vorhandenen dauerhaften Outbound-Journal
idempotent QUEUED; der unabhängige Worker führt sie über die bestehende
CallRegistry-/LiveKit-Sprachpipeline aus. Die PWA nimmt den eingehenden
Anruf weiterhin ausschließlich nach ausdrücklichem Benutzerklick an.

**Keine neue SaaS-Instanz, kein externer LLM, kein Telefonanbieter.**
Echte Smartphone-Erreichbarkeit setzt installiertes PWA-/Push-/TURN-/TLS-
Setup und vorhandene lokale Sprachmodelle voraus. Dieser GitHub-Code
verbindet noch nicht den aktuellen ChatGPT-Chat mit einem MCP-Tool.

## Host-Konfiguration

Die vorhandene Call-Control-Umgebung erhält zusätzlich:

- `PB_CALL_JOBS_DB` = vorhandener, absoluter privater SQLite-Journalpfad
- `PB_CALL_ME_AGENT_ID` = exakt eine zuvor registrierte Agentenidentität

Beide Variablen müssen gemeinsam gesetzt sein. Ohne diese
Host-Konfiguration ist die Call-Me-HTTP-Funktion abgeschaltet
(503, keine Freigabe). Die Call-Control- und Outbound-Worker-
systemd-Einheiten erhalten denselben privaten Journalpfad.
Der Outbound-Worker benötigt weiterhin den bestehenden signierenden
Agentenschlüssel sowie installierte lokale Whisper-/Ollama-/Piper-Modelle.
Die Browser-PWA bekommt diese Geheimnisse niemals.

## Schnittstelle

Für eine signierte menschliche Identität, die selbst beim
Call-Control-Dienst registriert ist, KI-Anrufe erlaubt und DND
deaktiviert hat:

`POST /v1/ip-calls/call-me`

Request JSON: `{"idempotency_key":"uuid32fromUserClick"}`

Es gibt bewusst **kein** `to`, `agent`, `tenant`,
`phone_number` oder `consent_reference` im Request. Diese Werte
werden nicht aus der KI-Antwort oder ungeprüften Browser-Feldern gelesen.
Der Server erzeugt die dauerhaft gebundene Host-Einwilligungsreferenz
aus der authentifizierten Identität und dem stabilen Klick-Request-ID.
Ein verlorener HTTP-Response oder PWA-Reload kann dieselbe Freigabe
niemals versehentlich in zwei unterschiedliche Dial-IDs umsetzen.

Antwort: `porcelainberryme.outbound-job.v1` mit eigenem
`approval_id`, `state` und `revocation_confirmed`.
Kein Empfänger, Bearer, Transkript oder Modell-Prompt wird ausgegeben.

Die zugehörigen Endpunkte sind:

- `GET /v1/ip-calls/call-me?approval_id=<opaque-id>`:
  nur Status der eigenen Freigabe, keine Informationen über andere
  Mandanten/Kontakte.
- `DELETE /v1/ip-calls/call-me` mit
  `{"approval_id":"<opaque-id>"}`: nur noch nicht beanspruchte
  APPROVED/QUEUED-Aufträge können als REVOKED bestätigt werden.
  Bei bereits DISPATCHING lautet der unveränderte Status weiter
  DISPATCHING – der Host muss einen möglicherweise bereits klingelnden
  Anruf über das vorhandene Call-Control-System beenden.

Die Browserbibliothek bietet
`client.requestSelfCall(key)`, `client.selfCallStatus(id)`
und `client.revokeSelfCall(id)`. Die existierende ungestylte
`mountCallConsole`-Oberfläche stellt die Aktionen
„KI soll mich anrufen“ und „KI-Anrufanforderung zurücknehmen“
für angemeldete Menschen bereit. Keine neue CSS-Gestaltung oder
zweite PWA wird benötigt.

## Sicherheit und Wiederanlauf

- Benutzeridentität wird ausschließlich per serverseitig geprüftem
  Bearer-Tokeneintrag ermittelt; der KI-Agent darf sich nicht selbst
  als menschlicher Benutzer anmelden oder Anrufe freigeben.
- DND, Opt-in und Registrierung werden vor der Freigabe kontrolliert
  und vor dem tatsächlichen Wählen erneut durch CallRegistry.originate
  geprüft.
- Ohne akzeptierten Worker-Auftrag gibt es keinen Ring-Versuch;
  Warteschlange, Zustimmung, Einwilligungsrücknahme und Agent-Identität
  sind persistent/mandantengetrennt.
- Ein unsicheres POST-Ergebnis bleibt durch dieselbe Klick-ID
  wiederholbar, ohne neue Berechtigung zu erzeugen.
- Für einen weiteren Anruf nach einem alten Auftrag wird dessen
  tatsächlicher terminaler Journalstatus geprüft; ein noch laufender
  Auftrag wird nicht doppelt gestartet.
- PWA-sessionStorage enthält höchstens eine vorübergehende zufällige
  Klick-ID und eine eigene Freigabe-ID. Es enthält niemals JWT,
  Gesprächsinhalte oder Audio.
- Keine unautorisierte PSTN-Wahl. Öffentliche Telefonnummern bleiben
  an eine getrennte, echte SIP-Netzanbindung gebunden.

## Was noch gegen echte Hardware verifiziert werden muss

Browser-/API-Tests prüfen reale HTTP-Routen, gültige und ungültige
Identitäten, Queue-Zustände, Wiedervorlage und das tatsächlich erzeugte
CallRegistry-RINGING-Ereignis. Diese Tests ersetzen keinen
eingehenden Push auf dem ausgeschalteten Smartphone, keine
angenommene LiveKit-Session mit installierter deutscher Spracherkennung
und keine aktive Hermes- oder ChatGPT-Connectorinstallation.

Für den Betriebsnachweis siehe
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).
