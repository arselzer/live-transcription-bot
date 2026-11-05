# Zoom admission, bot detection and supported ingestion

## Observed incident

On 2026-11-05, a completed personal-room test with two named TTS senders produced
zero word errors on its 46-word bilingual baseline. In a later stress retry, the
English sender's web client displayed “Automated bots aren't allowed to join this
meeting.” The German sender and the main recorder were admitted. The main archive
continued and its finalized primary and safety-copy inventories verified.

That establishes a provider automation block for that join. It does not establish
which proprietary detection signal triggered it. Several repeated joins and
persistent browser profiles were involved; name, profile/session history,
automation signals, account authentication and network reputation are hypotheses,
not a measured explanation. A different deployment of Vexa reports the same
[provider block](https://github.com/Vexa-ai/vexa/issues/345), reinforcing that this
is not unique to this transcript implementation.

## What the platform documents

Zoom's [bot-protection documentation](https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0088381)
distinguishes web-app automation protection from waiting rooms and app allow lists.
Its web protection is enabled by default and cannot be disabled. Removing a
waiting room does not remove it. The documented sign-in option is for a real
person incorrectly flagged; it is not a promise that an automated client becomes
supported by logging into a human account.

Current [Web Meeting SDK](https://developers.zoom.us/docs/meeting-sdk/web/) and
[Linux Meeting SDK](https://developers.zoom.us/docs/meeting-sdk/linux/) landing
pages direct AI notetakers to RTMS. Older SDK examples and community advice can
conflict with that current guidance. Do not build a new bot migration around a
stale authentication example.

## Current behavior

The maintained adapter distinguishes explicit automation denial, CAPTCHA, host
refusal and locked meetings. Host refusal, locks and CAPTCHA remain terminal.
For automation denial only, an explicitly configured list permits at most two
name fallbacks after the initial name. The invitation, browser profile and original
deadline are preserved; retries stop after the configured names are exhausted. It records a private,
session-scoped screenshot and structured `failure.json`, prints a precise reason,
and attempts to retrieve those artifacts to the host. Diagnostic storage failure
must not turn a terminal denial into a blind retry. The host lifecycle and Hermes
watchdog expose the reason and request operator inspection.

Test helpers share a persisted per-profile cooldown and rolling join budget.
These controls reduce avoidable test churn; there is no claim that they make
Zoom's detector accept a bot. Offline replay/fixtures cover most regression work.
There is no arbitrary identity or profile rotation, cookie reset, network change,
or CAPTCHA retry. Name retries use only the explicitly configured sequence.

Set a clear participant name through `identity.name` or `start --name`.
`admission.zoom_name_fallbacks` defaults to an empty list. Operators may supply
up to two alternative names they are authorized to use, for example
`Example User (Transcript)` followed by `Example User`. These are synthetic
examples, not deployment defaults. Successful fallback names are retained for
watchdog recovery. Fixture success does not prove provider acceptance.

The existing browser adapter remains a compatibility path with provider-policy
and HTML fragility. Its prejoin success cannot guarantee future admission. Do not
promise a meeting is captured until actual admission, media and archive progress
are verified. Preserve a separate authorized recording option for important calls.

## Longer-term architecture

[Zoom RTMS](https://developers.zoom.us/docs/rtms/) supplies live media to an app
without a participant bot. [Media documentation](https://developers.zoom.us/docs/rtms/meetings/media/)
includes participant identifiers and per-participant audio, which can eliminate
much mixed-stream overlap ambiguity and DOM-based identity inference.

A concrete migration needs:

1. A Zoom app/account configuration, host authorization, appropriate media scopes
   and RTMS credits; no credentials or app are configured by this code change.
2. An authenticated webhook/signaling receiver with replay protection and a
   durable session identity before acknowledging media.
3. Independent lossless archives for each original participant stream, explicit
   timestamps/gaps/joins/leaves, and the existing durable transcription interface.
4. Paired audio/overlap/failure tests and verified archival recovery before
   switching scheduled meetings to the new input path.

RTMS ingestion should be a new media-source adapter, not an HTML fallback pretending
to be the same participant. The current caption-camera and TTS output features
need separate supported output design; receiving RTMS media alone does not prove
that those features are provided. Changing account billing, creating/publishing
an app or enabling a new external data route is a separate deployment step.
