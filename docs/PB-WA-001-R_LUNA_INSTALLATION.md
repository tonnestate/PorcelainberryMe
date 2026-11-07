# PB-WA-001-R – verbindlicher Installationsauftrag für Luna

**Priorität: Abschluss der bestehenden WhatsApp-MCP-Integration. Nicht nur prüfen: tatsächlich installieren, anschließen, testen und protokollieren.**

**Ausgangslage (VPS-Abnahme 2026-11-07): NICHT BESTANDEN.** TonnEstate API/CRM, n8n, bestehende WhatsApp-Bridge und TonnEstate MCP laufen bereits. PorcelainberryMe ist auf dem VPS nicht installiert; Loopback-Ports 8873/8874 sind unbelegt. Die TonnEstate-Arbeitskopie ist auf `fix/propstack-gaps-2026-10-24` (lokal `4b2b6a0…`) statt der geprüften Integrations-Baseline. Im aktiven n8n-Workflow **TONNAIR Aurora WhatsApp Reply** haben die beiden relevanten HTTP-Nodes `retryOnFail=false`. Die bisherige Abnahme hat **nichts** installiert oder verändert.

Dieser Auftrag ist **PB-WA-001-R (Recovery/Integration)**. Der vorherige Auftrag PB-WA-001 war faktisch nur eine Bestandsprüfung. **Nicht erneut nur einen Prüfbericht erstellen.** Der Auftraggeber verlangt einen nachweisbar laufenden Nachrichteneingang und eine kontrolliert freigegebene Ausgangsnachricht über den *bestehenden* WhatsApp-Provider.

## Verifizierte Quellstände

- `tonnestate/PorcelainberryMe`, `main`, Commit **`0aecb39e7e5111a6c03a04defef2f3efbf5f29ab`** (private WhatsApp-Datenbanken und Freigaben gehärtet).
- `tonnestate/TonnEstate`, `import/vps-baseline-2026-10-22`, Commit **`0014dea6cee4d91006970ee6d96ea5483724fbe0`** (Owner-Mirror-Outbox, Replay, Health, Dateisicherheitsprüfung).
- Quellcode- und Integrations-CI sind bestanden. **Kein VPS-/Live-Nachweis** durch CI.

**Verboten:** bestehende TonnEstate-Arbeitskopie blind auf einen anderen Branch setzen, fremde Commits überschreiben, laufende WhatsApp-Bridge duplizieren, einen zweiten WhatsApp-Login/Consumer starten, Zugangsdaten in Git/CI/Protokolle kopieren oder eine WhatsApp-Nachricht ohne explizite Freigabe versenden.

## Ausführungsreihenfolge – jeder Schritt ist eine Arbeit, nicht nur eine Frage

### 0. Betriebsabsicherung und Versionskonflikt lösen

1. Auf dem VPS laufende `systemctl`-Units, Dienstidentitäten, Prozesspfade, Python-Versionen und belegte Ports ermitteln. Die vom Abnahmeprotokoll identifizierten n8n- und WhatsApp-Bridge-Pfade **unangetastet lassen**.
2. Vor jeder Änderung Sicherungen von aktiver TonnEstate-Arbeitskopie, relevanter n8n-Workflow-Version, bestehenden Dienstdateien, Env-Konfigurationen und betroffenen Datenbanken erstellen; Wiederherstellbarkeit und Rollback festhalten. Secrets nicht in Berichte übernehmen.
3. Die **tatsächlich verwendeten** TonnEstate-Dateien mit dem angepinnten Commit vergleichen: `tonnestate-crm/app/api/aurora_whatsapp_internal_api.py`, `tonnestate-crm/app/services/porcelainberry_whatsapp_mirror.py`, `tonnestate-crm/app/services/porcelainberry_whatsapp_outbox.py` sowie die drei Mirror-Systemd-Units. Existierende andere CRM-Änderungen bleiben erhalten. Erforderliche WhatsApp-Änderungen gezielt übertragen und testen, nicht das gesamte Repository ersetzen. Bei Konflikt den exakten Diff/Fehler dokumentieren; kein stilles Überspringen.
4. Nach dem Abgleich nachweisen, dass **der laufende** TonnEstate-API-Prozess die richtige Route und den Mirror-Hook enthält; der Git-Branchname allein reicht nicht.

