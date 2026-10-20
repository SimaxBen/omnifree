# Email receipts -> expenses (Gmail, Google Workspace, other IMAP mailboxes)

The hub reads purchase receipts from one or more mailboxes and turns them into expenses
(`hub_api/routers/inbox.py`). No Google Cloud project, no billing and no OAuth app are needed: each
mailbox is read over IMAP (TLS, port 993) with an **app password**.

Mailboxes set up for this hub:

| name | address | host | password vault key |
|---|---|---|---|
| `simaxhah` | simaxhah@gmail.com | imap.gmail.com | `INBOX_PW_SIMAXHAH` |
| `ilyas` | ilyasouhnine@gmail.com | imap.gmail.com | `INBOX_PW_ILYASOUHNINE` |
| `bidtndr` | iouhnine@bidtndr.com | imap.gmail.com if the domain is on Google Workspace (see 1c) | `INBOX_PW_BIDTNDR` |

What one import does (`POST /inbox/receipts/sync`):

1. visits the mailboxes least recently synced first (a mailbox the previous run did not reach or finish
   goes first). Each mailbox is isolated: a wrong password or a timeout on one is reported and the
   others are still imported;
2. searches for receipt-looking messages of the last `days` days:
   * Gmail / Google Workspace (`imap.gmail.com`): Gmail's own search (`X-GM-RAW`) in "All Mail":
     `newer_than:7d (receipt OR reçu OR facture OR invoice OR "votre commande" OR "order confirmation" OR ticket OR paiement OR payment) -in:spam`;
   * any other host: standard IMAP `SEARCH SINCE <date>` with subject keywords
     (receipt, facture, invoice, commande, order, ticket, paiement, payment, recu) in INBOX;
3. skips every message it has already seen: imports are deduplicated per (mailbox, `Message-ID`)
   (table `app_inbox_seen`, and `source_ref = "<mailbox>#<Message-ID>"` on the expense, so the same email
   received in two mailboxes is imported once per mailbox);
4. for each new message, newest first (at most `max` per call, default 15):
   * a PDF or image attachment goes to the receipt vision model (same pipeline as "Scan receipt"), a PDF
     the vision model refuses is read from its text instead; the file is kept in the Drive under
     `receipts/email/<yyyy>/<mm>/`;
   * without an attachment, the email body (HTML stripped) goes to a text model, which also decides
     whether the email is a purchase at all (newsletters, shipping notices... are skipped);
5. creates the expenses with `source = "email"` (an "email" badge in the app; the tooltip and the expense
   sheet show the mailbox) and a note holding the subject, sender and mailbox;
6. stops starting new work after ~50 s (the serverless limit is 60 s) and reports what is `remaining`
   (and which mailboxes were "not reached"); the next run continues there.

Mailboxes are opened read-only: messages are never marked as read, moved or deleted.

## 1. Create an app password for each mailbox

### 1a. simaxhah@gmail.com and 1b. ilyasouhnine@gmail.com (personal Gmail)

Do this once per account, signed in to that account. App passwords require **2-Step Verification**.

1. Open <https://myaccount.google.com/security>.
2. Under "How you sign in to Google", turn on **2-Step Verification** if it is off and finish its setup
   (phone prompt, authenticator app or SMS).
3. Open <https://myaccount.google.com/apppasswords> (or search "App passwords" in the account settings).
   If the page says the setting is not available, 2-Step Verification is not on yet or Advanced
   Protection is enabled.
4. Type a name such as `OmniFree hub` and click **Create**.
5. Google shows a 16-letter password (`abcd efgh ijkl mnop`). Copy it now: it is shown only once.
   Spaces do not matter, the hub removes them.
6. IMAP is always on for Gmail accounts since 2025; on older accounts check Gmail > Settings > See all
   settings > Forwarding and POP/IMAP > "Enable IMAP".

### 1c. iouhnine@bidtndr.com (custom domain)

First find out who hosts the mailbox: `nslookup -type=mx bidtndr.com`.

