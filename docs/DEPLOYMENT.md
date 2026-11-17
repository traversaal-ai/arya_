# Deploying an Arya engine on Google Cloud Platform (GCP)

The engine is the deploy target for the builder's **Publish & deploy → Deploy to engine** flow. It serves the share link, the embeddable widget, and the public call API. You build agents locally; the engine runs them publicly. This guide was written by actually performing every step; the caveats are real ones hit along the way.

## Requirements

| Thing | Why |
|---|---|
| GCP account with billing | the VM |
| e2-standard-2 (2 vCPU, 8 GB), Ubuntu 24.04 LTS x86/64, 50 GB disk | the full stack includes local models (Ollama, Speaches) which need about 8 GB RAM and about 25 to 30 GB of disk after the first build. CPU is enough: the whole stack is CPU-built, a GPU only ever improves local-model latency |
| A static external IP (recommended; ephemeral is fine for a one-off test) | the hostname, and every pasted snippet, must not change. Ephemeral IPs survive reboots but change on stop/start |
| Open ports: 80, 443, 7881 (TCP) and 50000-50100 (UDP) | HTTP/TLS, LiveKit TCP fallback, and the WebRTC audio itself |
| No domain purchase needed | `<ip>.sslip.io` resolves any dash-style name to that IP (example: `34-133-176-4.sslip.io` resolves to `34.133.176.4`), which is enough for free Let's Encrypt TLS via Caddy. Swap in a real domain later by changing two hostnames |

## 1. Create the VM

Console: Compute Engine → Create instance. Everything not listed below stays at its default.

| Section | Setting |
|---|---|
| Machine configuration | **e2-standard-2** |
| OS and storage | **Ubuntu 24.04 LTS, x86/64 (amd64)**, NOT Arm64 (the Docker images are amd64). Boot disk: **balanced persistent disk, 50 GB** (the 10 GB default cannot hold the first build) |
| Networking → Firewall | check BOTH **Allow HTTP traffic** and **Allow HTTPS traffic** (Caddy needs 80 for the certificate and 443 to serve) |
| Networking → network interface | External IPv4: **Reserve static external IP** for a real deployment. For a throwaway test, Ephemeral is acceptable. Leave the NIC (gVNIC) and everything else at defaults |
| Networking → Network tags | Only if you use the tag-scoped firewall rule from step 2 (shared projects): add **arya-engine** here so the audio ports apply to this VM. A dedicated project using the untagged rule leaves this empty. An existing VM can be tagged later: `gcloud compute instances add-tags <vm-name> --tags arya-engine --zone <zone>` |
| Network Service Tier | **Premium** (the default). Voice is jitter-sensitive; Premium rides Google's backbone |
| Observability | Ops Agent optional (pennies at this volume, handy for a first deploy) |
| Security | all defaults (the stack uses no Google APIs) |
| Advanced | optional: **Enable deletion protection** for a long-lived engine. For a test, skip it, and remember to DELETE the VM when done: billing is per second while it exists, about $54/month |

## 2. Open the audio ports

The create-form checkboxes only covered 80/443. The WebRTC media ports must be added as a separate rule or calls will connect with SILENT audio (the number one trap).

Run this in **Cloud Shell** (the `>_` icon top-right in the Cloud Console). Not inside the VM: the VM's service account lacks Compute permissions, and the firewall lives in front of the VM, not on it.

```bash
gcloud compute firewall-rules create arya-audio \
  --allow tcp:7881,udp:50000-50100
```

Firewall rules are per NETWORK, not per VM: this covers every instance in
the project's default network, so a second engine in the same project needs
no new rule. That cuts both ways in a project shared with other workloads
(every VM gets these ports opened, and their untagged rules open theirs on
your engine; check with `gcloud compute firewall-rules list`). To scope
rules to tagged VMs only, add `--target-tags arya-engine` to the rule and
tag the engine: `gcloud compute instances add-tags <vm-name> --tags
arya-engine --zone <zone>` (or set **Network tags** in step 1's create form;
tag VMs BEFORE restricting an existing rule, so audio never has a gap). A
dedicated project doesn't need this.

## 3. Install Docker and get the code (on the VM, via SSH)