### 1. PorcelainberryMe tatsächlich installieren

1. Auf dem VPS die PorcelainberryMe-Quellen aus dem angepinnten `main`-Commit in einer **separaten** Arbeitskopie installieren. Der in `deploy/systemd/*.example` dokumentierte Referenzpfad ist `/opt/porcelainberryme`; vor Anlegen/Überschreiben prüfen, ob er frei ist. Keine TonnEstate-Verzeichnisse ersetzen.
2. Einen dedizierten unprivilegierten Dienstaccount `porcelainberryme`, eine passende Python-3.11+-Virtualenv und die Anwendung einschließlich optionaler `mcp`-Abhängigkeit einrichten. Nur tatsächlich erforderliche Abhängigkeiten installieren, keine zusätzlichen WhatsApp-Provider oder Modelle.
3. Privaten State unter dem geprüften Hostpfad (Vorlage: `/var/lib/porcelainberryme`) einrichten; Verzeichniszugriff nur für den Dienstaccount, SQLite-Dateien **0600**, weder Symlinks noch Hardlinks.
4. Die geprüften Templates `deploy/systemd/porcelainberryme-inbound.service.example` und `deploy/systemd/porcelainberryme-call-control.service.example` auf die tatsächliche Umgebung anpassen, als Systemd-Dienste installieren und kontrolliert starten. Die Instanz muss nach Neustart dauerhaft laufen. Die HTTP-Dienste dürfen **nur** auf `127.0.0.1:8873` (Ingress/Mirror) und `127.0.0.1:8874` (authentifizierte Call-Control-Identität) lauschen.
5. Für den Inbound-Dienst `PORCELAINBERRY_DB`, `PORCELAINBERRY_SIGNING_SECRET`, `PB_WHATSAPP_MIRROR_ENABLED=1`, `PB_WHATSAPP_MIRROR_SIGNING_SECRET`, `PB_WHATSAPP_MIRROR_TENANT` und `PB_WHATSAPP_MIRROR_DESTINATION` setzen. Der Mirror-Schlüssel muss eigenständig und mindestens 32 Zeichen lang sein; er ist **nicht** der allgemeine Inbound-Schlüssel.
6. Für Call-Control `PB_CALL_IDENTITY_SECRET` (mindestens 32 Zeichen) und einen privaten absoluten `PB_CALL_DB` bereitstellen. Wenn WhatsApp-MCP verwendet wird, ist die in `/v1/ip-calls/whoami` verifizierte Eigentümeridentität zwingend erforderlich. Call-Control wird **nicht** als Beleg für produktive Telefonie gewertet.

**Gate 1:** Beide Systemd-Dienste laufen, beide Loopback-Ports sind lokal erreichbar, und `GET http://127.0.0.1:8873/v1/owner-whatsapp-mirror/readyz` liefert HTTP **200** mit `status=ready` und `scope=owner_whatsapp_mirror`. HTTP 404/503 oder bloßes `/healthz` sind **kein** Erfolg.

### 2. TonnEstate-Mirror-Outbox und n8n anschließen

