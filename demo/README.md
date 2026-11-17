# Widget demo page

A standalone static page (`index.html`) for testing the embed widget
outside the builder, exactly the way a real customer would paste it into
their site. It is styled after the Arya theme and its whole job is
to host the bubble.

The embedded agent key defaults to the one baked into the page. Override
it (or the engine, or the widget color) with query params:
`?key=pk_YOUR_KEY&engine=https://YOUR-ENGINE&accent=%235a63d6`.
Opened with an empty `?key=`, the page shows a corner note explaining
where to get one instead of loading the widget.

## Test against the local stack

1. Have the stack running: `docker compose up -d` (the widget loader is
   served by the api container at `http://localhost:8000/arya-widget.js`).
2. In the builder, open your agent's **Preview -> Share & embed** screen
   and publish it. Copy the `pk_...` key from the snippet.
3. Serve this folder on any port:

   ```bash
   cd demo && python3 -m http.server 5500
   ```

4. Open it with the key as a query param (no file editing needed):

   ```
   http://localhost:5500/?key=pk_YOUR_KEY
   ```

The bubble appears bottom-right. Click it and talk.

## Test against a deployed engine

Point the page at the engine instead of the local stack:

```
http://localhost:5500/?key=pk_YOUR_KEY&engine=https://YOUR-ENGINE-HOST
```

See [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) for deploying an engine.

## If the bubble does not appear or errors

- **Allowed websites**: if the deployment's allowed-origins list is not
  empty, it must contain this page's exact origin, port included
  (`http://localhost:5500`). An empty list allows any origin, which is
  fine for local testing but should be locked down before going live.
- **Microphone**: browsers only grant the mic on secure contexts.
  `http://localhost` counts; opening the file directly (`file:///...`)
  does not. Always go through a server.
- **Engine down**: the widget hides itself when the loader script or the
  key cannot be resolved. Check that the engine URL is reachable and the
  key has not been rotated or revoked.
- Use `http://localhost:5500`, not `http://0.0.0.0:5500` (0.0.0.0 is a
  listen address, not a browsable one).
