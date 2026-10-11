# Bruno

**An autonomous re-engagement agent for local restaurants. It texts lapsed regulars and takes phone orders.**

Bruno reads a pizzeria's order history and learns each regular's rhythm. When someone goes quiet, it sends a personal iMessage/SMS at an off-peak hour:

> Hey Dave! It's Marco from Tony's. Noticed it's been a few weeks since your usual Friday large pepperoni pizza. Want me to spin up your usual for pickup around 6:00? Reply here or feel free to give me a call right back at this number to order instantly!

The customer can reply by text ("Swap pepperoni for mushroom, add a Diet Coke") or call. Either way, Bruno works from the same cart and customer memory. It confirms a priced summary, charges the card on file, and injects the order into the POS so a kitchen ticket prints.

```
[POS (Toast/Square/Clover)] ──nightly sync / webhook──▶ POST /webhook/pos/order
                                                              │
                                   [Cadence engine: Postgres + Celery/Redis]
                                       nightly recompute · 3:00 PM scan · 4:30 PM send
                                                              │
                              [Context orchestrator: menu · history · usual · weather]
                                   │                                         │
                   [Text / iMessage: Sendblue + LLM]            [Voice inbound: Retell AI]
                   POST /webhook/incoming                       POST /voice/retell/inbound
                   GPT-4o or Claude, strict JSON                POST /voice/retell/functions/*
                                   └──────────────┬──────────────────────────┘
                                 [Shared Conversation row: FSM state + cart JSON]
                                                  ▼
                              [Menu validator + server-side pricing + payment]
                                                  ▼
                              [POS injection: console ticket / Square Orders API]
```

## Quick start (no API keys needed)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

python scripts/seed.py --phone +15551234567 --reset   # 3 test customers, your cell is "Dave"
python scripts/chat.py --phone +15551234567 --outreach
```

```
bruno> Hey Dave! It's Marco from Tony's. Noticed it's been a few weeks since your usual ...
you> Swap pepperoni for mushroom, add a Diet Coke
bruno> You got it!

Here's what I've got:
• 1x Large Pizza w/ Mushroom — $20.50
• 1x Diet Coke (20oz) — $2.99
Total $25.37 (incl. tax) · pickup ~6:00 · Visa •4242
Reply YES to place it, or tell me what to change.
you> yes
🍕  KITCHEN TICKET RECEIVED ... (ticket + exact POS JSON payload)
bruno> Awesome! You're all set, Dave! Ready around 6:00. Charged $25.37 to your Visa •4242. See you soon 🍕
```

With no API key set, `LLM_PROVIDER=auto` falls back to an offline rule-based mock, so the whole loop runs locally. To use a real model, set `OPENAI_API_KEY` (GPT-4o, the default) or `ANTHROPIC_API_KEY` (Claude). In the chat simulator, `/call` shows how the voice agent would greet you right now, and `/state` prints the FSM state and cart.

Run the tests with `pytest`.

## The 24-hour MVP loop with real texts

1. **Database.** Use the SQLite default, or point `DATABASE_URL` at Supabase (`postgresql+psycopg://…`, then `pip install -e ".[postgres]"`). Run `python scripts/seed.py --phone <your cell> --reset`.
2. **API.** Run `uvicorn bruno.api:app --port 8000`, then expose it with `ngrok http 8000` (or similar).
3. **Sendblue.** Set `MESSAGING_PROVIDER=sendblue`, the two API keys, `SENDBLUE_FROM_NUMBER` and a `WEBHOOK_SECRET`. In the Sendblue dashboard, point the receive webhook at `https://<tunnel>/webhook/incoming?secret=<WEBHOOK_SECRET>`.
4. **Trigger.** Run `python scripts/trigger.py --all` (or omit `--all` to contact only customers who are actually drifting). Your phone gets the outreach text.
5. **Reply from your phone.** The LLM updates the cart, Bruno texts back a priced summary, and after you reply "yes" the kitchen ticket and POS JSON print in the uvicorn terminal. Each ticket is also appended to `data/kitchen_tickets.jsonl`.

## Voice fallback (Retell AI)

`python scripts/retell_config.py --base-url https://<tunnel>` prints everything you need to paste into Retell:

- **Inbound call webhook** (set on the phone number): `/voice/retell/inbound`. Bruno looks up the caller by phone and returns `dynamic_variables`: a contextual greeting, the usual order, any in-progress cart from texts, the payment method and the menu.
- **Agent prompt**, which uses those variables.
- **Two custom functions.** `update_cart` validates and prices the full cart and saves it to the shared conversation. `place_order` charges the card and injects the order. Leave Retell's "args only" payload option off, because Bruno identifies the caller from `call.from_number`.

Because both channels read and write the same `conversations` row, a customer can start by text and finish by phone. The voice agent opens with *"Hey Dave! I've got your large mushroom pizza from our texts. Want me to put that in?"*

## How it stays safe

- **Deterministic FSM** (`bruno/fsm.py`). The states are `STATE_INITIAL_OUTREACH → STATE_AWAITING_RESPONSE → STATE_NEGOTIATING_MODIFICATIONS → STATE_CONFIRMING_PAYMENT → STATE_POS_INJECTION`, plus the terminal states `COMPLETED`, `DECLINED`, `OPTED_OUT` and `FAILED`. The LLM only reports an *intent*. Code decides the transition, and an order can only fire from `CONFIRMING_PAYMENT`, after the customer has seen the exact items, total, pickup time and payment method. If the customer changes anything in the same message as "yes", Bruno asks them to confirm again.
- **Menu-constrained structured output** (`bruno/llm/base.py`). The JSON schema enumerates the live menu's item and modifier IDs. OpenAI uses strict Structured Outputs, and Claude uses a forced tool call. The validator rejects an invalid size or topping and gives the model one corrective retry.
- **Server-side pricing.** The model is told never to quote prices. Totals come from `bruno/menu.py`, and Bruno appends the priced summary to the reply itself.
- **Compliance.** STOP/UNSUBSCRIBE/… opt a customer out immediately, START opts them back in, and opted-out customers are never contacted. The outreach footer is configurable.
- **Contact rules.** Bruno never re-texts within the cooldown period, and after an ignored nudge it waits a full ordering cycle. It skips anyone with an open conversation and re-checks eligibility at send time.
- **Failure handling.** Duplicate webhook deliveries are de-duplicated by message ID. If the LLM is down, the customer gets a polite fallback. If POS injection fails, the charge is refunded and the conversation is marked `FAILED`.

## Layout

| Path | What |
|---|---|
| `bruno/models.py` | `customers`, `orders`, `cadence_tracker`, plus `conversations`/`messages` (shared dialogue state) |
| `bruno/cadence.py` | average gap between orders, drift flag, "usual" order, typical pickup slot, outreach candidates |
| `bruno/orchestrator.py` | outreach copy, LLM system prompt, priced summaries, voice greeting |
| `bruno/agent.py` | the engine: outreach, inbound text turns, finalize (pay + POS), voice tools |
| `bruno/llm/` | OpenAI, Anthropic and offline mock adapters behind one `generate_turn` interface |
| `bruno/messaging/` | Sendblue sender and inbound parser, console sender |
| `bruno/voice/` | Retell dynamic variables, agent prompt and function schemas |
| `bruno/pos/` | canonical `POSOrder` JSON, console kitchen printer, Square Orders API adapter |
| `bruno/payments.py` | charge/refund interface (mock provider for the MVP) |
| `bruno/tasks.py` | Celery: nightly cadence recompute, 3 PM scan → 4:30 PM sends |
| `bruno/api.py` | FastAPI webhooks |
| `scripts/` | `seed.py`, `trigger.py`, `chat.py`, `retell_config.py` |
| `data/menu.json` | the menu (prices in cents, sizes, modifier groups, aliases) |

## Running the scheduler

```bash
docker compose up -d                       # Postgres + Redis
celery -A bruno.tasks worker --loglevel=info
celery -A bruno.tasks beat   --loglevel=info
```

POS history arrives through `POST /webhook/pos/order` (`{phone, name, external_id, items, total, order_date}`), from a nightly export or a POS webhook relay. Each ingested order recomputes that customer's cadence.

## Not built yet

- **Payments** are mocked. To go live, add a Stripe or Square Payments provider behind `PaymentProvider` and store only processor tokens.
- **POS:** Square is implemented with ad-hoc line items and needs catalog mapping for production. Toast and Clover adapters still need to be written.
- **Schema:** tables are created with `create_all`. Add Alembic before the schema starts changing.
- **Scaling:** the per-customer lock is in-process. Swap it for a Redis lock before running multiple API workers, or move inbound processing onto Celery.
- **Voice signatures:** Retell webhooks are protected by the shared secret. Verifying `X-Retell-Signature` is a TODO.