1. Die eigentlichen Eigentümer-IDs **aus der TonnEstate-Datenbank** ermitteln (`User.tenant_id`, `User.user_id`), nicht aus WhatsApp-Payloads und nicht aus Schätzungen. Diese Werte müssen mit `PB_WHATSAPP_MIRROR_TENANT` und `PB_WHATSAPP_MIRROR_DESTINATION` übereinstimmen.
2. Unter der echten TonnEstate-API-Dienstidentität (`www-data`, nach VPS-Befund) `TE_PB_WHATSAPP_MIRROR_ENABLED=1`, `TE_PB_WHATSAPP_OWNER_TENANT`, `TE_PB_WHATSAPP_OWNER_ID`, einen absoluten privaten `TE_PB_WHATSAPP_OUTBOX_DB` und `TE_PB_WHATSAPP_SIGNING_SECRET_FILE` setzen. Das Secret-File muss dem ausführenden Benutzer gehören, **0600** sein und denselben Schlüssel enthalten wie der PB-Mirror (nicht den allgemeinen PB-Inbound-Schlüssel). Der Datenträger und Besitzer der Outbox müssen vor Start geprüft werden.
3. Die vorhandenen Vorlagen unter `tonnestate-crm/deploy/` verwenden: `tonnestate-whatsapp-mirror-outbox.service`, `tonnestate-whatsapp-mirror-health.service` und `tonnestate-whatsapp-mirror-health.timer`. Arbeitsverzeichnis, Python-Pfad und Schreibrechte vor Installation am tatsächlich laufenden System validieren. Danach Worker, Timer und den benötigten TonnEstate-API-Prozess kontrolliert aktivieren/neu starten.
4. Im aktiven n8n-Workflow **TONNAIR Aurora WhatsApp Reply** die zwei betroffenen HTTP-Nodes gezielt reparieren. Zuvor Workflow als versionsgebundenes Backup exportieren. `retryOnFail=false` darf für den dauerhaften Ingress nicht bestehen bleiben, wenn das System HTTP 503 zurückgibt. Nachweisen, dass die *originale* Provider-`message_id` im Retry unverändert ist, ein temporärer 503 einen begrenzten Wiederholungsversuch auslöst und keine stille `continueOnFail`-/Erfolgskante Nachrichten verliert. Keine unkontrollierten Mehrfachantworten erzeugen; Ursprungstransport und vorhandene Aurora-Automatik erhalten.
5. Das tatsächliche Provider-Eingangsformat der n8n-Nodes abgleichen (`message_id` stabile Zeichenkette; `sender` direkter Kontakt mit `@c.us`; `message` Text; `conversation_id` Zeichenkette; `from_me` nur boolesch `true` bedeutet ausgehend). Anweichungen am richtigen Normalisierungspunkt beheben und kontrolliert testen.
6. **Nicht** den alten inaktiven WhatsApp-Workflow parallel aktivieren; keine weitere eingehende WhatsApp-Sitzung.

**Gate 2:** TonnEstate-Outbox-Worker und Health-Timer laufen; `python -m app.services.porcelainberry_whatsapp_outbox status` und `... check --max-delay 300` liefern unter dem richtigen Service-Account und dessen Environment einen gesunden Zustand. Ein echter absichtlich provozierter HTTP-503 darf die Originalnachricht nicht verlieren.

### 3. Owner-MCP mit unabhängiger Freigabe starten

1. Den bestehenden `porcelainberryme-mcp`-**stdio**-Server in den **authentifizierten** FruitStack-/Hermes-Hostprozess integrieren; nicht als öffentlich erreichbaren MCP-HTTP-Server und nicht als zweiten WhatsApp-Client starten. Der stdio-Prozess braucht einen wirklichen MCP-Host/Client.
2. Für dieselbe fest verifizierte Identität `PB_MCP_TENANT`, `PB_MCP_SUBJECT`, `PB_MCP_ROLE=agent`, `PB_MCP_CALL_TOKEN_FILE`, `PB_MCP_WHATSAPP_ENABLED=1`, `PB_MCP_WHATSAPP_OWNER_TENANT`, `PB_MCP_WHATSAPP_OWNER_AGENT`, `PB_MCP_WHATSAPP_OWNER_SENDER`, `PB_MCP_WHATSAPP_APPROVALS_DB`, `PB_MCP_WHATSAPP_TOKEN_FILE`, `PB_MCP_WHATSAPP_INBOX_DB` und `PB_MCP_WHATSAPP_OWNER_DESTINATION` setzen. Der Owner-Agent ist das MCP-Subjekt; das Mirror-Ziel ist die echte TonnEstate-`User.user_id`. Diese Werte sind **nicht automatisch identisch**.
3. Einen authentisierten Agent-Identitätstoken über die lokale Call-Control ausstellen und als private, ggf. rotierbare Token-Datei bereitstellen. Den vorhandenen TonnEstate-API-Bearer ausschließlich über den Host-Secret-Manager in eine für den PB-Dienstaccount eigene private **0600** Token-Datei geben; Besitzerkonflikte zwischen `www-data` und `porcelainberryme` nicht durch `chmod 644` umgehen.
4. Die private Freigabe-/Entwurfsdatenbank durch die bestehenden Owner-MCP-Klassen initialisieren. Den Eigentümer-Prüf-/Freigabeweg `python -m porcelainberryme.whatsapp_owner_consent` nach `docs/WHATSAPP_MCP.md` bereitstellen. **Nur** eine tatsächliche interaktive Eigentümerbestätigung darf einen Sendeentwurf freigeben.
5. MCP-Tool-Discovery und die vier WhatsApp-Operationen prüfen: Inbox, Prepare, Send, Send Status. Ein direkter Aufruf eines lokalen stdio-MCP aus dieser ChatGPT-Sitzung ist damit **noch nicht** automatisch möglich. Remote-ChatGPT-Anbindung nur als **separat nachgewiesenes** zulässiges Produkt-Setup behandeln.

