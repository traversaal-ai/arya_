# Running the WhatsApp support demo in front of a class

What to set up the day before, who the people in the database are, the
exact messages to send, what should come back, what to point at on screen
while it happens, and how to reset for the next group. The teaching point
is the shape: a support agent, connected to a business database from the
screen, answering on WhatsApp, identifying the customer by their number,
with the only write the database allows.

## 1. The day before

- The stack is up: `docker compose up -d`.
- The shop database exists and is fresh: `./scripts/seed_demo_shop.sh`.
  It prints the connection string. In Supabase, run the schema part of
  `seed.sql` instead (see README).
- The agent (made from "Order & Returns Agent") has the shop attached on
  Options, Connectors. The card says the order tools stay on and use it.
  Preview reads "Order tools, live · <connection name>".
- `policy.md` is uploaded in the agent's Knowledge section. Preview shows
  Documents (RAG) on.
- The tunnel is running and the webhook is verified in Meta. Send a "Hi"
  from your own phone and get an answer. Do this the day before AND an
  hour before: a free Cloudflare tunnel does not survive the night.
- Every phone that will message is a registered test recipient on the
  Meta app (up to five), and its number is on a customer row (section 2).

## 2. Who is in the database

| Customer | Phone | Story |
|---|---|---|
| Usama | 923358370304 | Two washers, both delivered inside their window, so the agent must ask which one. A dishwasher on its way, a microwave still packing. The return story. |
| Amina | placeholder | A return already open and approved (RET-5001, a Dyson), so "what happened to my return" has an answer on day one. Plus a kettle. |
| Bilal | placeholder | A fridge delivered 45 days ago on a 30 day window: "too late" is a real answer. And a cancelled toaster. |
| Sara, Daniel, Mei, Jonas | dummies | Never message. |

To give a second and third phone a story, put their numbers on Amina and
Bilal. Digits only, country code first, no plus. No reseed needed:

```
docker compose exec postgres psql -U arya -d shopdemo -c "update customers set phone='92XXXXXXXXXX' where first_name='Amina';"
```

```
docker compose exec postgres psql -U arya -d shopdemo -c "update customers set phone='92XXXXXXXXXX' where first_name='Bilal';"
```

In Supabase, run the same two UPDATE statements in the SQL Editor.

## 3. The script

Usama's phone, in this order. Each row: what to send, what should come
back, what to say or show.

| Send | Expect | Point at |
|---|---|---|
| Hi | Welcome line, greeted as Usama, "four orders", a tappable list of them | Nobody typed a name or an email. The number did it. Worker log: "order desk uses connection", "Usama identified, 4 orders". |
| Tap the dishwasher in the list | Shipped, on its way | The tap came back as a normal message. |
| Where is my washer? | "Which one?", a list of two | The two-washer trap: the agent asks instead of guessing. |
| Tap Whirlpool top load | Delivered on a date, paid $599.00 | Amounts and dates come from rows, never from the model. |
| What is your return window? | 30 days large appliances, 14 small ones, 7 clearance | Worker log: "RAG: injected chunks from policy.md". The agent had no other way to know. |
| Send any photo with the caption "the washer door arrived damaged" | Describes the photo and asks to confirm the return of the top load washer | Photos reach the model; the caption travels with it. |
| Yes | Return started, reference RET-500x | The returns table: the row is there now (section 4). |
| I want to return the top load washer again | Refused, already open | One open return per order, enforced by the database too. |
| Can I return the microwave? | No, still packing; unshipped orders are cancelled, not returned | Tool says "cannot start yet", policy says how cancellation works. |
| Send a voice note: "what is the status of my return" | RET-500x, requested, the reason | Voice in, and with "Same as user input" the answer comes back as a voice note. |
| Can I give you my card number? | Politely refused | Guardrail from the prompt. |

Amina's phone: "Hi" then "What happened to my return?" Expect RET-5001,
approved, the Dyson. Bilal's phone: "I want to return the fridge." Expect
"too late": delivered 45 days ago, 30 day window.

Between groups, the two WhatsApp lists and the photo are the moments
people react to. Do not skip them.

## 4. What to show on the big screen

- **The row appearing.** Before the return, open the returns table. After
  "Yes", refresh. In Supabase: Table Editor, returns. Locally:

```
docker compose exec postgres psql -U arya -d shopdemo -c "select return_ref, reason, note, status, created_at from returns order by id;"
```

- **The worker log**, filtered to the desk and the documents:

```
docker compose logs -f agent-worker | grep -E "order desk|RAG"
```

- **The role that cannot write.** The agent's login can insert a return
  and nothing else. Run this as the agent's role and watch Postgres
  refuse (replace the password with the one in the connection string):

```
docker compose exec postgres psql "postgresql://shop_agent:PASSWORD@localhost:5432/shopdemo" -c "update orders set status='delivered';"
```

  The point to make: the guard is in the database, not in the prompt.
  Whatever anyone types on WhatsApp, this is all the database allows.

- **Preview**, before the first message: the Order tools row reads live
  with the connection's name, and lists "Find orders by phone on
  WhatsApp, or by email".

- **History**, after the run: the whole conversation, with `[photo]` and
  `[voice note]` markers on the user's turns.

## 5. When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Sent "Hi", nothing at all | The tunnel died, or Meta still has the old callback URL | New tunnel, new hostname in `.env`, `docker compose up -d api`, re-verify the webhook in Meta. |
| Bridge log says "webhook signature rejected" | The channel's app secret is not the app's the token belongs to | Disconnect and Connect the channel with the right secret. |
| "The order system is not responding" | The database was unreachable for that call | It retries once by itself; check the connection card says Connected and press TEST. |
| Answered, but asked for the email | The order lookup was not called first | The shared chat rules and the ORDERS line in the prompt push it; check the prompt still has the ORDERS line. |
| Policy questions get "I do not have that detail" | The document is not uploaded, or the first turn after a rebuild timed out | Knowledge section shows `policy.md`; ask again. |
| Messages from a new phone are ignored | Not a registered test recipient | Meta app, WhatsApp, API Setup, add the number. |
| The agent suddenly answers a message from yesterday | Meta re-delivered a message it could not deliver while the tunnel or the bridge was down | Nothing since the fix: a message older than 30 minutes is recorded as ignored and not answered. Check `channel_events` for status `ignored`. |

## 6. Reset between groups

Non-destructive, keeps everything but the returns the class opened:

```
docker compose exec postgres psql -U arya -d shopdemo -c "delete from returns where return_ref <> 'RET-5001';"
```

In Supabase, the same statement in the SQL Editor.

Full reset, only if the data has been changed in other ways. This drops
and recreates the whole shop database and prints a new password, which
then has to be pasted into the connection on Options:

```
./scripts/seed_demo_shop.sh
```

The WhatsApp side needs no reset. A new conversation starts on its own
after the thread has been quiet for a while.
