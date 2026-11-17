# Deploying an Arya engine on Amazon Web Services (AWS)

The engine is the deploy target for the builder's **Publish & deploy → Deploy to engine** flow. It serves the share link, the embeddable widget, and the public call API. You build agents locally; the engine runs them publicly.

This is the AWS twin of [DEPLOYMENT.md](DEPLOYMENT.md) (Google Cloud). The engine-side procedure (steps 3 to 8) is identical and was performed for real on GCP; the AWS-specific parts (instance creation, security groups, Elastic IP) follow AWS's current console flow and pricing. Where AWS behaves differently from GCP, the difference is called out.

## Requirements

| Thing | Why |
|---|---|
| AWS account with billing | the VM (EC2 instance) |
| t3a.large (2 vCPU, 8 GB), Ubuntu Server 24.04 LTS, 64-bit x86, 50 GB gp3 disk | the full stack includes local models (Ollama, Speaches) which need about 8 GB RAM and about 25 to 30 GB of disk after the first build. CPU is enough: the whole stack is CPU-built, a GPU only ever improves local-model latency. t3a.large is the cheapest 2 vCPU / 8 GB option (about $55/month on-demand); t3.large (Intel) costs slightly more for the same shape |
| An Elastic IP (recommended; the auto-assigned IP is fine for a one-off test) | the hostname, and every pasted snippet, must not change. Auto-assigned public IPs survive reboots but change on stop/start. Note: AWS charges about $3.65/month for ANY public IPv4, Elastic or not, so the Elastic IP costs nothing extra while attached |
| Open ports: 22, 80, 443, 7881 (TCP) and 50000-50100 (UDP) | SSH, HTTP/TLS, LiveKit TCP fallback, and the WebRTC audio itself. On AWS these live in a security group, not a firewall CLI |
| No domain purchase needed | `<ip>.sslip.io` resolves any dash-style name to that IP (example: `34-133-176-4.sslip.io` resolves to `34.133.176.4`), which is enough for free Let's Encrypt TLS via Caddy. Swap in a real domain later by changing two hostnames |

Pick the region closest to your expected callers (voice is latency-sensitive), and stay in that region for every console step below: EC2 resources are per-region and "my instance disappeared" is usually the region dropdown.

## 1. Launch the instance

Console: EC2 → Instances → **Launch instance**. Everything not listed below stays at its default.

| Section | Setting |
|---|---|
| Name and tags | anything, e.g. `arya-engine` |
| Application and OS Images | Quick Start → Ubuntu → **Ubuntu Server 24.04 LTS (HVM)**, Architecture **64-bit (x86)**, NOT Arm (the Docker images are amd64) |
| Instance type | **t3a.large** (2 vCPU, 8 GB) |
| Key pair (login) | **Create new key pair** (RSA, .pem) and keep the file; it is your SSH login. The browser "Connect" button also works on Ubuntu, but see the note in step 3 |
| Network settings | keep **Auto-assign public IP: Enable**. Under the security group checkboxes, tick **Allow SSH traffic from** (Anywhere is simplest during setup; tighten later), **Allow HTTPS traffic from the internet**, and **Allow HTTP traffic from the internet** |
| Configure storage | **50 GiB, gp3** (the 8 GiB default cannot hold the first build) |
| Advanced details | all defaults. Optional for a long-lived engine: Termination protection: Enable |

Launch, then wait for **Instance state: Running** and **Status checks: passed**.

A test instance bills per second while running (about $55/month for the instance plus about $3.65/month for its public IPv4 plus about $4/month for the disk). Terminate it when done (see Teardown).

## 2. Open the audio ports (security group)

The launch checkboxes only covered 22/80/443. The WebRTC media ports must be added or calls will connect with SILENT audio (the number one trap, same as on GCP).

No CLI needed on AWS; it is a console edit. Instance page → **Security** tab → click the security group → **Edit inbound rules** → **Add rule** twice:

