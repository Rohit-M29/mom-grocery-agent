# Mom's Grocery Agent

A WhatsApp voice helper for someone who isn't comfortable with apps. She sends a voice
note in Hindi, Marathi or English ("do litre Amul doodh aur ek kilo shakkar"). The agent
checks the quick-commerce apps, replies **by voice in her language** with the best price
and where it is, adds items to the cart when she agrees, and places the order only after
she says "haan".

```
Mom (voice): "Do litre doodh aur ek kilo shakkar"
Agent (voice): "Amul Taaza 1 litre Zepto par 68 रुपये है, Instamart par 70. Do packet Zepto se loon?
                Shakkar 1 kilo 48 रुपये. Total 184 रुपये, Zepto Cash se. Order kar doon?"
Mom: "Haan"
Agent: "Order ho gaya."      (and you get a WhatsApp copy of the order)
```

## How it works

```
WhatsApp voice note
  -> Sarvam speech-to-text (Hindi / Marathi / English, auto-detected)
  -> Claude, with tools:
       - each shopping app's official MCP tools (search, cart, checkout)
       - compare_offers: plain code that matches pack sizes and ranks prices
       - propose_order: saves the order and checks spending limits before asking
  -> Sarvam text-to-speech in the same language
  -> WhatsApp voice note + text reply
```

| App | Status | Can order? |
|---|---|---|
| Zepto | Official MCP, log in once with your phone OTP | Yes |
| Swiggy Instamart | Official MCP, **needs approval** (builders@swiggy.in) | Yes, up to Swiggy's cart limit |
| Blinkit | No official option; prices via a comparison service | No: the agent says "cheapest on Blinkit" and you order it yourself |

### Safety rails (in code, not left to the AI)

- **Exact sizes.** "1 litre" never quietly becomes "200 ml". `compare_offers` separates
  exact matches, equivalent multipacks (2 x 500 ml) and other sizes, and the agent is told
  to ask when nothing matches exactly. See `grocery_agent/units.py`.
- **Spoken yes required.** A checkout tool runs only if, in an *earlier* message, the agent
  saved a proposal (platform, items, total), and the *current* message is a clear yes
  ("haan", "हो", "ठीक है", "yes"...) with no "nahi"/"नको"/question in it. See
  `grocery_agent/guard.py`.
- **Prepaid only, never cash on delivery.** Pays from the app wallet (Zepto Cash, no PIN
  needed), or sends a UPI payment link to you on WhatsApp so a person pays with their PIN.
  COD is refused in three separate checks.
- **Spending limits.** Per order (default ₹1,500) and per week (default ₹5,000). The wallet
  balance is a hard ceiling on top of that.
- **Only allowed numbers** can talk to the bot; WhatsApp webhook signatures are checked.
- **Compare-only apps** can never touch a cart or order.
- **You get a copy** of every order on WhatsApp.

The full review against OWASP's LLM Top 10, RBI payment rules, India's DPDP Act and
WhatsApp policy, plus what's still open, is in [SECURITY.md](SECURITY.md).

## Setup

You need: Python 3.11+, Node.js (for `npx mcp-remote`), and `ffmpeg`.

```bash
git clone https://github.com/Rohit-M29/mom-grocery-agent && cd mom-grocery-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                  # fill in keys, see below
cp servers.example.json servers.json  # choose which apps are on
pytest                                # 92 tests, no keys needed
pytest tests/test_end_to_end.py -s    # prints a simulated WhatsApp conversation
```

### 1. Keys