Open SSH from the Console (the SSH button on the VM row).

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER
```

The group change needs a fresh login: close the SSH window and open a new one (or run `newgrp docker` in the current shell). Verify with `docker ps`; an empty table is correct, "permission denied" means the re-login did not happen yet.

**Cloning.** While the repo is private, plain clone fails ("password authentication is not supported"). Create a GitHub token first: Settings → Developer settings → Fine-grained token, resource owner set to the org, repository `arya`, permission **Contents: Read-only**, short expiry. If the org blocks fine-grained tokens (the symptom is a misleading 403 "Write access to repository not granted"), fall back to a classic token with the `repo` scope. Use the token as the password at the clone prompt; never embed it in the URL. Delete the token from GitHub once done.

```bash
git clone https://github.com/traversaal-ai/arya_.git arya && cd arya
# The clone lands on main, which is the release branch: stay on it. An
# engine on a work branch would pull half-finished code on every update.
./scripts/init.sh         # generates .env, livekit.yaml, seaweedfs-s3.json
# The containers run as uid 1000; on GCE your login user is often 1001.
# Without this, pushing an agent later fails with a PermissionError:
sudo chown -R 1000:1000 backend/data
```

## 4. Configure for public serving

**Where to find your IP:** Console → Compute Engine → VM instances → the **External IP** column (example: `34.133.176.4`).

Set it ONCE as a shell variable, dots replaced by dashes. Every later command expands `$IP` itself, so nothing needs hand-editing. If you open a new terminal later, set it again first. This dash-style hostname is the one to use EVERYWHERE, including the browser: the dot form resolves too, but Caddy only holds a certificate for the dash form, and mixing them shows up as ERR_SSL_PROTOCOL_ERROR.

```bash
IP=34-133-176-4           # <- YOUR external IP, dash-style
```

Then, in `~/arya`:

```bash
# LiveKit must advertise the VM's PUBLIC IP for WebRTC:
sed -i 's/use_external_ip: false/use_external_ip: true/' livekit.yaml
grep use_external_ip livekit.yaml     # confirm: use_external_ip: true

# Engine settings, appended to the .env that init.sh generated. The last
# line is where Meta reaches the WhatsApp bridge (section 9); it is the
# same host as the API, and Caddy routes the webhook path to the bridge.
cat >> .env <<EOF
ARYA_RUNTIME_ONLY=1
ARYA_ADMIN_TOKEN=$(openssl rand -hex 24)
PUBLIC_BASE_URL=https://$IP.sslip.io
LIVEKIT_URL=wss://lk.$IP.sslip.io
ARYA_WEBHOOK_BASE_URL=https://$IP.sslip.io
EOF

grep sslip .env               # confirm REAL addresses landed, not a literal IP placeholder
grep ARYA_ADMIN_TOKEN .env    # COPY this value; the builder's push form needs it
```

`docker-compose.yml` already passes all four variables through from `.env`; no compose edits are needed.

## 5. TLS with Caddy (the mic only works over HTTPS)

```bash
sudo apt install -y caddy
# Uses the $IP variable from step 4 (same shell session):
sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
$IP.sslip.io {
    reverse_proxy /webhooks/whatsapp* localhost:8030
    reverse_proxy localhost:8000
}
lk.$IP.sslip.io {
    reverse_proxy localhost:7880
}
EOF
cat /etc/caddy/Caddyfile      # confirm the REAL address landed, not "IP"
sudo systemctl reload caddy
```

The `/webhooks/whatsapp` line sends Meta's calls to the WhatsApp bridge (port 8030) and everything else to the API. Harmless if you never connect a number.

The first HTTPS load takes 30 to 60 seconds while Caddy fetches the certificate. sslip.io is a shared domain: if Let's Encrypt rate-limits it, retry later or use a real (sub)domain; only these two hostnames change.

## 6. Start and verify

```bash
docker compose up -d --build
```

The first build downloads about 18 GB and takes about 10 minutes. How to know it succeeded:

```bash
docker compose ps                 # every service Up
docker compose ps -a | grep init  # the two one-shot *-init jobs: Exited (0)
docker compose logs agent-worker | grep -o 'registered worker.*' | head -1
curl -s localhost:8000/api/health # {"ok":true,...}
```

Then in a browser: `https://$IP.sslip.io` (dash form) shows **"Arya engine is running"** with a padlock. No builder UI is served; that is correct and deliberate.

## 7. Give the engine its provider keys (once)

API keys deliberately never travel inside agent exports; the engine needs its own. Publishing tells you exactly which are missing if you skip one. Run from WSL or the VM (not PowerShell, which mangles quoted JSON), and keep the Content-Type header (without it the request 422s):

