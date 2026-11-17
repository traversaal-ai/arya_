# Connecting external tools (connectors)

A connector lets an agent act on a real system during a call instead of
answering only from its prompt and documents: check a calendar and book a
slot, look something up in a tool server you run, and so on. Arya reaches
these systems through the Model Context Protocol (MCP): the external side
runs an MCP server that exposes tools, and the agent calls those tools
mid-call. The agent never talks to a database or an API directly.

Five kinds ship today:

- **Cal.com (appointment booking)**: real bookings that respect your
  working hours, appointment lengths and buffers. Start here if the agent
  should book anything.
- **Google Calendar**: Google's hosted Calendar MCP server. You sign in
  with the Google account whose calendar the agent should use. A raw
  calendar knows what is busy but not what your hours are, so prefer
  Cal.com for booking.
- **Your database (Postgres, read-only)**: your own orders, customers or
  bookings database. The agent reads live rows, never writes.
- **Composio**: one URL that covers Google Calendar, Gmail, Shopify,
  HubSpot, Slack and hundreds more, with Composio handling the sign-in for
  each. Useful when a vendor's own server needs an approval you do not have
  yet.
- **Custom MCP server**: any MCP server reachable over HTTP (streamable
  HTTP or SSE), with an optional bearer token. This is also the slot for
  anything you build yourself.

## Connections are shared, attachment is per agent

You connect a system once (Options, Connectors section). That connection
is then available to every agent. Each agent decides on its own Options
screen whether to attach it and which of its tools it may call. Removing a
connection that agents still use asks you to confirm and then detaches it
from those agents.

Credentials (tokens, sign-in grants) stay on this machine's database and
are never sent to the browser or included in an exported agent.

## Sample tools versus real ones

Most templates ship with a demo desk: the appointment templates can book
imaginary slots, the retail one can look up imaginary orders. That is so a
fresh agent demos without any setup, and it invents plausible answers.

So when you attach a real connector that covers the same ground, Arya ticks
**Replaces the sample appointment tools** (or order tools) on the
attachment for you. The demo desk then stops being built for that agent and
only the real system is used. You can untick it, but then the agent has two
ways to answer the same question and may pick the invented one, which is
why Preview complains until one of them wins.

If the real system is unreachable when a call starts, the agent says it
cannot look that up. It deliberately does not fall back to the demo desk:
inventing an order status or a booking that does not exist is worse than
admitting the system is down.

## Composio

Composio fronts Gmail, Google Calendar, Shopify, HubSpot and about 200 more
apps through one connection, and does the vendor sign-in itself.