* MX records `aspmx.l.google.com` / `*.googlemail.com` / `smtp.google.com` -> **Google Workspace**:
  host `imap.gmail.com`, port 993. Create the app password exactly as in 1a, signed in as
  iouhnine@bidtndr.com. If "App passwords" is missing, the Workspace admin must allow it:
  Admin console (admin.google.com) > Security > Authentication > 2-step verification: allow users to
  turn it on (then turn it on for the account), and Security > Access and data control > Less secure
  apps / IMAP: Apps > Google Workspace > Gmail > End User Access > "POP and IMAP access" enabled.
* `*.mail.protection.outlook.com` -> **Microsoft 365 / Outlook**: host `outlook.office365.com` (or
  `imap-mail.outlook.com` for outlook.com accounts), port 993. Microsoft 365 tenants often disable basic
  IMAP authentication; an app password only works when the tenant allows it (security defaults off,
  "Authenticated SMTP/IMAP" enabled for the mailbox).
* `mx.zoho.eu` -> **Zoho Mail**: host `imap.zoho.eu`, port 993; enable IMAP in Zoho Mail > Settings >
  Mail accounts > IMAP access, and create an app-specific password in accounts.zoho.eu > Security >
  App passwords.

Other hosts work too: they use the standard IMAP search described above.

To revoke access later, delete the app password where it was created (changing the account password
also revokes Google app passwords).

## 2. Store the mailboxes in the vault

From the repository root, with `OMNIFREE_VAULT_PASSPHRASE` set. The account list is plain JSON without
any password; each password goes into its own key (`set KEY` with no value prompts for it, so it does
not end up in the shell history):

```bash
python -m core.vault set INBOX_ACCOUNTS '[
  {"name": "simaxhah", "user": "simaxhah@gmail.com",     "host": "imap.gmail.com", "port": 993, "password_key": "INBOX_PW_SIMAXHAH"},
  {"name": "ilyas",    "user": "ilyasouhnine@gmail.com", "host": "imap.gmail.com", "port": 993, "password_key": "INBOX_PW_ILYASOUHNINE"},
  {"name": "bidtndr",  "user": "iouhnine@bidtndr.com",   "host": "imap.gmail.com", "port": 993, "password_key": "INBOX_PW_BIDTNDR"}
]'
python -m core.vault set INBOX_PW_SIMAXHAH        # app password of simaxhah@gmail.com
python -m core.vault set INBOX_PW_ILYASOUHNINE    # app password of ilyasouhnine@gmail.com
python -m core.vault set INBOX_PW_BIDTNDR         # app password of iouhnine@bidtndr.com
```

For `iouhnine@bidtndr.com` on another provider, change its `host` (for example `imap.zoho.eu` or
`outlook.office365.com`) and `port`. `host` defaults to `imap.gmail.com`, `port` to 993, `name` to the
address. Never put a `"password"` inside the JSON: the hub ignores it and reports it as a problem.

Single-mailbox setups from before still work: `GMAIL_IMAP_USER` + `GMAIL_APP_PASSWORD` is read as one
more Gmail account named `gmail` (skipped when the same address is already in `INBOX_ACCOUNTS`).

Values can also be given as environment variables of the same names (env wins over the vault).
Redeploy the hub so the new vault is picked up, then check:

```bash
curl -H "Authorization: Bearer $HUB_TOKEN" https://api.ilyasouhnine.tech/inbox/receipts/status
```

```json
{"configured": true, "problems": [], "last_sync_at": null, "last_result": null,
 "accounts": [{"name": "simaxhah", "user": "simaxhah@gmail.com", "host": "imap.gmail.com", "port": 993,
               "password_key": "INBOX_PW_SIMAXHAH", "configured": true, "last_sync_at": null,
               "last_attempt_at": null, "last_error": null, "last_result": null}, ...]}
```

`configured: false` on an account means its password key is not set; `problems` lists mistakes in
`INBOX_ACCOUNTS` (invalid JSON, missing `user`, duplicate names, a password inside the JSON).

## 3. Import on demand