All accepted names, in one call. DELETE the lines for services you don't
use (sending only some keys is fine: the endpoint merges, it never wipes
what you omit; an empty string `""` removes that stored key):

```bash
curl -i -X POST https://$IP.sslip.io/api/setup/keys \
  -H "X-Arya-Admin-Token: <your ARYA_ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "openai":  "sk-...",
    "groq":    "gsk_...",
    "gemini":  "AIza...",
    "serpapi": "...",
    "ares":    "ares_...",
    "tavily":  "tvly-...",
    "mem0":    "m0-..."
  }'
```

`openai`/`groq` power agents on that provider; `serpapi`, `ares` and
`tavily` are the web-search backends; `mem0` is memory. Expect
`{"saved":true,...}`. Verify anytime:

```bash
curl -s https://$IP.sslip.io/api/setup/status \
  -H "X-Arya-Admin-Token: <token>" | head -c 300
```

## 8. Deploy an agent from your laptop

Local builder → agent → Preview → **Publish & deploy** → *Deploy to a live engine*: pick your saved engine from the dropdown (or, the first time, choose *Enter URL and token manually* and paste `https://<ip-dashes>.sslip.io` plus the admin token), fill **Websites allowed to embed** with the customer domains (empty = any site, dev only), then **Deploy to engine**.

The share link and `<script>` snippet that appear now point at the engine and work on any website. Copy them right away: they are shown once. Tip: save the engine as a connection (Deployments → Source → Add engine) and it also appears in that dropdown, plus you get the engine's deployments, conversations and dashboard from your builder. Pushed documents re-ingest on the engine (a PDF takes a minute or two; knowledge answers may lag the very first calls). Re-deploying after local edits keeps the same key, so pasted snippets keep working.

**Try it like a customer:** open the share link from a phone on mobile data, or paste the snippet into a bare HTML page, serve it with `python3 -m http.server 5500`, and open `http://localhost:5500` (localhost, not 0.0.0.0: that address is a listen-address, not a browsable one). Leave *Allowed websites* empty while testing; lock it to the real customer domains once live.

## 9. WhatsApp on the engine

A push carries the agent and its documents. It does **not** carry the WhatsApp number or the business database: both hold credentials and belong to the machine that answers. Set them from your builder, which relays to the engine with the saved admin token:

1. Save the engine as a connection (Deployments → Source → Add engine), if you have not.
2. **Database**: Deployments → Source: this engine → Manage the agent → *Business database*. Paste a connection string the engine can reach, a hosted one such as Supabase with `?sslmode=require`. A database on your laptop is not reachable from here. If it has the shop tables, the card says the order tools will use it.
3. **Number**: on the same screen, **Channels** → the engine's card → connect with the phone number ID, access token and app secret. The engine checks them with Meta and subscribes the account itself.
4. **Meta**: copy the engine card's callback URL and verify token into the Meta app's WhatsApp Configuration, Verify and save. Once. The address never changes, so there is no tunnel to restart.

A number answers in one place. Connecting it on the engine while it is still connected on the laptop is refused with the agent it already answers as; disconnect it on the laptop first.

Check the route before touching Meta. This must answer **403** (the bridge refusing a wrong token), not 404 (the API, meaning the Caddy line is missing):

```bash
curl -s -o /dev/null -w "%{http_code}\n" "https://$IP.sslip.io/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=x&hub.challenge=1"
```

Without a builder nearby, the same four steps are plain API calls with the admin token: `POST /api/connections`, `PUT /api/agents/{id}/connectors`, `POST /api/agents/{id}/channels`, then Meta.

## What persists, and when

- **The pk_ key, the agents, their documents, conversation history:** stored in the engine's database and data volumes. They survive restarts, reboots, and stop/start. The key only changes if you rotate or revoke it.
- **An ephemeral external IP:** survives reboots but is RELEASED when you stop the VM; on the next start you get a new IP, which breaks the hostname, the certificate, and every pasted snippet. Fix by reserving a static IP (attachable after creation: VPC network → IP addresses), or run the **"The IP changed" recipe** further down this page and re-render snippets.
- **A static external IP:** yours until released, regardless of VM state.

## Changing things later