| Type | Port range | Source |
|---|---|---|
| Custom TCP | 7881 | Anywhere-IPv4 (0.0.0.0/0) |
| Custom UDP | 50000-50100 | Anywhere-IPv4 (0.0.0.0/0) |

Save rules. Security group changes apply immediately, no restart.

## 3. Install Docker and get the code (on the instance, via SSH)

Two ways in:

- **Browser:** instance page → **Connect** → EC2 Instance Connect tab → Connect. Caveat: this path only works while the SSH rule allows AWS's service range; if you tightened port 22 to "My IP", the browser button stops working and only the .pem login below does.
- **Your terminal:** `ssh -i your-key.pem ubuntu@<public-ip>` (the default user on Ubuntu AMIs is `ubuntu`).

Then:

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER
```

The group change needs a fresh login: close the SSH session and open a new one (or run `newgrp docker` in the current shell). Verify with `docker ps`; an empty table is correct, "permission denied" means the re-login did not happen yet.

**Cloning.** While the repo is private, plain clone fails ("password authentication is not supported"). Create a GitHub token first: Settings → Developer settings → Fine-grained token, resource owner set to the org, repository `arya`, permission **Contents: Read-only**, short expiry. If the org blocks fine-grained tokens (the symptom is a misleading 403 "Write access to repository not granted"), fall back to a classic token with the `repo` scope. Use the token as the password at the clone prompt; never embed it in the URL. Delete the token from GitHub once done.

```bash
git clone https://github.com/traversaal-ai/arya_.git arya && cd arya
# The clone lands on main, which is the release branch: stay on it. An
# engine on a work branch would pull half-finished code on every update.
./scripts/init.sh         # generates .env, livekit.yaml, seaweedfs-s3.json
# The containers run as uid 1000. On Ubuntu EC2 AMIs the login user is
# usually uid 1000 too, so this often changes nothing, but it is cheap
# insurance: without matching ownership, pushing an agent later fails
# with a PermissionError.
sudo chown -R 1000:1000 backend/data
```

## 4. Configure for public serving

**Where to find your IP:** Console → EC2 → Instances → select the instance → **Public IPv4 address** (example: `34.133.176.4`).

Set it ONCE as a shell variable, dots replaced by dashes. Every later command expands `$IP` itself, so nothing needs hand-editing. If you open a new terminal later, set it again first. This dash-style hostname is the one to use EVERYWHERE, including the browser: the dot form resolves too, but Caddy only holds a certificate for the dash form, and mixing them shows up as ERR_SSL_PROTOCOL_ERROR.

```bash
IP=34-133-176-4           # <- YOUR public IPv4, dash-style
```

Then, in `~/arya`:

```bash
# LiveKit must advertise the instance's PUBLIC IP for WebRTC:
sed -i 's/use_external_ip: false/use_external_ip: true/' livekit.yaml
grep use_external_ip livekit.yaml     # confirm: use_external_ip: true

