# Agent Integration Guide

How to run the bundled demo page and how to embed a published Arya voice
agent into any website: where the script tag goes, the localhost and
deployed-engine variants, and how to set the widget's accent color.

---

## 1. Launch the demo page

The repo ships a demo page at [`demo/`](../demo/): a single Arya-branded
static page whose whole job is to host the widget, for testing it on a
page that is not the builder.

```bash
cd demo
python3 -m http.server 5500
```

Open http://localhost:5500. The page itself needs no backend; the voice
bubble appears once a valid key reaches the page (next section). The
snippet shown ON the page is display-only teaching text; the loader that
actually runs is injected from query params as described in section 3.

Always use `http://localhost:5500`, never `file:///...` (browsers only
grant the microphone on secure contexts, and localhost counts) and never
`http://0.0.0.0:5500` (a listen address, not a browsable one).

## 2. Get a key

1. Stack up: `docker compose up -d`.
2. In the builder, open the agent's **Share & embed** (Publish) screen and
   publish it.
3. Copy the `pk_...` public key. The key is restricted: it can only start
   calls with this one agent, and it only works against the engine that
   minted it (a local key will not work on a deployed engine, and vice
   versa).

## 3. Embed on the demo site (no file editing)

The demo page injects the loader from query params, so you never edit
`demo/index.html` (the file also carries a default key from the last
local demo; a `?key=` param always wins over it):

```
http://localhost:5500/?key=pk_YOUR_KEY                        local stack
http://localhost:5500/?key=pk_YOUR_KEY&engine=https://HOST    deployed engine
http://localhost:5500/?key=pk_YOUR_KEY&accent=%23708090       custom color
```

`engine` defaults to `http://localhost:8000`; `accent` takes a hex color
(URL-encode the `#` as `%23`).

## 4. Embed on a real website (the one-line script tag)

A customer pastes ONE line, copied from the builder's Share & embed
screen. It goes in the page's HTML, **just before the closing `</body>`
tag**, on every page that should show the assistant:

```html
    ...page content...

    <script src="http://localhost:8000/arya-widget.js"
            data-arya-agent-key="pk_YOUR_KEY" async></script>
  </body>
</html>
```

- **Local stack:** `src` points at `http://localhost:8000/arya-widget.js`.
  Only pages served on the same machine can use it.