| What you want to change | How |
|---|---|
| **The VM's IP** (it changed, or you moved to a static one) | Three places: run the **"The IP changed" recipe** just below this table on the VM; **in the builder**, the saved engine connection still holds the old URL, so remove and re-add it (Deployments → Source: that engine → Engine details → Remove, then Add engine with the new URL and the same admin token), or edit `base_url` in `backend/data/engines.json`, re-read on every call; **on customer websites**, pasted snippets and share links carry the old hostname and stop working, so deploy each agent to the engine again to render the new snippet (the public key is unchanged, only the host in it). This is exactly why a static IP is recommended for anything real |
| **Provider keys** (rotate an OpenAI key, add Groq, remove one) | Re-run the step 7 curl with the new values. An empty string (`"tavily":""`) removes a key; omitted fields stay untouched. Takes effect on the next call, no restart |
| **The admin token** | Terminal only, there is deliberately no UI for it: run the **"Rotating the admin token" recipe** just below this table, then update the token wherever you push from |
| **The agent's public key (pk_)** | In the builder: Deployments → Source: this engine → Manage → **Rotate key**. Every pasted snippet dies instantly (that is the point); the NEW share link and snippet are shown once, right there; copy them before leaving the screen |
| **Allowed websites** | In the builder, either re-deploy with the **Websites allowed to embed** field set (the allowlist rides the push), or edit it live: Deployments → Source: this engine → Manage → **Whitelist domains**. Applies immediately, no restart |
| **The agent itself** (prompt, voice, documents) | Edit locally, deploy to the engine again. Same key, same snippet, customers change nothing |
| **A real domain instead of sslip.io** | Point the domain's A record at the VM's IP, replace the two hostnames in the Caddyfile and the two URLs in `.env`, reload Caddy, `docker compose up -d`, re-render snippets |

### The IP changed (full recipe)

On the VM, in `~/arya`. Nothing in the database moves: agents, keys,
allowlists and transcripts all survive, only the hostname changes.

```bash
grep sslip .env               # shows the OLD dash-style IP currently baked in
IP=NEW-DASH-IP                # <- the NEW external IP, dots as dashes

# Same two hostnames as step 5, rendered against the new IP:
sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
$IP.sslip.io {
    reverse_proxy /webhooks/whatsapp* localhost:8030
    reverse_proxy localhost:8000
}
lk.$IP.sslip.io {
    reverse_proxy localhost:7880
}
EOF
cat /etc/caddy/Caddyfile      # confirm the REAL new address landed, not "IP"
sudo systemctl reload caddy

# Swap the old address for the new one (OLD-DASH-IP = what the first grep showed):
sed -i "s/OLD-DASH-IP/$IP/g" .env
grep sslip .env               # confirm BOTH URLs now carry the new address

docker compose up -d
```

The first HTTPS load takes 30 to 60 seconds while Caddy fetches the new
certificate. Then do the builder and customer-website steps from the table
row above.

If you are also pulling new code while you are here, run `git pull &&
docker compose up -d --build` first and see "Updating the engine" above
for the catalog-refresh step that goes with it; an IP change by itself
needs none of that.

### Rotating the admin token (full recipe)

On the VM, in `~/arya`. Rotate after the token leaked (a screenshot, a
screen share) or when someone who had it should no longer administer the
engine. Published agents keep answering throughout: visitors use `pk_` keys,
never this token.

```bash
# Mint a fresh token in place:
sed -i "s/^ARYA_ADMIN_TOKEN=.*/ARYA_ADMIN_TOKEN=$(openssl rand -hex 24)/" .env
grep ARYA_ADMIN_TOKEN .env    # COPY the new value now

# Recreate the containers so they pick it up (env vars are baked in at
# container creation, so a plain restart is not enough):
docker compose up -d
```

The old token stops working the moment the containers come back. Then update
every builder that pushes to this engine: the saved connection still holds
the old token, so remove and re-add it (Deployments → Source: this engine →
Engine details → Remove, then Add engine with the same URL and the new
token). A builder still on the old token gets "engine rejected the saved
admin token" until that is done.

## Before going live (checklist)

- [ ] Static IP reserved and attached (not ephemeral)
- [ ] **Allowed websites** locked to the real customer domains (never empty in production)
- [ ] Call caps reviewed for the expected traffic (defaults: 3 concurrent, 100/day, 600 s per call; change via `PATCH /api/agents/<id>/deployment` or the builder)
- [ ] Provider keys set on the engine and verified via `/api/setup/status`
- [ ] The GitHub clone token deleted
- [ ] Admin token stored somewhere safe (it is the only way to administer the engine)

