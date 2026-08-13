# WeCom application-mail inbound contract status

Status: **RED — not frozen**

Official documentation reviewed: **2026-08-13 (Asia/Shanghai)**

This note records only facts visible in current first-party WeCom documentation. It is not a
wire-contract approval. The five inbound JSON Schemas and sanitized fixture bundle remain absent,
and Tasks 7–9 must not start while the mandatory gap below is unresolved.

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

## Mandatory unresolved gap

The approved Task 8 test plan requires exact handling of **HTTP 429 with `Retry-After`**. None of
the current first-party pages reviewed above specifies that the application-mail endpoints return
HTTP status 429 or a `Retry-After` header. The official material instead describes a JSON
`errcode=45009` and interval-based blocking.

No schema or fixture may translate `45009` into HTTP 429, invent a `Retry-After` header, or choose a
retry delay. Task 6 therefore remains RED. Tasks 7–9 remain blocked until a current first-party
WeCom source confirms the required HTTP contract or the approved implementation plan is revised to
use only the officially documented `45009` behavior.

## Outbound boundary

Outbound behavior is not frozen by this note. No send/status schema or fixture is approved, and
outbound remains unresolved and disabled.