# Engine settings, appended to the .env that init.sh generated:
cat >> .env <<EOF
ARYA_RUNTIME_ONLY=1
ARYA_ADMIN_TOKEN=$(openssl rand -hex 24)
PUBLIC_BASE_URL=https://$IP.sslip.io
LIVEKIT_URL=wss://lk.$IP.sslip.io
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
    reverse_proxy localhost:8000
}
lk.$IP.sslip.io {
    reverse_proxy localhost:7880
}
EOF
cat /etc/caddy/Caddyfile      # confirm the REAL address landed, not "IP"
sudo systemctl reload caddy
```

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

API keys deliberately never travel inside agent exports; the engine needs its own. Publishing tells you exactly which are missing if you skip one. Run from WSL or the instance (not PowerShell, which mangles quoted JSON), and keep the Content-Type header (without it the request 422s):

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

## What persists, and when

- **The pk_ key, the agents, their documents, conversation history:** stored in the engine's database and data volumes. They survive restarts, reboots, and stop/start. The key only changes if you rotate or revoke it.
- **An auto-assigned public IPv4:** survives reboots but is RELEASED when you stop the instance; on the next start you get a new IP, which breaks the hostname, the certificate, and every pasted snippet. Fix by attaching an Elastic IP (EC2 → Network & Security → Elastic IPs → Allocate → Associate with the instance), or run the ["The IP changed" recipe](#the-ip-changed-full-recipe) below.
- **An Elastic IP:** yours until released, regardless of instance state, and costs the same as any public IPv4 (about $3.65/month) whether attached or idle.

## Changing things later

| What you want to change | How |
|---|---|
| **The instance's IP** (it changed after a stop/start, or you moved to an Elastic IP) | Three places: run the ["The IP changed" recipe](#the-ip-changed-full-recipe) below on the instance; **in the builder**, the saved engine connection still holds the old URL, so remove and re-add it (Deployments → Source: that engine → Engine details → Remove, then Add engine with the new URL and the same admin token), or edit `base_url` in `backend/data/engines.json`, re-read on every call; **on customer websites**, pasted snippets and share links carry the old hostname and stop working, so deploy each agent to the engine again to render the new snippet (the public key is unchanged, only the host in it). This is exactly why an Elastic IP is recommended for anything real |
| **Provider keys** (rotate an OpenAI key, add Groq, remove one) | Re-run the step 7 curl with the new values. An empty string (`"tavily":""`) removes a key; omitted fields stay untouched. Takes effect on the next call, no restart |
| **The admin token** | Terminal only, there is deliberately no UI for it: run the ["Rotating the admin token" recipe](#rotating-the-admin-token-full-recipe) below, then update the token wherever you push from |
| **The agent's public key (pk_)** | In the builder: Deployments → Source: this engine → Manage → **Rotate key**. Every pasted snippet dies instantly (that is the point); the NEW share link and snippet are shown once, right there; copy them before leaving the screen |
| **Allowed websites** | In the builder, either re-deploy with the **Websites allowed to embed** field set (the allowlist rides the push), or edit it live: Deployments → Source: this engine → Manage → **Whitelist domains**. Applies immediately, no restart |
| **The agent itself** (prompt, voice, documents) | Edit locally, deploy to the engine again. Same key, same snippet, customers change nothing |
| **Open or close a port** | Instance → Security tab → the security group → Edit inbound rules. Applies immediately |
| **A bigger instance** | Stop the instance → Actions → Instance settings → Change instance type → Start. WARNING: without an Elastic IP the stop/start changes the public IP (see the first row) |
| **A real domain instead of sslip.io** | Point the domain's A record at the instance's IP, replace the two hostnames in the Caddyfile and the two URLs in `.env`, reload Caddy, `docker compose up -d`, re-render snippets |

### The IP changed (full recipe)

On the instance, in `~/arya`. Nothing in the database moves: agents,
keys, allowlists and transcripts all survive, only the hostname changes.

```bash
grep sslip .env               # shows the OLD dash-style IP currently baked in
IP=NEW-DASH-IP                # <- the NEW public IP, dots as dashes

