# Answering on WhatsApp

A **channel** lets a published agent answer on a messaging number as well
as on your website. The customer texts your WhatsApp number, Arya answers
with the same knowledge base, the same tools and the same connectors the
web widget uses. Nothing about the agent itself changes.

A channel is not a connector. Connectors are the agent reaching out to
something (a calendar, a database). A channel is a customer arriving.
That is why channels live on **Publish and deploy**, next to the share
link, rather than in Options.

## What you need from Meta

WhatsApp is Meta's network and there is no way around their platform. The
API itself is free; you pay Meta per message (see [Costs](#costs)).

**To try it out (about 20 minutes, free)**

1. A Meta developer account at developers.facebook.com.
2. An app created with the **Connect with customers through WhatsApp** use
   case, attached to a Meta Business account.
3. From **WhatsApp, API Setup** in that app: the **Phone number ID** and
   the **WhatsApp Business Account ID**. A free test number is provided
   automatically.
4. Add your own phone under **To** on that page. A test number can only
   message numbers you register there, up to five of them.
5. The **temporary access token** from the same page. It expires every 24
   hours, which is fine while you are testing.
6. The **App secret** from **Settings, Basic**. Arya uses it to verify
   that incoming messages really came from Meta.

**To go live**

7. **Business verification** in Meta Business Settings. Takes weeks, so
   start early. It raises your limit from 250 to 2,000 customers per day
   and is required before WhatsApp voice calls are possible at all.
8. **A dedicated phone number** that is not registered in the WhatsApp
   consumer app or the WhatsApp Business app. Delete it there first if it
   is. You verify it by SMS and set a six digit PIN.
9. **A permanent token**: Business Settings, System Users, create one,
   assign your app, grant `whatsapp_business_messaging` and
   `whatsapp_business_management`.
10. **A payment method** on the WhatsApp Business Account. Since 1 October
    2026 Meta charges for replies beyond the free monthly allowance and
    stops delivering without a card on file.

> **One webhook per Meta app.** Meta allows a single callback URL per app.
> If you already point that app at n8n, Chatwoot or anything else, Arya
> cannot also receive from it. Use a separate app, or move the webhook.

## Connecting a number

1. Publish the agent first. A channel hangs off a deployment, so the
   **Connect a WhatsApp number** button only appears once the agent is
   published.
2. Fill in the phone number ID, access token and app secret. Arya checks
   them against Meta before saving, so a wrong token fails here with
   Meta's own wording rather than silently at the first customer message.
3. Copy the **Callback URL** and **Verify token** Arya shows you.
4. In the Meta app, go to **WhatsApp, Configuration**, paste both, save,
   and subscribe to the **messages** field.
5. Message the number from a registered phone. You should get a reply
   within a few seconds.

### Making the webhook reachable

Meta must reach your server over HTTPS with a valid certificate. The
bridge listens on port 8030 on the host, published to localhost only.

- **Locally**, put a tunnel in front of it. A Cloudflare named tunnel is
  free and keeps a stable hostname, which matters because changing the URL
  means re-verifying it with Meta. ngrok's free static domain also works.

  On **Docker Desktop** the tunnel must run inside the same network
  namespace as the bridge. Docker Desktop does not forward ports for
  host-networked containers, so neither Windows nor your WSL distro can
  reach port 8030, and a tunnel started from an ordinary shell answers
  502. Run it as a container instead:

      docker run --rm --network host cloudflare/cloudflared:latest \
        tunnel --url http://localhost:8030

  This does not apply on a Linux host, where host networking means the
  bridge really is on the host and any reverse proxy reaches it.
- **In production**, route `/webhooks/whatsapp` to `127.0.0.1:8030` from
  whatever proxy already fronts the API.

Either way, set `ARYA_WEBHOOK_BASE_URL` to the public origin so the
Callback URL shown on Publish is the one to paste.

### The subscription Arya makes for you

A WhatsApp Business Account keeps a list of apps allowed to receive its
messages, and a newly created app is not on its own account's list. There
is no screen for this in either Meta portal and their docs do not mention
it, so the webhook verifies, the messages field is ticked, the app is
live, and nothing is ever delivered.

Arya performs that subscription when you connect a number and again when
you rotate the token, and the card reports the result. If it ever says
Meta is not delivering, the Fix subscription button retries it.

## How a conversation works

A WhatsApp thread is not a phone call, and Arya treats it accordingly.

- **No greeting.** The customer opened with a question. Answering "Hi, how
  can I help?" first would ignore it and cost an extra message.
- **Written, not spoken.** The agent's voice rules (short spoken
  sentences, reference numbers read digit by digit, never reading a link
  aloud) are replaced with rules for writing. Links are written out, order
  numbers look like order numbers, short lists are allowed.
- **Threads pause, they do not end.** After the quiet period you choose
  (ten minutes by default) Arya closes the session to free resources. The
  next message starts a new one and replays the tail of the last, so the
  customer can pick up an hour later and be understood.
- **Options become tappable lists.** When the agent offers choices, they
  arrive as a WhatsApp list; tapping one continues the conversation.
- **Visitor rules apply.** Exactly like the website widget, a WhatsApp
  customer gets only the tools you marked public, and never sees your
  internal document names.

### Photos

A customer can send a picture, with or without a caption, and the agent
answers about what is in it. The caption travels on the same turn, so
"what is this?" alongside a photo is one question, not two.

Whether a photo is read depends on the agent's model. The card says so
plainly: with a model that cannot see, photos are declined politely and a
hint on the card explains why, rather than leaving you to find out from a
customer. Setting Photos to off refuses them regardless.

The image is shrunk before it reaches the model, and it is not stored.
History records that a photo arrived, and its caption, not the picture.

### Voice notes

A voice note is transcribed and answered like any other message. The words
go into the conversation, so History, memory and search all see them.

Transcription and spoken replies use the agent's own provider: OpenAI,
Groq and the local stack over their speech routes, Gemini through Google's
API in the voice chosen on Options. With expressive delivery switched on
for a Gemini agent, each spoken reply is given the agent's expressive
settings as a direction ("say this warmly, without fillers"), the nearest
a plain-text reply can get to the call pipeline's expressive mode.

The agent can answer out loud too. The Reply setting decides:

- **Same as user input** (default): a voice note gets a voice note back, a
  typed message or a photo gets text.
- **Always text**: never speaks.
- **Always voice**: speaks every reply.

A reply goes out one way or the other, never both. Three things always
fall back to text: a reply too long to be worth listening to, a reply that
is nothing but a link, and any failure to produce audio. A customer is
never left with silence because speech was unavailable.

Links are unaffected by any of this. They travel separately and always
arrive as tappable text, because an address read aloud is useless.

### Knowing who is messaging

With the "Order & Returns Agent" template, the agent's order tools answer
from your own shop database as soon as you attach it: Options, Connectors,
Add database, paste the connection string, attach it to the agent. If the
database has the shop's tables (customers, orders, order items, returns,
see `demo/shop/README.md` for a ready-made one), the connector card says
the order tools will use it, and Preview lists them as live. Any other
database switches the sample order tools off, as before.