In the app: More > Expenses > **Import from email**. The sheet shows each mailbox (added / left / its
error), the new expenses, the skipped emails grouped by reason, and how many are left for another run.

From a shell:

```bash
curl -X POST -H "Authorization: Bearer $HUB_TOKEN" -H "Content-Type: application/json" \
     -d '{"days": 7, "max": 15, "dry_run": true, "account": "bidtndr"}' \
     https://api.ilyasouhnine.tech/inbox/receipts/sync
```

Body fields (all optional, the body itself is optional too): `days` (1-90, default 7), `max` (1-50,
default 15 new emails per call, across mailboxes), `dry_run` (default false: read and extract, but store
nothing and do not mark emails as processed), `account` (a name or address: sync only that mailbox;
unknown -> 404). Reply:

```json
{"scanned": 12, "matched": 3, "created": [/* Expense */],
 "skipped": [{"account": "ilyas", "message_id": "...", "reason": "not a receipt"}],
 "errors": [{"account": "bidtndr", "message_id": null, "error": "login failed: check the address and the app password (IMAP must be enabled)"}],
 "remaining": 0, "dry_run": false, "days": 7, "elapsed_s": 14.2,
 "accounts": [{"name": "simaxhah", "user": "simaxhah@gmail.com", "scanned": 7, "created": 2, "skipped": 5,
               "remaining": 0, "error": null, "complete": true}, ...]}
```

Skip reasons: `already imported`, `already processed` (seen before, not a receipt), `not a receipt`,
`no amount found`, `unreadable`, `empty message`. A message that fails (LLM or storage error, timeout) is
reported in `errors` and retried on the next run. No mailbox with a password answers **409**.

## 4. Run it every day (QStash schedule)

The sync endpoint accepts an empty POST, so a scheduler can call it as is.

In the app: More > **Schedules** > New schedule:

1. Tap **Template: import email receipts daily**. It fills in:
   * name `gmail-receipts`, schedule **Daily 08:00 UTC** (`0 8 * * *`), method `POST`;
   * target `https://api.ilyasouhnine.tech/inbox/receipts/sync` (your hub address from Settings);
   * body `{"days": 2, "max": 15}` (two days of overlap: a missed day is caught up, duplicates are skipped);
   * **Send my hub token** switched on: the schedule carries `Authorization: Bearer <your hub token>`.
2. Pick the `upstash_qstash` provider and tap **Create schedule**.
3. Tap the play button on the new schedule to run it once and check the result in Expenses.

The same with the API (QStash forwards the headers to the hub as `Upstash-Forward-*`):

```bash
curl -X POST -H "Authorization: Bearer $HUB_TOKEN" -H "Content-Type: application/json" \
  https://api.ilyasouhnine.tech/cron/jobs -d '{
    "provider": "upstash_qstash", "name": "gmail-receipts", "schedule": "0 8 * * *",
    "target": "https://api.ilyasouhnine.tech/inbox/receipts/sync", "method": "POST",
    "headers": {"Authorization": "Bearer <HUB_TOKEN>", "Content-Type": "application/json"},
    "body": "{\"days\": 2, \"max\": 15}"}'
```

One schedule covers all three mailboxes. With three mailboxes and a busy day, one call may not read
everything within its ~50 s: add a second schedule a little later (for example `30 8 * * *`), it picks
up where the first stopped. The hub token is stored in the QStash schedule (the hub masks it in its own
responses); if you rotate the hub token, recreate the schedule.

## Limits and privacy

* Up to 100 newest matching emails are considered per mailbox and call; attachments above 10 MB are
  ignored, and so are inline images under 10 KB (logos, tracking pixels).
* Email text (up to 12 000 characters) and attachments are sent to the configured LLM providers
  (Gemini first for images and PDFs). Free tiers may use prompts to improve their products: do not add a
  mailbox whose receipts you would not share with them (check your employer's policy for the
  bidtndr.com mailbox).
* `GET /inbox/receipts/status` shows, per mailbox, the last successful sync, the last attempt and the
  last error, plus the last run overall.