## Updating the engine (new code, e.g. UI changes)

The widget and pages are baked into the Docker image, so shipping code changes means rebuilding on the VM:

```bash
cd ~/arya && git pull && docker compose up -d --build
```

Agents themselves never need this: they travel via push, no rebuild involved.

If the build fails with `no space left on device`, see the first Troubleshooting row about it below: build one image at a time, and grow the boot disk if that is not enough.

**After the pull, check the generated configs are still there.** On a clone
old enough to have once tracked them, the first pull past the commit that
untracked `livekit.yaml` and `seaweedfs-s3.json` DELETES both, and LiveKit
and SeaweedFS then die at startup (Docker puts an empty directory where each
file should be):

```bash
ls livekit.yaml seaweedfs-s3.json || ./scripts/init.sh
```

**Also after the pull, refresh the catalogs.** Model lists, connector
types and the other editable catalogs in `backend/src/catalog.py` seed
into Postgres once, on an engine's first boot, and are never auto-updated
after that. A rebuild alone leaves an old engine showing stale connector
text or stale model lists. Run this once per rebuild; it is safe to run
even when nothing changed:

```bash
./scripts/refresh_catalogs.sh
```

It reads the VM's own `.env` for the URL and admin token, so nothing needs
to be typed by hand.

If `livekit.yaml` or `seaweedfs-s3.json` had to be regenerated, one
engine-only step: `init.sh` writes `use_external_ip: false`, and an engine
must advertise its public IP for WebRTC or calls connect silent. Re-apply
step 4's edit, then rebuild:

```bash
sed -i 's/use_external_ip: false/use_external_ip: true/' livekit.yaml
grep use_external_ip livekit.yaml     # confirm: true
docker compose up -d --build
```

This only ever happens once per clone; pulls after that never touch either
file (`.env` and `backend/data` were never affected at all).

## Troubleshooting (each of these was hit for real)

| Symptom | Cause and fix |
|---|---|
| Browser: ERR_SSL_PROTOCOL_ERROR | Using the dot-form hostname while Caddy holds the dash-form certificate (use dashes), or the Caddyfile still contains the literal "IP" placeholder (rewrite it with the real address, `sudo systemctl reload caddy`) |
| `docker ps`: permission denied | The docker group needs a fresh login: close and reopen SSH, or `newgrp docker` |
| Clone: 403 "Write access to repository not granted" | Fine-grained token not authorized for the org repo; use a classic token with `repo` scope |
| Deploy to engine: "engine import failed: Internal Server Error" | Data folder not writable by the containers: `sudo chown -R 1000:1000 backend/data` on the VM, then deploy again |
| Deploy to engine: "engine publish failed: ... API key is missing" | The engine has no provider keys yet; do step 7 |
| Keys curl "worked" but keys still missing | The request never parsed: missing Content-Type header, or run from PowerShell. Re-run with `-i` from WSL/the VM and expect `{"saved":true}` |
| Call connects but SILENT audio | The UDP firewall rule (step 2) is missing, or `use_external_ip` is still false. Tunnels (ngrok, cloudflared) can never carry the audio |
| Mic blocked in the browser | The page is not HTTPS; check Caddy got its certificate: `sudo journalctl -u caddy | tail` |
| Engine refuses to boot | `ARYA_RUNTIME_ONLY=1` without `ARYA_ADMIN_TOKEN`; set the token (this guard is deliberate) |
| Demo page "site can't be reached" at 0.0.0.0:5500 | Browse `http://localhost:5500` instead; 0.0.0.0 is the server's listen-address |
| Build fails with `no space left on device`, and `df -h` shows space free again afterwards | The four app images unpack together and briefly need more than was free. Build them one at a time: `COMPOSE_PARALLEL_LIMIT=1 docker compose build`, then `docker compose up -d`. Still failing: grow the boot disk in the console, then on the VM run `lsblk` to confirm the root device, `sudo growpart /dev/sda 1` and `sudo resize2fs /dev/sda1`. Never run `docker volume prune` or `docker system prune --volumes`: they delete the database, Qdrant and uploaded documents. |

## Teardown

A test VM keeps billing while it exists. When finished: Console → VM instances → delete the instance, and release the static IP if you reserved one (an unattached static IP has a small idle fee). Delete the GitHub token used for the clone.