# Same two hostnames as step 5, rendered against the new IP:
sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
$IP.sslip.io {
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

On the instance, in `~/arya`. Rotate after the token leaked (a
screenshot, a screen share) or when someone who had it should no longer
administer the engine. Published agents keep answering throughout: visitors
use `pk_` keys, never this token.

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

- [ ] Elastic IP allocated and associated (not the auto-assigned one)
- [ ] SSH (port 22) tightened from Anywhere to **My IP** in the security group (remember this disables the browser Connect button; keep the .pem)
- [ ] **Allowed websites** locked to the real customer domains (never empty in production)
- [ ] Call caps reviewed for the expected traffic (defaults: 3 concurrent, 100/day, 600 s per call; change via `PATCH /api/agents/<id>/deployment` or the builder)
- [ ] Provider keys set on the engine and verified via `/api/setup/status`
- [ ] The GitHub clone token deleted
- [ ] Admin token stored somewhere safe (it is the only way to administer the engine)

## Updating the engine (new code, e.g. UI changes)

The widget and pages are baked into the Docker image, so shipping code changes means rebuilding on the instance:

```bash
cd ~/arya && git pull && docker compose up -d --build
```

Agents themselves never need this: they travel via push, no rebuild involved.

If the build fails with `no space left on device`, see the Troubleshooting row about it below: build one image at a time, and grow the volume if that is not enough.

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

It reads the instance's own `.env` for the URL and admin token, so
nothing needs to be typed by hand.

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

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Browser: ERR_SSL_PROTOCOL_ERROR | Using the dot-form hostname while Caddy holds the dash-form certificate (use dashes), or the Caddyfile still contains the literal "IP" placeholder (rewrite it with the real address, `sudo systemctl reload caddy`) |
| `docker ps`: permission denied | The docker group needs a fresh login: close and reopen SSH, or `newgrp docker` |
| Clone: 403 "Write access to repository not granted" | Fine-grained token not authorized for the org repo; use a classic token with `repo` scope |
| Deploy to engine: "engine import failed: Internal Server Error" | Data folder not writable by the containers: `sudo chown -R 1000:1000 backend/data` on the instance, then deploy again |
| Deploy to engine: "engine publish failed: ... API key is missing" | The engine has no provider keys yet; do step 7 |
| Keys curl "worked" but keys still missing | The request never parsed: missing Content-Type header, or run from PowerShell. Re-run with `-i` from WSL/the instance and expect `{"saved":true}` |
| Call connects but SILENT audio | The security group is missing the UDP 50000-50100 rule (step 2), or `use_external_ip` is still false. Tunnels (ngrok, cloudflared) can never carry the audio |
| Everything 502s from Caddy | The api container is down: `docker compose ps`, then `docker compose up -d`; read `docker compose logs api --tail 50` if it keeps exiting |
| Mic blocked in the browser | The page is not HTTPS; check Caddy got its certificate: `sudo journalctl -u caddy | tail` |
| Engine refuses to boot | `ARYA_RUNTIME_ONLY=1` without `ARYA_ADMIN_TOKEN`; set the token (this guard is deliberate) |
| Whole engine went dark after a stop/start | The auto-assigned public IP changed; follow the "IP changed" row in the table above, or attach an Elastic IP so it never happens again |
| Browser Connect button cannot reach the instance | Port 22 is restricted to My IP, which blocks AWS's Instance Connect service. Use `ssh -i key.pem ubuntu@<ip>` from your terminal, or temporarily widen the SSH rule |
| Local-model calls get sluggish after heavy sustained use | t3a is a burstable type; check the CPU credit balance in the instance's Monitoring tab. Occasional calls are fine on credits; for sustained production load pick a non-burstable type (m6a.large or bigger) |
| "This instance does not exist" / empty instance list | Wrong region selected in the console's top-right dropdown |
| Build fails with `no space left on device`, and `df -h` shows space free again afterwards | The four app images unpack together and briefly need more than was free. Build them one at a time: `COMPOSE_PARALLEL_LIMIT=1 docker compose build`, then `docker compose up -d`. Still failing: grow the volume (EC2, Volumes, Modify volume, 100 GiB), then on the instance run `lsblk` to confirm the root device, and on a t3a (NVMe) `sudo growpart /dev/nvme0n1 1` and `sudo resize2fs /dev/nvme0n1p1`. Never run `docker volume prune` or `docker system prune --volumes`: they delete the database, Qdrant and uploaded documents. |

## Teardown

A test instance keeps billing while it exists. When finished: Console → EC2 → Instances → Instance state → **Terminate** (this also deletes the root disk by default). Then Network & Security → Elastic IPs → **Release** the Elastic IP if you allocated one (a kept IP keeps billing about $3.65/month even unattached). Delete the GitHub token used for the clone.
