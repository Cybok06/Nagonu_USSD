# Moolre USSD migration status

## Status

The callback contract was supplied and confirmed after the public guide was
initially unavailable. The adapter is implemented at `/ussd/moolre`, but no
production settings or provider dashboard were changed automatically.

## Contract

Moolre sends a JSON callback containing `sessionId`, `new`, `msisdn`, `network`,
`message`, `extension`, and `data`. On the first callback, `data` supplies the
additional dialed value (the agent code). Continuing callbacks use the latest
value in `message`.

Responses are JSON with exactly `message` and `reply`; `reply=true` continues
the session and `reply=false` ends it.

## Intended callback flow

`/ussd/moolre` parses the Moolre request into normalized `session_id`, `phone`,
and `text` values and calls the same shared resolver used by `/ussd`. The resolver
checks both Nagonu and Zico sessions and agent-code ownership.

## First-time caller Results Checker flow

On a new Moolre session, the gateway searches both Nagonu and Zico `orders`
collections using local and international phone variants. It checks item,
buyer, dial, and USSD dial-phone fields.

- A caller with order history continues to the existing shared agent-code flow.
- A caller without order history enters the Nagonu guest Results Checker flow
  and sees only WASSCE and BECE.

Guest prices come from the same Nagonu `results_checker_settings` record used by
the public `/results-checker` page, with the same inventory-amount fallback.
The dialed number is used for Paystack Mobile Money and checker SMS delivery.
The existing Nagonu payment verification, OTP, final order release, atomic
inventory allocation, and Arkesel SMS delivery remain in use. After completion,
the resulting Nagonu order makes the caller an existing customer on later dials.

The guest path is deliberately separate from normal agent ordering: it cannot
purchase bundles and does not bypass agent validation for store-based orders.

## Deployment

Configure Moolre to send JSON callbacks to:

`https://nagonu-ussd.onrender.com/ussd/moolre`

No callback authentication header was included in the confirmed contract, so no
Moolre secret environment variable was added. Verify the route in a Moolre
sandbox before replacing the legacy callback registration.

Before deployment, apply the first-dial order-history indexes once to both
MongoDB databases:

`python ussd_order_indexes.py`

The migration is idempotent and detects equivalent indexes by key pattern.

Export existing order customers into the dedicated, uniquely indexed
`phone_numbers` collection in both databases:

`python phone_number_registry.py`

The USSD first-dial history check reads these registries concurrently instead
of scanning either `orders` collection. Re-run the exporter after importing
historical orders until all order-creation paths write to the registry directly.

Merge Zico's registry into Nagonu before deploying the single-database lookup:

`python merge_zico_phone_numbers.py`

The merge preserves existing Nagonu records when a phone exists in both
collections and inserts only missing Zico phone numbers.

The supplied contract did not explicitly state the HTTP method. The route keeps
the existing GET/POST availability. It accepts the documented JSON object when
sent with `application/json`, and also parses a valid JSON body when Moolre omits
or mislabels the media type. Non-JSON callback bodies are rejected.

## Rollback

Rollback consists of restoring the previous gateway callback registration to
`https://nagonu-ussd.onrender.com/ussd`. The legacy routes remain available.

## Arkesel SMS

Outbound Arkesel SMS remains separate and unchanged. This includes Nagonu order
notifications, Zico notifications, results-checker delivery, deposit messages,
and complaint messages.