On WhatsApp the order tools then know the user by their number: no "what
is your email", the agent greets them by first name and lists their
orders. A number the database does not know falls back to asking for the
email, exactly as the website widget does. The greeting comes with a
tappable list of their orders, and when two orders could match what the
user said (two washers), the question arrives with a list of those two.
Tapping a row sends that product's name back as a normal message. If the
shop's products carry picture links, pictures appear where they answer
something and nowhere else: the two-way choice comes with both photos
above the list, a specific order comes with its photo the first time it
is looked up, and "show me a picture of the dishwasher" always gets one.
The overview list never carries photos, and a product is never pictured
twice unasked. Meta fetches the links itself, so they must be public and
must serve JPEG or PNG (an Unsplash link needs `fm=jpg`; its default
serves AVIF, which WhatsApp rejects after accepting the send). A second,
hosted link per product is sent if Meta reports the first failed, and a
shop without pictures simply sends none.

Every lookup is filtered by the identified customer inside the database,
so a user cannot reach someone else's order by naming it, in any phrasing.
The one write is starting a return, and the database role the connection
logs in as should be allowed to insert into the returns table and change
nothing else.

Without such a database attached, the order tools answer from built-in
sample data as they always have.

### What is not supported yet

Documents are declined politely with a line asking the customer to paste
the key details. Live WhatsApp voice calls need business verification and
a real number first.

## Settings

| Setting | What it does |
|---|---|
| Close a quiet thread after | Silence before Arya frees the session. The customer can always write again. |
| Photos | Whether pictures reach the agent. `Read them when the model can` defers to the model; `Always decline` refuses regardless. |
| Reply | Whether answers are written or spoken. See Voice notes above. |
| Pause | Stops answering without losing the setup. Messages that arrive while paused are ignored. |

Changing any of the first three marks it as pending and shows a Save
button. Nothing reaches live conversations until you press it. Pause and
Disconnect act immediately, as single deliberate actions.
| Send test | Sends one message to a number you own. Only works for a registered test recipient or someone who messaged you in the last 24 hours. |

### Raise your concurrent limit

Every open WhatsApp conversation counts as a session against the
deployment's caps, and those default to three because they were sized for
a website widget. A support number will turn customers away at three.
Twenty or more is a sensible starting point. The caps are on the same
Publish screen.

## Costs

You pay Meta, not Arya. As of October 2026:

- Replies inside the 24 hour window after a customer writes: the first
  1,000 per number per month are free, then roughly a third of a US cent
  each, varying by country.
- Messages you start outside that window need an approved template and
  cost more. Arya never sends these; it only replies.
- Incoming WhatsApp voice calls are free on Meta's side.

Your model, speech and search costs depend on the provider the agent
uses. Two of them are new here: reading a photo costs image tokens on the
model, and every spoken reply costs text to speech. Both go through
whichever provider the agent already uses, so no new account is involved.

## Troubleshooting

**Meta rejects the callback URL.** The verification GET must return the
challenge. Check the tunnel is running and the URL ends in
`/webhooks/whatsapp`, and that the verify token matches the one on
Publish exactly.

**Messages arrive but nothing answers.** Check the bridge log with
`docker compose logs -f arya-whatsapp`. A rejected signature means the app
secret is wrong. "no channel for number" means the phone number ID in the
webhook does not match the one saved.

**The customer gets "we have a lot of conversations open right now".**
The deployment's concurrent cap is full. Raise it on Publish.

**Replies stop after a day.** A temporary access token expired. Replace it
with a system user token, which does not.

**"Recipient phone number not in allowed list".** A test number can only
message the numbers registered on the API Setup page.

**A number answers as the wrong agent.** One number can serve one agent.
Disconnect it from the other agent first.
