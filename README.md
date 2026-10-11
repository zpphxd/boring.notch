# Bruno

**A texting sales manager for local restaurants. It learns your customers from your order history, reaches out to them like a person would, and takes their order by text or phone.**

> Hey Dave! Marco from Tony's. It's been a few weeks since your large pepperoni pizza. Want me to get one going for pickup around 6:00? Just reply here or call me right back at this number.

```
Dave:   swap pepperoni for mushroom, add a diet coke
Bruno:  You got it!
Bruno:  Cool, large mushroom pizza and Diet Coke for 6:00. $25.37 on your Visa •4242. Should I send it to the kitchen?
Dave:   yes
Bruno:  Awesome!
Bruno:  You're all set, Dave! Ready around 6:00. $25.37 on your Visa •4242. See you soon 🍕
```

The order lands in your POS and the kitchen ticket prints. Bruno remembers that Dave likes it well done, and it learns which kinds of messages actually bring people back.

---

## Get it running (about 10 minutes)

```bash
git clone <this repo> && cd <repo>
docker compose up -d          # Bruno + Postgres
open http://localhost:8000    # create your dashboard password
```

Without Docker:

```bash
pip install -e .
python -m bruno               # same app, uses SQLite unless DATABASE_URL is set
```

Then everything else happens in the **dashboard**:

1. **Settings.** Paste your keys and hit Save. They take effect immediately, with no restart.
   - **AI:** an OpenAI key (GPT-4o) *or* an Anthropic key (Claude).
   - **Texting:** your BlueBubbles server URL and password (see [Set up your iMessage gateway](#set-up-your-imessage-gateway-bluebubbles) below), plus a webhook secret. Sendblue keys work instead if you'd rather use a hosted service.
   - *(optional)* **Stripe** secret key and webhook secret. These enable saved cards (one-text reorders) and secure pay links. Without Stripe, customers pay at pickup.
   - *(optional)* **Square** access token and location ID. Without them, tickets print to the server log and `data/kitchen_tickets.jsonl`.
   - *(optional)* **DoorDash Drive** credentials for delivery, and/or your DoorDash store link.
   - Your restaurant name, the name customers see texts from, address, timezone and hours.
2. **Paste the webhook URLs** shown at the bottom of Settings into BlueBubbles (or Sendblue), Stripe (event `checkout.session.completed`), and Retell if you use phone calls.
3. **Import.** Upload an order-history CSV from Square, Toast, Clover, DoorDash or any spreadsheet.
4. **Menu.** Check that the items and prices match yours. Bruno can only sell what's on this page.
5. **Outreach.** Turn on Autopilot. Start in *approve* mode (you OK each text), then switch to *auto* once you like what it writes.

Use **Test AI**, **Test BlueBubbles** and **Send me a test text** on the Settings page to confirm the keys work.

To deploy, any container host works (Railway, Render, Fly.io, a VPS). Set `DATABASE_URL` to a Postgres database (Supabase works) and `PUBLIC_BASE_URL` to the app's public address. It's a single process: the web app, webhooks and scheduler all run together, so there's no Redis or worker to manage.

### Set up your iMessage gateway (BlueBubbles)

Bruno texts customers through [BlueBubbles Server](https://bluebubbles.app), which runs on a Mac signed into iMessage and turns it into a small REST API. It's free and self-hosted, and it's meant for **proof-of-concept volumes**: Apple may rate-limit or flag an Apple ID that sends lots of unsolicited messages. For production volume, use Sendblue instead (its fields are right below BlueBubbles on the Settings page).

1. **A Mac that stays on**, with the Messages app signed into the Apple ID (or number) you want customers to see texts from. Turn off sleep. Bruno sends blue-bubble iMessages through it, so customers without iMessage (Android phones) won't get texts this way.
2. **Install BlueBubbles Server**: download the `.dmg` from its [latest GitHub release](https://github.com/BlueBubblesApp/bluebubbles-server/releases/latest) and walk through the setup wizard (it asks for Accessibility and Full Disk Access so it can read and send messages; you can skip the Google/Firebase step with *Manual Setup*).
3. **Set a server password** on the wizard's *Connection* step (click the save icon).
4. **Expose it to the internet** so Bruno can reach it, with the *Proxy Service* setting: the built-in *Cloudflare* option is the easiest; *zrok*, *Ngrok* or your own Dynamic DNS also work. Copy the server URL it shows, e.g. `https://xyz.trycloudflare.com`. If Bruno runs on the same network you can also use the Mac's LAN address, e.g. `http://192.168.1.20:1234`. (Free Cloudflare URLs change when the server restarts, so update Bruno's Settings if that happens.)
5. *(optional)* **Enable the Private API** in BlueBubbles (needs its helper installed, which involves partially disabling SIP on the Mac; follow BlueBubbles' Private API guide). It gives customers the "..." typing bubble while Bruno writes. Skip it and Bruno still works, just without typing bubbles.
6. **Add the webhook.** In Bruno's Settings, set a *Webhook secret* and Save, then copy the **BlueBubbles: webhook** URL from the table at the bottom. In BlueBubbles go to *API & Webhooks*, add a webhook with that URL and select the **New Messages** event.
7. **Paste the server URL and password into Bruno** (Settings → *Texting (BlueBubbles)*), turn on *Use the BlueBubbles Private API* if you did step 5, and Save. Hit **Test BlueBubbles**, then **Send me a test text**.

### Try it with no keys at all

```bash
python -m bruno demo --phone +15551234567            # 30 demo customers; your number is "Dave"
python -m bruno chat --phone +15551234567 --outreach # Bruno texts you first; reply in the terminal
python -m bruno                                      # dashboard at http://localhost:8000
```

With no AI key, Bruno runs on a built-in offline model and template messages, so you can click through everything first.

---

## What it does

### Learns from your history
- Imports CSV exports and recognizes the columns automatically. It groups line-item rows into orders and maps item text onto your menu ("Lg Pep Pizza" becomes a large pizza with pepperoni). Items it can't match are listed so you can add an alias.
- Builds a profile of every customer: how often they order, their usual, their favorites, the day and time they usually order, average ticket and lifetime value. It puts each customer in a segment: *regular*, *drifting* (overdue), *lapsed*, *one-time*, *new* or *dormant*.
- Keeps learning from live orders (`POST /webhook/pos/order` from your POS) and from every conversation.

### Reaches out on its own
- **Who:** drifting, lapsed and one-time customers, ranked by value. Bruno skips anyone who opted out, was texted recently, or is mid-conversation.
- **When:** about 75 minutes before *that person* usually orders, inside your send window, with a daily cap.
- **What:** each text is written fresh by the AI for that customer: their usual, how long it's been, what they've told you, even the weather. Guardrails reject any draft with prices, promos, links, emoji spam or marketing-speak, or that doesn't say who's texting on first contact. If the AI is unavailable or keeps failing the checks, Bruno uses varied hand-written templates instead.
- **Learning what works:** every text is tagged with a strategy (*usual*, *win-back*, *second order*) and a tone (*casual*, *playful*, *brief*). An order within 48 hours counts as a win. Bruno uses Thompson sampling, so it keeps testing approaches while shifting toward whatever converts for *your* customers. Opt-outs count heavily against an approach. The Outreach tab shows the results.
- **Follow-up:** if someone was mid-order and went quiet, Bruno sends one gentle nudge and never more.

### Talks like a person
- Short texts, matched to the customer's vibe, sometimes split into two bubbles, with a typing indicator and natural pauses before replying.
- Remembers lasting preferences ("likes it well done") and uses them next time. You can edit what it remembers on the customer page. The notes also print on the kitchen ticket.
- May suggest one add-on that people actually order together in your data, and only once.
- If a customer sincerely asks whether it's a bot, Bruno says honestly that it's the restaurant's automated assistant and that a person can jump in.
- Complaints, refunds, allergies, or "let me talk to someone": Bruno stops replying, texts the owner, and flags the conversation in the dashboard. You reply from the dashboard, then hand it back.

### Takes the order and gets it to the kitchen
- **Text (iMessage via BlueBubbles, or Sendblue) and voice (Retell).** Both channels share one cart, so a customer can start by text and finish by calling.
- **Prices always come from your menu.** The AI writes the wording with placeholders like `{total}`, and Bruno fills in the exact numbers. If a reply contains a dollar amount Bruno didn't calculate, the reply is replaced.
- **Explicit confirmation.** Nothing is charged or sent to the kitchen until the customer says yes to a recap showing items, total, time and payment method. Changing anything triggers a new recap.
- **Payment (Stripe):** customers with a saved card are charged on their "yes". For delivery (or for every order, if you turn on *require prepay*), customers without a saved card get a secure Stripe Checkout link. The order goes to the kitchen only after Stripe's signed webhook confirms payment, and their card is saved so next time it's one text. Without Stripe, pickup orders are pay-at-pickup.
- **POS:** Square Orders API with a scheduled pickup, or console/log tickets.
- **Delivery:**
  - **DoorDash Drive:** Bruno quotes the address to check DoorDash can deliver there, adds your delivery fee to the recap, dispatches a courier when the order is placed, and texts the tracking link. If dispatch fails, the charge is refunded.
  - **DoorDash store link:** if Drive isn't set up, customers who want delivery get your DoorDash store link. (DoorDash doesn't offer a public API for placing marketplace orders, so a link handoff is the legitimate option.)

---

## Compliance, please read

Proactive marketing texts are regulated (TCPA, carrier rules). Only text customers who agreed to hear from you. The import page asks for this, and unchecking it imports customers for learning only. Bruno puts an opt-out line on the first text to each person, honors STOP/UNSUBSCRIBE and similar keywords immediately, honors START to opt back in, and never re-contacts anyone who opted out. Check your local rules (and, if you use Sendblue, its guidelines) before turning on Autopilot. With BlueBubbles you're texting from a personal iMessage account, so keep volumes low: Apple can restrict accounts that get reported as spam.

---

## For developers

```
bruno/
  agent.py         conversation engine: text turns, finalize (pay → delivery → POS), voice tools, staff replies
  outreach.py      who/when/what: AI-written outreach + guardrails, autopilot, approvals, nudges
  learning.py      outcome tracking, Thompson-sampling strategy choice, attribution, add-on pairings
  cadence.py       per-customer profile + segments
  importer.py      CSV history import
  orchestrator.py  prompts, recap facts, voice greeting
  humanize.py      placeholders, bubbles, typing pauses, fallback copy
  fsm.py           deterministic dialogue state machine
  menu.py          menu, validation, server-side pricing, item-text matching
  delivery.py      DoorDash Drive client (JWT, quotes, deliveries) + store links
  payments.py      Stripe (saved cards, Checkout pay links, refunds, webhook signatures) / mock
  llm/             OpenAI / Anthropic / offline adapters (strict JSON, menu-constrained enums)
  messaging/       BlueBubbles / Sendblue (+ typing indicators) / console
  pos/             canonical order JSON, console tickets, Square
  dashboard/       owner dashboard (server-rendered)
  scheduler.py     in-process minute ticker (autopilot, nudges, hourly relearn)
  api.py           FastAPI app: webhooks, voice, dashboard
```

The conversation flow is a strict state machine: `AWAITING_RESPONSE → NEGOTIATING_MODIFICATIONS → CONFIRMING_PAYMENT → (AWAITING_PAYMENT) → POS_INJECTION → COMPLETED`, with exits to `DECLINED`, `OPTED_OUT` and `FAILED`. The AI only reports an *intent*. Code decides the transitions, and an order can only be placed from `CONFIRMING_PAYMENT`.

```bash
pip install -e ".[dev]" && pytest                      # SQLite
BRUNO_TEST_DATABASE_URL=postgresql+psycopg://... pytest  # against Postgres
python -m bruno retell-config --base-url https://...    # prompt + function schemas for Retell
```

**Not built yet:**
- **Payments:** Square Payments as an alternative to Stripe. Without Stripe keys, a mock provider runs: only demo customers have "cards", and real imported customers pay at pickup.
- **POS:** Toast and Clover adapters. Square line items aren't mapped to your Square catalog yet.
- **Database migrations:** there's no Alembic yet; tables are created on startup.
- **Retell:** request signature verification (webhooks are protected by the shared secret).
- **Scaling:** running more than one web worker needs the per-customer lock moved to Postgres or Redis.