1. Sign in at [composio.dev](https://composio.dev) and create an MCP server.
2. Add only the toolkits this agent needs. A Composio server exposes one
   "execute" tool that can run anything on it, so a server with Gmail on
   it can send AND delete mail. Make one server per purpose rather than one
   server with everything.
3. Open the auth link and sign in to the app you added. Until you do, the
   server has no tools.
4. Copy the server URL from the config Composio shows you. It ends in
   `?user_id=...`.
5. Copy your API key from **Settings, API keys**. That is a different page
   from the server.
6. In Arya: Options, Advanced settings, Connectors, **Add Composio server**.
   Paste both, press **Test**, then tick the tools you want.

Where a dedicated connector exists for the same job, such as Cal.com for
appointments, that one is faster: Composio looks a tool up before running
it, which adds a few seconds per action.

## Cal.com (appointment booking)

Cal.com holds the rules a calendar does not: which services you offer, how
long each takes, which hours you work, how much buffer you need between
appointments. It syncs to your Google or Outlook calendar and emails the
confirmation itself, so the agent only has to ask, offer and book.

1. Sign up at [cal.com](https://cal.com) (the free plan is enough) and
   connect your Google or Outlook calendar if you want the bookings to
   appear there.
2. Create at least one **event type**, for example "30 Minute
   Consultation". This is what the agent offers callers. Set your
   availability on it.
3. In Cal.com go to **Settings, Developer, API keys** and create a key. It
   starts with `cal_`.
4. In Arya: Options, Advanced settings, Connectors, **Connect Cal.com**.
   Name it and paste the key. Arya checks the key, names the account it
   reached, and warns if the account has no event types yet.
5. Tick **Attach to this agent**. The read tools are pre-selected; also
   tick `book_appointment`, and `reschedule_appointment` or
   `cancel_appointment` if callers should be able to change bookings. They
   are marked **writes** and start unticked.
6. Leave **Replaces the sample appointment tools** ticked, so the demo desk
   is switched off.

What the agent does with it:

- `list_services` when the caller is vague about what they want.
- `find_slots` before offering any time, so it only offers hours you work.
- `book_appointment` after confirming the date, time, name and email aloud.
- `find_my_bookings`, then `reschedule_appointment` or
  `cancel_appointment`, for changing an existing booking.

The agent always asks which time zone the caller is in, because "3 pm"
means nothing without it, and it never reads a booking reference aloud.
Cal.com sends the confirmation email, so the agent says the email is on its
way rather than promising a text message.

Public embed sessions: ticking **Allow in public embed sessions** lets
website visitors book. For booking that is usually what you want. Think
twice about including `find_my_bookings` there, since a visitor could look
up someone else's appointment by guessing an email address.

## Custom MCP server

1. Options, Connectors, **Add server**.
2. Name it, paste the server URL (most servers end in `/mcp`), and a
   bearer token if the server wants one.
3. Save. Arya connects and lists the server's tools before storing
   anything; a wrong URL or token fails here with the reason.
4. Tick **Attach to this agent**, then pick the tools the agent may call.
   Tools that create or change data are marked **writes** and start
   unticked.

If the server runs on this same machine and Arya runs in Docker, use
`http://host.docker.internal:<port>/mcp`, not `localhost`: inside the
container, localhost is the container itself.

## External database (Postgres, read-only)

This is your own database, not Arya's: the one holding your orders,
customers, patients or bookings. The agent answers "where is my order" from
live rows instead of an uploaded document. Postgres only, for now.

1. Create a **read-only role** for the agent. On your database server:

   ```sql
   CREATE ROLE arya_readonly LOGIN PASSWORD 'choose-something-long';
   GRANT CONNECT ON DATABASE yourdb TO arya_readonly;
   GRANT USAGE ON SCHEMA public TO arya_readonly;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO arya_readonly;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public
     GRANT SELECT ON TABLES TO arya_readonly;
   ```

2. Options, Connectors, **Add database**. Name it and paste the connection
   string:

   ```
   postgresql://arya_readonly:choose-something-long@your-host:5432/yourdb
   ```

3. Save. Arya connects, reports the database and user it reached, and warns
   if the login is a superuser (a superuser can bypass the read-only guard,
   so use the role above instead).
4. Attach it to the agent and tick the tools: `list_tables`,
   `describe_table` and `run_query`. Add what the tables mean under "What
   these tools cover", for example "orders and customers; order status
   lives in orders.status".

The agent can only read. That is enforced four ways, so a prompt injection
in your data cannot turn into a write: only a single SELECT (or WITH) is
accepted, the session sets `default_transaction_read_only`, every query runs
in a read-only transaction that is rolled back, and there is a three second
statement timeout with a 50 row cap.

### When the database is a shop

Attaching a database normally switches an agent's sample order tools off.
One exception: if the database has the four tables the order tools were
written for (`customers`, `orders`, `order_items` and `returns`, the layout
in `demo/shop/`), the card says so and the order tools stay on and run over
it instead. They then know a WhatsApp user by their number, look up only
that customer's orders, and can start a return, which is their one write.
Give that role INSERT on `returns` as well as SELECT, and nothing else. The
read-only query tools above stay available alongside.

This connector runs inside Arya's own `arya-mcp` service, because no vendor
hosts an MCP server for a private database. It starts with the stack. If the
Add button says the service is not running:

```
docker compose up -d arya-mcp
```

Databases other than Postgres are not supported yet.

## Google Calendar

Everything happens in two places: [Google Cloud
Console](https://console.cloud.google.com/) once, and the Options screen per
agent. About ten minutes.

**In Google Cloud Console**

1. **Project**: pick an existing one or create one (top-left picker).
2. **Enable two APIs** (APIs and Services, Library): search **Google
   Calendar API**, Enable; search **Google Calendar MCP API**, Enable. If
   the second is missing, enroll in the Google Workspace Developer Preview
   Program and look again.
3. **Branding** (Google Auth Platform, Branding): app name `Arya` (anything),
   your email as user support and developer contact. Leave logo, home page,
   privacy policy and terms empty.
4. **Audience** (Google Auth Platform, Audience): type External, status
   Testing, leave it there. Under **Test users** add the Google account
   whose calendar the agent will use. Sign-in fails with "access blocked"
   for any account not listed.
5. **Data Access** (Google Auth Platform, Data Access, Add or remove
   scopes): paste these three into "Manually add scopes", one per line,
   then Add to table, Update, Save:

   ```
   https://www.googleapis.com/auth/calendar.events
   https://www.googleapis.com/auth/calendar.calendarlist.readonly
   https://www.googleapis.com/auth/calendar.events.freebusy
   ```

   Keep `openid` and `userinfo.email` ticked too, nothing else. For a
   read-only agent use `calendar.events.readonly` as the first line; then
   `create_event` will not work.
6. **Clients** (Google Auth Platform, Clients, Create client): type Web
   application, name `Arya`. Under Authorized redirect URIs add exactly the
   URI shown on the Google Calendar card in Options. Locally that is
   `http://localhost:8000/api/connections/oauth/callback`; a public
   deployment uses `https://<your-domain>/api/connections/oauth/callback`
   and needs `PUBLIC_BASE_URL` set (Google requires HTTPS there). Create,
   then copy the Client ID and the Client secret (shown once).

**In Arya (Options, Advanced settings, Connectors, Google Calendar card)**

7. Expand **OAuth app**. Paste the client id, Save; paste the client secret,
   Save. These are stored once and shared by every agent.
8. Click **Connect Google Calendar** and choose the test-user account on
   Google's screen. On "Google hasn't verified this app" click Advanced,
   then "Go to Arya (unsafe)", then Allow; that is normal for a Testing-mode
   app. You return to the same Options screen with the connection attached.
9. Click **Test** once to load the tool list: list_calendars, list_events,
   get_event, suggest_time, create_event, update_event, delete_event,
   respond_to_event. Read tools are pre-ticked; tick `create_event` (marked
   writes) if the agent should book. Tick "Allow in public embed sessions"
   only if website visitors may trigger the ticked tools; for booking, tick
   `suggest_time` and `create_event` there and leave `list_events` and
   `delete_event` off.
10. Continue, Preview, Test agent: "Am I free tomorrow at 10?" should call
    `suggest_time` or `list_events`; "Book John at 3 pm" calls
    `create_event` after a spoken confirmation. The worker log
    (`docker compose logs -f agent-worker`) shows `connector 'Google
    Calendar': N tool(s)` at call start.

Testing-mode limits: Google expires the sign-in after 7 days for apps in
Testing mode. The card then shows **Needs sign-in**; click Sign in again.
Publishing the app in Google Cloud removes that limit but requires
Google's verification because calendar scopes are sensitive.

## Public embed sessions

By default a connector only runs on calls you place from the builder.
Visitors on your website (the embedded widget or share link) get nothing
unless you tick **Allow in public embed sessions** on the attachment. Turn
it on only for tools that are safe for anonymous callers: for appointment
booking that is typically `suggest_time` and `create_event`, while
`list_events` (other customers' names) and `delete_event` stay off.

Every tool call made by any agent is recorded in the `tool_events` table
(tool name, arguments, success, latency, conversation id).

## Troubleshooting

- **Needs sign-in**: the grant expired or was revoked. Click Sign in.
- **Error after Test** on Google Calendar: usually the Calendar MCP API is
  not enabled in the Cloud project, or the account is not a test user.
- **unreachable / timed out** on a custom server: check the URL from the
  machine Arya runs on; see the `host.docker.internal` note above.
- **server rejected the token**: the bearer token is wrong or expired.
  Remove the connection and add it again with the current token.
- A connector that fails to connect when a call starts is skipped and
  logged (`connector '<name>' unavailable`); the call still goes ahead
  without it. Preview blocks Publish while a connector needs sign-in or
  has no tools selected.

Cal.com, specifically:

- **a Cal.com API key starts with cal_**: you pasted something else, an
  OAuth token or another service's key.
- **Cal.com rejected the API key**: it was revoked or mistyped. Create a
  new one under Settings, Developer, API keys.
- **this account has no event types yet**: create one in Cal.com, then
  click Test. Without an event type there is nothing to book.
- The agent keeps asking for a time zone: that is deliberate. Tell it in
  the system prompt if your callers are always in one place, for example
  "callers are in Pakistan unless they say otherwise".
- The agent offers no times at all: check the availability on that event
  type in Cal.com, and that the window it searched is not entirely outside
  your working hours. It looks two weeks ahead by default.

Database connector, specifically:

- **the arya-mcp service is not reachable**: run
  `docker compose up -d arya-mcp`.
- **the database rejected the username or password**, **that database name
  does not exist**, **the database could not be reached**: the connection
  string is wrong, or the database does not accept connections from this
  machine. Test it with `psql "<your connection string>"` first.
- **this database user is not allowed to read that**: grant SELECT to the
  read-only role (see the GRANT lines above).
- **the query took longer than 3 seconds and was stopped**: the agent asked
  for something too big. Index the column it filters on, or narrow the
  question.
- Agents on a **public embed** get database tools only if you ticked "Allow
  in public embed sessions" on the attachment. Think twice: a visitor could
  ask about someone else's order. Prefer keeping the database owner-only and
  exposing a narrower lookup instead.