| What | Where | Cost |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com | Pay per use. Haiku 5.5 is the cheapest model, about $0.10 per million input tokens |
| `SARVAM_API_KEY` | dashboard.sarvam.ai | Starts with free credits |
| WhatsApp Cloud API | developers.facebook.com → create app → WhatsApp | Replies to messages she sends are free within 24 hours (check Meta's current pricing) |

### 2. Connect Zepto (one time)

Use a **separate Zepto account for the bot**, with a small Zepto Cash balance and no
saved cards (see [SECURITY.md](SECURITY.md#bot-accounts-least-privilege)).

```bash
npx -y mcp-remote@0.15.2 https://mcp.zepto.co.in/mcp
chmod 700 ~/.mcp-auth
```

A browser opens. Log in with the bot's Zepto account (phone + OTP). The login is saved in
`~/.mcp-auth`. If the agent runs on a server without a browser, do this on your
laptop and copy `~/.mcp-auth` to the server.

Then check what Zepto exposes and which tools count as placing an order:

```bash
python -m grocery_agent.cli --list-tools
```

Every tool that places an order or takes payment must show `<- PLACES ORDER`. If one
doesn't, add its name to `order_tools_pattern` for that app in `servers.json`. Also check
how the checkout tool takes the payment method. The agent must pass `zepto_cash` or `upi`
explicitly, and checkout is blocked if no payment method is named.

**Top up the bot's Zepto Cash** with about a week's groceries. That balance is the most the
bot can ever spend by itself.

### 3. Try it in the terminal first

```bash
python -m grocery_agent.cli            # type: "1 litre Amul doodh chahiye"
python -m grocery_agent.cli --voice    # also saves spoken replies to replies/*.ogg
```

To try without any real app, set `"enabled": true` on the `demo` entry in `servers.json`.

### 4. WhatsApp

1. In Meta's developer console, create an app → add WhatsApp → note the **phone number
   ID** and a **permanent access token** (System User token). Use a spare number, not your
   personal WhatsApp.
2. Run the server where WhatsApp can reach it:
   ```bash
   uvicorn grocery_agent.server:app --host 0.0.0.0 --port 8000
   ```
   For testing from a laptop: `cloudflared tunnel --url http://localhost:8000` (free).
3. In WhatsApp → Configuration, set the webhook URL to `https://<your-host>/webhook`, the
   verify token to `WHATSAPP_VERIFY_TOKEN`, and subscribe to **messages**.
4. Save the bot's number in Mom's phone as "Grocery" and send it a voice note.

### 5. Hosting for ₹0

Any always-on machine works: an old laptop at home, a Raspberry Pi, or a free cloud VM
(for example Oracle Cloud's free tier). It needs Python, Node and ffmpeg.

### Adding Instamart and price comparison

- **Instamart:** email builders@swiggy.in for access. When approved, put the token in the
  `instamart` entry of `servers.json` and set `"enabled": true`.
- **Blinkit prices:** sign up at quickcommerceapi.com (covers Blinkit, Zepto, Instamart and
  more by pincode), put its MCP URL and key in the `compare` entry, and enable it. It stays
  compare-only.

## Things to verify before trusting it with real orders

- **Sarvam API fields** (`speech.py`) follow Sarvam's documented request shape as of 2025
  (`/speech-to-text` with `language_code=unknown`, `/text-to-speech` returning base64
  audio). Confirm against docs.sarvam.ai; adjust the model or speaker names in `.env`.
- **Zepto's tool names** are discovered at runtime. Run `--list-tools` (step 2) and confirm
  checkout is flagged before enabling ordering.
- **First real order:** do a small one (one packet of milk) paid from the bot's Zepto Cash while
  you watch the logs.

## Project layout

```
grocery_agent/
  agent.py     Claude loop, system prompt, local tools, order gating
  commerce.py  connects to each app's MCP server, routes tool calls
  guard.py     yes/no detection (hi/mr/en) and spending limits
  units.py     pack-size parsing and offer ranking
  speech.py    Sarvam speech-to-text / text-to-speech, ffmpeg conversion
  whatsapp.py  WhatsApp Cloud API client and signature check
  server.py    FastAPI webhook
  cli.py       terminal mode for testing
  store.py     SQLite: conversations, proposals, orders, preferences
tests/         92 tests: unit tests, tool tests against fake shops over MCP, and a full
               simulated WhatsApp conversation (tests/test_end_to_end.py)
```
