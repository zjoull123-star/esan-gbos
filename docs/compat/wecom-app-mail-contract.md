# WeCom application-mail inbound contract status

Status: **GREEN for inbound Tasks 6–9 — frozen**

Official documentation reviewed: **2026-08-14 (Asia/Shanghai)**

This note records only facts visible in current first-party WeCom documentation. The five closed
inbound JSON Schemas and sanitized synthetic fixture bundle are now frozen for Tasks 7–9. This
approval covers callback verification, token acquisition, inbox listing, full EML reads, and the
explicit `45009` pause policy below. It does not approve a live mailbox, credentials, provider
network access, historical backfill, or outbound send.

## Confirmed first-party facts

### Permission and application mailbox

- An application must have mail API permission. For an internal application, an administrator adds
  it to the mail API's callable-application list. WeCom then assigns the application an application
  mailbox account.
- The mail-list and read-mail pages require the access token obtained from the secret of an internal
  application on that callable list. Third-party and delegated-development applications require the
  mail permission.

Source: [Mail API overview](https://developer.work.weixin.qq.com/document/path/95486).

### Callback verification, encryption, and new-mail signal

- Callback configuration uses a public URL, a developer-selected Token of at most 32 alphanumeric
  characters, and a 43-character alphanumeric EncodingAESKey.
- URL verification is an HTTP GET carrying `msg_signature`, `timestamp`, `nonce`, and encrypted
  `echostr`. The receiver URL-decodes the parameters, verifies the signature, decrypts `echostr`,
  and returns the plaintext without quotes, BOM, or newline within one second.
- Business callbacks are HTTP POST requests. The query carries `msg_signature`, `timestamp`, and
  `nonce`; the XML envelope carries `ToUserName`, optional application `AgentID`, and `Encrypt`.
  The decrypted business payload is XML.
- The application-mail receive event has `MsgType=event`, `Event=app_email_change`,
  `ChangeType=receive_email`, and `Amount`, where `Amount` is the application's current new-mail
  count. `FromUserName` is fixed to `sys`.
- WeCom states that callback delivery is not guaranteed and recommends an additional mechanism to
  reconcile business data. Therefore GBOS treats this event as a wake/count hint only. It is not a
  message delivery, stable message identifier, page cursor, or completion proof.

Sources: [Callback configuration](https://developer.work.weixin.qq.com/document/path/90930) and
[mail callback event](https://developer.work.weixin.qq.com/document/path/97495).

### Token

- An internal application obtains an application-scoped token with HTTP GET at
  `/cgi-bin/gettoken?corpid=ID&corpsecret=SECRET`.
- The successful JSON response contains `errcode`, `errmsg`, `access_token`, and `expires_in`.
  `access_token` is at most 512 bytes; its normal lifetime is 7200 seconds. It must be cached by
  application, may be invalidated early, and must remain server-side.

Source: [Get access token](https://developer.work.weixin.qq.com/document/path/91039).

### Mail list, pagination, and stable provider ID

- The application inbox list is fetched with HTTP POST at
  `/cgi-bin/exmail/app/get_mail_list?access_token=ACCESS_TOKEN` using JSON `begin_time` and
  `end_time`, with optional `cursor` and `limit`.
- `cursor` is the previous response's `next_cursor`. `limit` defaults to 100 and has a documented
  maximum of 1000.
- A successful response contains `errcode`, `errmsg`, `next_cursor`, `has_more`, and `mail_list`.
  `has_more` is `0` or `1`; each list entry contains `mail_id`.
- `mail_id` is the provider identifier accepted by the read-mail endpoint. Only a pulled `mail_id`,
  not a callback occurrence or `Amount`, may identify an Observer delivery.

Source: [Get application inbox mail list](https://developer.work.weixin.qq.com/document/path/97369).

### Full EML fetch

- One message is fetched with HTTP POST at
  `/cgi-bin/exmail/app/read_mail?access_token=ACCESS_TOKEN` using JSON `mail_id`.
- A successful JSON response contains `errcode`, `errmsg`, and `mail_data`; WeCom describes
  `mail_data` as the mail's EML content.

Source: [Read application mail](https://developer.work.weixin.qq.com/document/path/97979).

### Rate limit, token expiry, revocation, and disabled state

- The general first-party frequency page states base limits per enterprise, IP, and third-party
  provider. The global error page documents `45009` for an exceeded interface-call limit and says
  blocking duration generally follows the applicable minute/hour/day/month interval.
- The global error page documents `40014` (invalid access token), `42001` (expired access token),
  `48004` (authorization invalid or cancelled), `48006` (API permission reclaimed), `50003`
  (application disabled), and `60031` (application prohibited from calling APIs).
- These are general WeCom error codes. The reviewed mail-list and read-mail pages do not publish a
  mail-endpoint-specific error table.

Sources: [API frequency limits](https://developer.work.weixin.qq.com/document/path/90312) and
[global error codes](https://developer.work.weixin.qq.com/document/path/90313).

## Approved `45009` policy

The reviewed first-party pages do not specify HTTP status 429 or a `Retry-After` header for the
application-mail endpoints. No schema, fixture, or adapter may translate `45009` into HTTP 429,
invent `Retry-After`, or derive an automatic recovery timer.

The approved inbound behavior is therefore deliberately conservative:

- JSON `errcode=45009` immediately durably pauses only the affected mailbox connector;
- the failed operation does not advance its provider cursor or checkpoint;
- other mailbox connectors continue independently;
- restart, callback replay, periodic reconciliation, elapsed time, and backoff never clear the
  pause;
- only an authenticated `Integration Admin` or `GBOS Admin` may explicitly resume it through the
  governed admin command, with expected-revision, idempotency, and audit checks;
- already-published immutable items may finish their local processing, but no further provider pull
  is made while the connector is paused.

This operator-approved rule resolves the former Task 6 inbound blocker without asserting an
undocumented provider retry contract. Tasks 7–9 may proceed offline and remain default-off; a real
shadow mailbox still requires separate explicit authorization and evidence.

## Outbound boundary

Status: **RED — Task 18 stopped**

The current first-party outbound page does document submission of an ordinary email:

- `POST /cgi-bin/exmail/app/compose_send?access_token=ACCESS_TOKEN`;
- required `to` plus `subject` and `content`;
- optional `cc`, `bcc`, `attachment_list`, `content_type`, and `enable_id_trans`;
- each recipient group may contain `emails` and `userids`; at least one `to` address or User ID is
  required;
- attachments contain `file_name` and Base64 `content`; body plus attachments is limited to 50M,
  with at most 200 attachments;
- the authenticated application mailbox is the sender; the request has no arbitrary `from` or
  sender-alias selector.

The exact documented success response is only `{ "errcode": 0, "errmsg": "ok" }`. The request has
no stable client request or idempotency field, and the response has no mail/message/receipt ID. The
official inbox list and read-mail APIs apply to received mail only. The reviewed official catalog
does not publish a sent-mail list, submission/delivery status, receipt lookup, or post-timeout
reconciliation endpoint.

The send page also leaves recipient-count, subject-length, arbitrary-header, decoded-versus-Base64
size, and endpoint-specific error limits unspecified. The general first-party error contract still
documents JSON `45009` for rate limiting; it does not specify HTTP 429 or `Retry-After`.

Sources: [send ordinary email](https://developer.work.weixin.qq.com/document/path/97445),
[mail API overview](https://developer.work.weixin.qq.com/document/path/95486),
[query application mailbox](https://developer.work.weixin.qq.com/document/path/97991),
[update application mailbox](https://developer.work.weixin.qq.com/document/path/97373),
[frequency limits](https://developer.work.weixin.qq.com/document/path/90312), and
[global error codes](https://developer.work.weixin.qq.com/document/path/90313). Reviewed
2026-08-14 (Asia/Shanghai).

An ambiguous timeout may occur after the provider accepted the email. Without an official
idempotency key or a stable receipt plus lookup API, retry could duplicate a real customer email.
Therefore no outbound schema/fixture, adapter, provider credential, or runtime enablement is
approved. Task 18 can resume only after first-party proof supplies either documented idempotent
replay semantics or a stable send receipt with post-timeout status lookup. External send remains
disabled.
