# Microsoft 365 mailbox integration

This optional staging integration lets the two roles connect either separate company
mailboxes or one shared personal forwarding inbox, import new mail from one selected
folder per role, and create TranNNB outputs without ever sending mail.

## Safety contract

- A company-tenant deployment requests `Mail.Read` for Ngan and `Mail.ReadWrite` for
  Tran; the latter is used only for Graph `createReplyAll`.
- A personal forwarding inbox (`consumers`) also requests `Mail.Read` for Ngan and
  `Mail.ReadWrite` for Tran. Tran uses write access only to create a standalone,
  recipient-free, unsent draft in the personal mailbox; it cannot join the company
  thread.
- The application never requests `Mail.Send`.
- Each Connect action includes `prompt=select_account`, so Ngan and Tran can choose
  different signed-in accounts.
- There is no send route and no Graph `/send` call.
- Sync only reads raw MIME. It never marks read, moves, deletes, labels, or modifies a
  source message.
- OAuth access/refresh tokens stay inside a server-side MSAL token cache. They are not
  stored in cookies, files, SQLite, logs, or API responses.
- The browser cookie contains only a random opaque session ID. It is `HttpOnly`,
  `SameSite=Lax`, uses path `/`, and is `Secure` when the configured callback is HTTPS.
- OAuth state is one-time and expires after 10 minutes. Browser sessions expire after
  8 hours and the process retains at most 512 sessions.
- All M365 API responses, including errors, use `Cache-Control: private, no-store`.
  The OAuth callback additionally uses `Referrer-Policy: no-referrer`.

Microsoft references:

- [MSAL Python authorization-code flow](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens)
- [Create a Reply-All draft](https://learn.microsoft.com/en-us/graph/api/message-createreplyall?view=graph-rest-1.0)
- [Create a standalone message draft](https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0)
- [Incremental folder message sync](https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0)
- [Get raw MIME content](https://learn.microsoft.com/en-us/graph/outlook-get-mime-message)

## Entra app registration

Use a confidential web application with one explicit audience:

- a tenant GUID/domain for one company tenant; or
- `consumers` for a dedicated personal Outlook.com/Hotmail mailbox.

The product deliberately rejects `common` and `organizations` so a deployment
cannot silently expand from one intended mailbox audience to every Microsoft
organization. The Entra app's **Supported account types** must match the configured
authority. For `consumers`, choose personal Microsoft accounts (or the Microsoft
option that includes personal accounts) and connect only the dedicated forwarding
mailbox.

1. In Microsoft Entra admin center, create or select an App registration.
2. Under **Authentication**, add the **Web** redirect URI exactly as deployed:
   `https://<render-service>.onrender.com/api/m365/callback`.
3. Under **Certificates & secrets**, create a client secret and copy its value into the
   deployment secret store. Do not commit it.
4. Under **API permissions > Microsoft Graph > Delegated permissions**, add:
   - company tenant mode: `Mail.Read` and `Mail.ReadWrite`;
   - personal forwarding mode: `Mail.Read` and `Mail.ReadWrite`.
5. Do not add `Mail.Send`.
6. Grant user/admin consent according to the tenant's policy.

The application requests the smaller scope for each role at connection time. Ngan is
read-only. Tran can create or delete only the temporary unsent draft needed by this
workflow; the product has no send operation.

## Environment

All four settings are required. Missing, partial, or malformed configuration fails
closed and reports the capability as unavailable.

```dotenv
ASSET_HUB_M365_TENANT_ID=22222222-2222-2222-2222-222222222222
ASSET_HUB_M365_CLIENT_ID=11111111-1111-1111-1111-111111111111
ASSET_HUB_M365_CLIENT_SECRET=<secret-value>
ASSET_HUB_M365_REDIRECT_URI=https://<render-service>.onrender.com/api/m365/callback
```

For the personal forwarding-inbox workflow, use:

```dotenv
ASSET_HUB_M365_TENANT_ID=consumers
```

The user still signs in and grants the delegated permission. The product does not
obtain mailbox access merely because this value is set.

For local development only, the callback may use HTTP on loopback:

```dotenv
ASSET_HUB_M365_REDIRECT_URI=http://127.0.0.1:5000/api/m365/callback
```

`ASSET_HUB_RETAIN_RAW_EML=true` is required to create Outlook drafts. Company-tenant
Reply-All reads the exact source `Message-ID`; personal forwarding mode uses the
retained inner EML for source binding and its standalone draft subject. Sync itself
can ingest without retention, but source download, mail PDF, local draft, and Outlook
draft will then be unavailable.

On Render, set tenant ID, client ID, and client secret as secret environment values.
The staging blueprint already declares the callback variable. Its value must exactly
match both the real service URL and the Entra Web redirect URI.

## User workflow

For each role independently:

1. Select **Connect Microsoft 365**.
2. Choose the intended mailbox in Microsoft's account chooser and consent to the
   displayed delegated permission.
3. Load folders. The server returns visible folders and bounded nested folders.
4. Select exactly one folder.
5. Select **Sync now** when new mail should be imported.

For a personal forwarding inbox, create two folders such as `Ngan` and `Tran`. Route
each approved company notice into the matching folder, connect the **same personal
account** once under each Product role, then select the matching folder. Forwarding the
original message **as an attachment** is supported: Product unwraps exactly one
attached `.eml`, validates it with the existing safe EML contract, and stores/parses
that inner original message. Multiple attached messages or any additional real
attachment fail closed. The product never gains access to the company mailbox.

Because the personal copy is not the original company conversation, Product does not
call Graph Reply-All in this mode. Tran instead creates a **new standalone unsent
draft** in the personal Outlook mailbox, with no recipients, containing the approved
intro/table and workbook. The returned Outlook link opens that draft so the operator
can copy its content back to the original company mail. It is not the original thread.
Local Bridge remains the option that can open the exact original Reply-All window.

In company-tenant mode, Tran can resolve assets and choose **Create Outlook draft**.
The backend:

1. Reads the single exact `Message-ID` from the retained source EML.
2. Looks up exactly one matching mailbox message.
3. Calls Graph `createReplyAll`, which creates an unsent draft with Outlook's quoted
   thread and recipients.
4. Prepends the escaped, approved intro and the exact existing 15-column Tran table.
5. Attaches the newly generated workbook.
6. Returns a validated Outlook web link when Graph provides one.

If body update or attachment fails after draft creation, the server tries to delete
the temporary draft. The API reports explicitly when that rollback is uncertain.

Personal forwarding mode uses `POST /me/messages` instead of `createReplyAll`, omits
all recipients, attaches the same workbook, returns the validated Outlook link, and
uses the same rollback/no-send boundary.

## Manual sync limits

There is deliberately no durable server background timer or webhook on Render Free.
The React page can optionally run a five-minute manual-equivalent timer while its tab
is visible. That browser timer is off by default and stops when the tab is hidden,
closed, or the role disconnects.

- one selected folder per browser session and role;
- nested folder listing: depth 4, 200 folders, 10 Graph pages;
- first sync: created messages received in the last 30 days only;
- later syncs: stored folder delta cursor, `changeType=created`;
- exactly one bounded delta page and at most 10 messages per sync request; when Graph
  returns a next-page cursor it is committed and `has_more=true` tells the browser to
  continue with another request, preventing a legal 20 MiB page from poisoning a
  larger multi-page batch;
- at most 2 MiB raw MIME per message and 25 MiB total;
- existing stable case IDs and content hashes use the existing ingestion deduplication;
- the cursor commits only after ingestion succeeds;
- a message moved/deleted between delta listing and MIME retrieval is skipped with a
  count-only warning, so it cannot poison the cursor or escape the selected folder;
- raw MIME above 2 MiB is skipped with a count-only warning and the cursor can still
  advance; transport/provider failures still abort without committing the cursor;
- malformed/unsupported MIME is also skipped per message after the existing safe EML
  validator runs; the only wrapper exception is exactly one attached `.eml`, whose
  inner message must itself pass the normal no-attachment validator;
- `has_more=true` means the operator should click Sync again.

Selecting another folder resets that role's cursor. Disconnecting one role clears only
that role's account, token cache, folder, and cursor. Authorized staging data reset
clears every in-memory M365 session so testers can reconnect and resync.

## API contract

When the deployment uses shared HTTP Basic auth, all endpoints below require it except
`GET /api/m365/callback`. The callback exemption is intentional because the Entra
cross-site return cannot reliably carry cached Basic credentials; it remains protected
by the opaque SameSite cookie and one-time state validation.

### Status

```http
GET /api/m365/{role}/status
```

`role` is exactly `ngan` or `tran`.

```json
{
  "ok": true,
  "configured": true,
  "role": "tran",
  "required_scope": "Mail.Read",
  "connected": true,
  "account": {"display_name": "Tran User", "email": "tran@example.com"},
  "selected_folder": {
    "id": "opaque-folder-id",
    "display_name": "Asset Compensation",
    "path": "Inbox / Asset Compensation"
  },
  "cursor_ready": true,
  "storage": "memory",
  "background_sync": false
}
```

Disconnected status returns `account:null`, `selected_folder:null`, and
`cursor_ready:false`.

### Connect and callback

```http
POST /api/m365/{role}/connect
Content-Type: application/json

{"return_to":"/?tab=tran"}
```

`return_to` is optional and must be a same-origin absolute path. Response:

```json
{
  "ok": true,
  "role": "tran",
  "authorization_url": "https://login.microsoftonline.com/..."
}
```

Open `authorization_url` in the same browser. Microsoft returns to:

```http
GET /api/m365/callback?code=...&state=...
```

After state validation/token exchange it responds `303` to the stored `return_to`.
The callback never echoes provider code, state, tokens, or provider error details.

### Disconnect

```http
POST /api/m365/{role}/disconnect
Content-Type: application/json
X-Asset-Hub-Action: m365-disconnect-v1

{}
```

Response: `{"ok":true,"role":"tran","connected":false}`.

### List and select folder

```http
GET /api/m365/{role}/folders
```

```json
{
  "ok": true,
  "role": "tran",
  "folders": [
    {
      "id": "opaque-folder-id",
      "display_name": "Asset Compensation",
      "child_folder_count": 0,
      "path": "Inbox / Asset Compensation"
    }
  ],
  "maximum_depth": 4,
  "maximum_count": 200
}
```

```http
POST /api/m365/{role}/folder
Content-Type: application/json

{"folder_id":"opaque-folder-id"}
```

```json
{
  "ok": true,
  "role": "tran",
  "selected_folder": {
    "id": "opaque-folder-id",
    "display_name": "Asset Compensation",
    "path": "Inbox / Asset Compensation"
  },
  "cursor_ready": false
}
```

### Manual sync

```http
POST /api/m365/{role}/sync
Content-Type: application/json
X-Asset-Hub-Action: m365-sync-v1

{}
```

```json
{
  "ok": true,
  "role": "tran",
  "folder": {
    "id": "opaque-folder-id",
    "display_name": "Asset Compensation",
    "path": "Inbox / Asset Compensation"
  },
  "fetched_count": 2,
  "ingested": 1,
  "case_ids": ["LOST-202608-..."],
  "warnings": [],
  "unknown_files": [],
  "skipped_files": [],
  "has_more": false,
  "cursor_ready": true,
  "unavailable_count": 0,
  "oversized_count": 0,
  "invalid_mime_count": 0
}
```

`fetched_count` is raw mail retrieved. `ingested` is the number of newly created stable
case IDs; a duplicate can appear in `case_ids` while `ingested` remains zero.
`unavailable_count` is the number of delta items that disappeared before their
folder-scoped MIME read; no message or folder identifier is exposed in its warning.
`oversized_count` is the number of raw MIME messages over the 2 MiB per-message bound;
the API exposes no Graph ID, folder, subject, or provider body for them.
`invalid_mime_count` is the count of messages rejected by the existing safe EML
validator; the API does not expose their Graph IDs or subjects.

### Outlook Tran draft

```http
POST /api/tran/outlook-drafts
Content-Type: application/json

{
  "assets": [{"tag_number":"LAP10001","asset_name":"Laptop","domain":"user"}],
  "source_bindings": [
    {"case_id":"LOST-202608-...","source_row_index":0}
  ],
  "mail_artifact_handle": "eml-sha256-<64 lowercase hex>",
  "body_intro": "Dear team, ...",
  "processing_date": "2026-08-15",
  "year_sheet": "2026"
}
```

The asset schema and optional workbook fields are identical to the local Tran draft
route. `source_bindings` is required and must contain one binding for each asset in
the same order. A parsed table case can contain multiple source assets in
`metadata.asset_rows`, so the same `case_id` may be repeated with different
zero-based `source_row_index` values. Every `(case_id, source_row_index)` pair must be
unique. A LOST case without an `asset_rows` table uses `source_row_index: null`; a
table-backed case requires a valid row index.

Both `POST /api/tran/drafts` and `POST /api/tran/outlook-drafts` enforce this same
row-level provenance contract before reading retained mail or creating output. Every
case must be LOST, must not be `ACCOUNTED` or `CLOSED`, and must reference the exact
submitted retained-mail handle. The requested `tag_number` and normalized `domain`
must match the bound source row (or non-table case identity) exactly and cannot be
overridden. Other supported asset fields can still carry an operator-approved
correction before resolving/exporting. These checks prevent a valid handle, case, or
row index from being combined with an unrelated asset.

```json
{
  "ok": true,
  "role": "tran",
  "output_id": "<32 lowercase hex>",
  "asset_count": 1,
  "workbook_download_url": "/api/tran/workbooks/<id>/download",
  "outlook_draft": {
    "subject": "Re: ...",
    "web_url": "https://outlook.office.com/..."
  },
  "sent": false
}
```

`web_url` can be `null` if Graph does not return a link on the draft response. The
server only accepts HTTPS Outlook hosts. If the provider-created quoted thread would
push the final draft body above the 500,000-character safety cap, only that quote is
safely reduced and marked as truncated; the approved intro and exact Tran table remain
intact.

## Errors

All errors are sanitized JSON and do not contain tokens, raw Message-IDs, Graph object
IDs, local paths, or provider bodies.

- `400`: invalid role/input/state/folder/action header.
- `401` with `reconnect_required:true`: missing, expired, or rejected role token.
- `502`: bounded upstream Graph operation failed or draft rollback was uncertain.
- `503` with `capability_available:false`: M365 dependency/configuration is incomplete.

## Staging and production gap

Token caches, selected folders, delta cursors, and OAuth sessions are intentionally
process-memory-only for hackathon staging and expire after an absolute eight hours.
Render restart/redeploy, scaling, or a second worker loses them and requires
reconnect/reselect/resync. Do not increase Gunicorn worker count for this design.

Before production, replace the in-memory store with encrypted server-side persistence
that supports multiple instances, explicit retention/rotation, account revocation,
audit logging without personal mail content, and a production SSO/RBAC boundary.