- **Deployed engine:** `src` points at the engine host, e.g.
  `https://YOUR-ENGINE-HOST/arya-widget.js`, with a key minted by THAT
  engine (use the builder's push flow, then copy the snippet it returns).
  See [`DEPLOYMENT.md`](DEPLOYMENT.md).

Why before `</body>`: the loader appends its launcher to `document.body`,
so the body must exist; the `async` attribute keeps it from blocking the
page. Head placement also works because the loader waits for the DOM, but
end-of-body is the convention the builder's snippet uses.

The loader is self-contained (shadow DOM), roughly 3 KB, and never
interferes with the host page's styles. If the key is invalid, revoked, or
the origin is not allowed, the widget hides itself entirely rather than
leaving a dead bubble.

## 5. Styling: accent color and other attributes

The script tag accepts optional `data-` attributes:

| Attribute | Default | What it does |
|---|---|---|
| `data-arya-accent` | `#5a63d6` | Launcher and widget accent, any hex color |
| `data-arya-theme` | `light` | `dark` opts the panel into dark mode |
| `data-arya-position` | `bottom-right` | Corner for the launcher |
| `data-arya-z-index` | `2147483000` | Stacking order on busy pages |

Example matching a site's brand color:

```html
<script src="https://YOUR-ENGINE-HOST/arya-widget.js"
        data-arya-agent-key="pk_YOUR_KEY"
        data-arya-accent="#8B5E4F"
        data-arya-theme="light" async></script>
```

Pick the accent from the host site's own palette so the bubble reads as
part of the brand (the `#8B5E4F` above is an example for a warm,
earth-toned site). The demo page passes no accent at all: the loader's
default is already the Arya indigo. To try another color there, use the
`?accent=` query param instead of an attribute.

## 5b. What visitors see under an answer

The agent speaks short answers and, per the voice rules, only ever says a
site's name out loud ("it's on github dot com") and points at the chat for
the actual link. What appears under the bubble, top to bottom:

- **The one link the answer gives.** When the agent names a page or the
  caller asks for one ("send me the link"), a single chip appears with the
  page's domain, a short path and its title as a tooltip: click opens it in
  a new tab. This is deliberately never the full list of what was searched,
  even if the search itself returned several results; the model's own
  typed text is only a hint, the href always comes from the search result
  it is verified against, so a small local model naming a slightly wrong
  address still links to the real page.
- **Flight and hotel cards** from the travel concierge: tapping a row is
  the same as saying "option 2". The choice is sent to the agent as a typed
  message, the agent confirms and saves it, and the row keeps a check mark
  afterward so the caller can see what was picked. Rows carry no outside
  links; the written itinerary (the "View itinerary" button) is where the
  Google Flights and Maps links live.
- **A collapsed "Sources" line**, one row reading "Sources · 3": opening it
  shows the same domain/title/snippet cards as before, for a caller who
  wants to see everything the agent searched rather than only the one link
  it gave. Off by default is not the setting; **on** is, but an owner can
  turn it off per agent (see below) for a site that should show only the
  direct link and nothing else.
- **Document citations are never shown to visitors.** When the agent
  answers from uploaded documents, the file names and excerpts stay on the
  server for embed and share-link calls; only the builder and History show
  them. Visitors still hear the answer, they just do not see which internal
  document it came from.

**Turning the Sources line off:** open Publish & deploy for the agent and
use the "Sources under answers" chip in the usage row. This only affects
the collapsed list; the single link chip above it always shows, since it
is what a caller actually asked to be sent.

Nothing else to configure: the chips and cards render inside the widget and
the share-link page automatically, and inherit the accent color from
section 5.

## 6. Allowed origins

On the Share & embed screen, the deployment's **allowed websites** list
controls who may use the key:

- Empty list: any origin works. Fine for local testing, lock it down
  before going live.
- Non-empty list: it must contain the page's exact origin, port included,
  e.g. `http://localhost:5500` or `https://www.customer-site.com`.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No bubble at all | Key invalid/revoked, origin not allowed, or the loader URL is unreachable. Open the `arya-widget.js` URL directly in the browser to check the engine. |
| Bubble appears, call fails | Stack not fully up (worker or LiveKit still starting) or the agent is missing a required key in the builder (the Preview screen lists problems). |
| Mic permission never asked | Page opened via `file:///` or a non-localhost http origin. Serve over http on localhost, or https in production. |
| Works locally, dead when deployed | The page still points at `localhost:8000` or still uses the local key. Deployed pages need the engine URL AND the engine-minted key, plus the real origin in allowed websites. |

## 8. How visitors are identified

A visitor is **one browser**. On the first call the engine mints a signed
token and the widget stores it in that browser's `localStorage` (key
`arya_wt_<first 12 chars of the public key>`). Every later call sends it
back, so an agent with memory switched on recognises the returning visitor
and recalls what they said before.

What that means in practice:

- A new device, a new browser profile, or cleared site data is a **new
  visitor** with no history. There is no account to log into.
- The token only works for the agent that minted it. The same person
  talking to two of your published agents is two visitors.
- The token is signed by the engine, so a page cannot pick an identity:
  an edited or copied token is ignored and the visitor simply starts over.
  Signing does **not** protect a token that is stolen from the page itself
  (a script injected into your site, or someone else using the same
  device). Only your own site login could do that, and the widget does not
  use it.
- Builder test calls are a separate visitor from widget calls, even for
  the same agent.

To see or delete what an agent remembers about someone, open **Visitors**
in the builder (also for a deployed engine, through its Source dropdown).
**Forget this visitor** removes their memories across all agents and every
transcript of theirs in one step. The same action over the API:

```bash
curl -X DELETE http://localhost:8000/api/visitors/<visitor id> -H "X-Arya-Admin-Token: <token>"
```