**Gate 3:** `python -m porcelainberryme.whatsapp_preflight --network --preflight-only` liefert Exit **0** und alle angeforderten Prüfungen PASS. Dieser Erfolg ist nur technische Bereitschaft, nicht Live-Abnahme.

### 4. Reale Abnahme und Freigabe

Nur mit ausdrücklicher Zustimmung des Eigentümers und einem freigegebenen Testkontakt:

1. Reale eingehende WhatsApp-Testnachricht durch vorhandene Bridge → n8n → TonnEstate `/api/internal/v1/aurora/reply` → persistente Outbox → signierten PB-Ingress → private MCP-Inbox verfolgen. Mit echten Provider-IDs belegen (im Bericht nur redigierte Kennungen).
2. Identische Provider-ID erneut zustellen: **exakt ein** Eintrag in der Owner-Mirror-Inbox; die normale Agent-Eingangsqueue bleibt unverändert.
3. Fremde Tenant-/Owner-Identität, falsches Signing Secret und Gruppenabsender verweigern; keine Einsicht in private Nachrichten.
4. Einen Dienst gezielt stoppen, während eine **autorisierte** Testnachricht eingeht: Outbox bleibt PENDING, nach Neustart Replay → ACKED, kein Verlust und kein Duplikat. Gleichzeitig n8n-Verhalten bei HTTP 503 mit einem kontrollierten Test beobachten.
5. Freigegebenen MCP-Entwurf vorbereiten. Ohne Eigentümerfreigabe **kein Versand**. Danach lokale interaktive Bestätigung und einmaligen Versandversuch über die **bestehende** TonnEstate-Aurora-Bridge prüfen. `ACCEPTED` ist nur Provider-Annahme, **nicht** zugestellt/gelesen; tatsächlich erfassten Providerbeleg gesondert dokumentieren.
6. Service-Restart, Überwachung, Retention, fehlende/falsche Dateien, Alarm bei Replay-Rückstau, Datenbankbackup und **getestetes Restore** nachweisen.
7. Erst nach allen Tests den Status **BESTANDEN** vergeben. Ansonsten **NICHT BESTANDEN** mit präzisem technischem Restblocker, letzter tatsächlich ausgeführter Aktion, Beleg und nächstem konkret ausführbarem Arbeitsschritt.

## Unbedingt abzuliefern

- Exakte **laufende** Softwareversionen und Systemd-Unit-Namen, keine erfundenen Deployments.
- Nachweis, dass 8873 und 8874 lokal lauschen, Ready-/Whoami-Prüfung und Outbox-Worker-Heartbeat erfolgreich sind.
- Export/Diff des aktiven **geänderten** n8n-Workflows mit belegtem 503-Retry.
- Redigiertes, echtes Eingangs-/Replay-/Ausgangs-Abnahmeprotokoll mit Belegquelle.
- Reproduzierbarer Rollback und Backup-/Restore-Nachweis.
- Aussage, welcher Teil **wirklich** installiert und welcher nur im Git-Repository implementiert ist.

**STOP-Bedingung:** Falls VPS-/n8n-Adminzugriff, notwendige Eigentümeridentitäten oder Freigabe zum Live-Test fehlen, exakt diese Abhängigkeit nennen. Alles unabhängig davon Installierbare dennoch umsetzen; niemals eine reine Analyse als abgeschlossene Integration darstellen.
