# Troubleshooting Arya

Every failure we have actually seen, with the exact error text so you can
search this page for what your terminal printed. The README carries a short
table of the common first-run ones and links here for the full fix.

Two commands worth knowing before anything else:

```bash
docker compose ps
```

```bash
docker compose logs -f api agent-worker
```

Almost every diagnosis below starts with one of those.

---

## The stack will not start

### Windows: UDP port range blocked

```
Error response from daemon: ports are not available: exposing port UDP
0.0.0.0:50088 -> 127.0.0.1:0: listen udp 0.0.0.0:50088: bind: An attempt was
made to access a socket in a way forbidden by its access permissions
```

**Windows only.** Windows and Hyper-V reserve blocks of ports for their own
use, and which blocks they pick changes on every reboot. This time one of
them overlaps `50000-50100`, the UDP range the audio server publishes, so
Docker cannot bind it. Nothing is wrong with your setup, and it explains why
the same project worked yesterday and not today.

Confirm it in PowerShell (look for a range overlapping 50000-50100):

```powershell
netsh interface ipv4 show excludedportrange protocol=udp
```

Fix it, in PowerShell **as Administrator**:

```powershell
net stop winnat
```

```powershell
net start winnat
```

Then **fully quit and reopen Docker Desktop**. This step gets skipped and
then nothing works: resetting winnat breaks Docker's networking until it
restarts. Now start the stack again:

```bash
docker compose up
```

**If it keeps coming back**, either reserve the range so Windows cannot take
it (run this right after the winnat restart, while the range is free):

```powershell
netsh int ipv4 add excludedportrange protocol=udp startport=50000 numberofports=101 store=persistent
```

...or move the range for good. Change `50000-50100` to `51000-51100` in
`docker-compose.yml` **and** the matching `port_range_start` /
`port_range_end` in `livekit.yaml` (generated for you by
`scripts/init.sh`). The two files must agree, or calls will connect and stay
silent.

### Postgres: password authentication failed

```
api-1       | asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "arya"
api-1       | ERROR:    Application startup failed. Exiting.
postgres-1  | FATAL:  password authentication failed for user "arya"
```

The password in `.env` and the one stored inside the database volume no
longer match.

Why it happens: `POSTGRES_PASSWORD` is only applied when the database volume
is **first** created. After that, the password baked into the volume wins.
So you hit this if `.env` was deleted and `./scripts/init.sh` re-ran (it
mints a fresh random password when `.env` is missing), or if you re-cloned
the project into a folder of the same name while the old volume survived.

**Fix by deleting only the database volume.** Safe before you have
conversations worth keeping; the schema is recreated on boot.

```bash
docker compose down
```

```bash
docker volume ls --filter name=pg-data
```

```bash
docker volume rm arya_pg-data
```

```bash
docker compose up --build
```

Use whatever name the second command prints: the volume is prefixed with
your folder name, so a ZIP download unzipped as `arya_-main` gives
`arya_-main_pg-data`.

**To keep the data instead**, change the stored password to match `.env`
(connections from inside the container are not password-checked):

```bash
docker compose up -d postgres
```

```bash
docker compose exec postgres psql -U arya -d arya -c "ALTER USER arya WITH PASSWORD 'PASTE_FROM_.env';"
```

> **Never use `docker compose down -v` for this.** It deletes every volume,
> including the ~11 GB of downloaded models, and you will re-download them.

### `livekit` or `seaweedfs` dies immediately with a config error, or an empty folder named `livekit.yaml` appeared

`livekit.yaml` and `seaweedfs-s3.json` are generated for your machine and
gitignored, so they can disappear: a `git clean -fdx` removes them, switching
to a branch from before they were untracked does too, and so does the first
`git pull` that carries the untracking commit into an old clone (this bites
once per clone, then never again). When the file is
missing, Docker creates a **directory** at the bind-mount path instead, and
the service starts with no config.

Delete the stray folder if there is one, then:

```bash
./scripts/init.sh
```

```bash
docker compose up
```

`init.sh` never overwrites what exists and reads the keys back out of
`.env`, so the regenerated files match what the rest of the stack already
uses. Nothing else is touched.

### Build fails with `failed to extract layer ... input/output error` or `no space left on device`

Out of disk. The download could not be written, and it usually dies on the
8 GB `ollama` image. Free space (see "Will this run on my machine?" in the
README), clear old images, and on Mac or Windows check Docker Desktop →
**Settings → Resources** for the virtual-disk limit.

**First, if `df -h` shows free space again after the failure,** the disk
was not really full. The four app images (`api`, `agent-worker`,
`arya-mcp`, `arya-whatsapp`) unpack at the same time and briefly need more
than was free; the failed build then cleans up after itself. Build them one
at a time:

```bash
COMPOSE_PARALLEL_LIMIT=1 docker compose build
```

Then start the stack:

```bash
docker compose up -d
```

**On a cloud VM that still fails,** the boot disk is too small (the stack
settles around 35 GB and an update needs room on top). Grow the disk in the
cloud console to 100 GB, then on the VM confirm the root device and extend
it. On Google Cloud the root is usually `/dev/sda1`:

```bash
lsblk
```

```bash
sudo growpart /dev/sda 1
```

```bash
sudo resize2fs /dev/sda1
```

Never run `docker volume prune` or `docker system prune --volumes`: they delete the database, Qdrant and uploaded documents.

**Otherwise,** free space by removing unused images (running containers
and their data are not touched; old images are downloaded again if needed):

```bash
docker system prune -a
```

Then retry, or skip the local-model services entirely:

```bash
docker compose up --build api agent-worker livekit postgres qdrant seaweedfs
```

### Mac says "your system has run out of application memory"

Same cause as the one above: macOS keeps its swap file on the startup disk,
so a full disk surfaces as an out-of-memory dialog. Free disk space first,
and lower Docker Desktop → Settings → Resources → Memory if it is set very
high.

### `Cannot connect to the Docker daemon` / `docker.sock: no such file or directory`

Docker Desktop is not running, or crashed. Start it, wait for the whale icon
to stop animating, then re-run the command.

### `port is already allocated`

Something else already uses one of the ports (an old copy of this project is
the usual answer). Run this in that project's folder first:

```bash
docker compose down
```

---

## It starts, but something is wrong

### "Can't reach the backend" warning in the UI

The stack is not up yet, or is still building. The UI polls every five
seconds and recovers on its own, so usually you just wait. If it persists:

```bash
docker compose ps
```

### The terminal shows `http://0.0.0.0:8000` and it will not open

`0.0.0.0` is the server saying "listening on every address", not an address
you can visit. Open **http://localhost:8000** instead. The API also prints
the correct URL in a banner once it is ready.

### The call connects but the agent never speaks

Almost always the **host networking** setting (step 3 of "Before you start"
in the README). Turn it on in Docker Desktop → Settings → Resources →
Network, then:

```bash
docker compose down
```

```bash
docker compose up
```

If it is already on, read the worker log while you place a call:

```bash
docker compose logs -f agent-worker
```

### The browser never asks for the microphone

Use Chrome or Edge on `http://localhost:8000`. Browsers allow microphone
access on `localhost` without HTTPS, but any other hostname needs it.

### A Groq agent hears you but never speaks

One-time licence acceptance on Groq's side: open
[the Orpheus playground](https://console.groq.com/playground?model=canopylabs/orpheus-v1-english)
and accept the voice terms, then call again. The worker log shows
`model_terms_required` when this is the cause.

### "Before this agent can run: ... key is missing"

Paste the key into the selected provider card on the abilities screen (it is
right under the card's title). Keys for optional abilities (web search,
memory, evaluation) live inside **Advanced settings** on that screen, which
opens by itself when one of them is missing. The review screen lists exactly
what is missing for the provider and abilities you picked.

### The first local-provider call is slow or silent

Whisper, Llama and Kokoro download once, on first use, and it is a few GB.
Give it a few minutes and try again:

```bash
docker compose logs -f speaches ollama
```

### `vLLM backend needs an LLM endpoint URL`

The vLLM backend has no bundled server to fall back to, unlike Ollama. Paste
its endpoint and model id under **Advanced settings → Models and voice** on
the abilities screen (the panel opens by itself while the URL is blank),
then press **Check** to confirm the server is reachable and serving that
model.

### A turn takes several seconds

Depends on the provider.

- **Groq or Open source (local):** a known trade-off of batch
  speech-to-text. Those endpoints only transcribe a whole utterance after
  you stop speaking, so every turn pays that round trip. Switching the
  agent to OpenAI removes it.
- **OpenAI:** speech-to-text streams by default, transcribing while you
  speak over OpenAI's realtime transcription websocket, so a slow turn
  usually means the streaming path is switched off. Check that
  `OPENAI_STT_REALTIME` is not set to `0` in the worker's environment and
  that the agent's config has no `"realtime": false` under `stt`. Both
  exist as kill switches for cost comparisons (realtime bills the whole
  audio stream, silence included; batch uploads only detected speech).
  If neither is set and turns are still slow, the delay is on the LLM or
  TTS stage: `docker compose logs -f agent-worker` prints per-stage
  metrics for every turn.

---

## Documents and RAG

### A document sits in `pending` for a long time

The first PDF is slow because docling downloads its models once. After that,
a text PDF is quick; a **scanned** one is OCR'd page by page on CPU and can
take minutes.

```bash
docker compose logs -f api
```

### A document reached `ready` but the agent does not use it

Retrieval is scoped per agent, and a chip means "used in the answer", not
"retrieved". Check: documents belong to the agent that uploaded them,
`Documents (RAG)` is on for the agent you are calling, and your question
uses words that appear in the document.

### RAG stops working after pointing the LLM somewhere else

Embeddings always call the bundled `ollama` container, independent of which
LLM backend the agent uses. A vLLM-backend agent with RAG on still needs the
`ollama` service running.

---

## Windows notes

### Where to clone

Clone inside WSL (for example `~/projects/arya`), not on `C:` or `D:`.
A Windows-side clone does work, but `backend/data` then lives on a mount
that ignores POSIX permissions, so the owner-only protection on
`keys.json` and `engines.json` (which holds engine admin tokens) is not
enforced. Windows-side clones are also slower and can give
`scripts/init.sh` CRLF line endings, which fails with a `bad interpreter`
error.

### Git or Docker suddenly cannot reach the network

WSL networking sometimes breaks, especially after a winnat reset or when a
VPN connects. Restart it from PowerShell:

```powershell
wsl --shutdown
```

Reopen your WSL terminal and Docker Desktop. On Windows 11 the durable fix
is mirrored networking, in `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

### The local models are slow or time out

WSL only gets half your RAM by default. With 16 GB or more, put this in
`C:\Users\<you>\.wslconfig`, then run `wsl --shutdown` and restart Docker
Desktop:

```ini
[wsl2]
memory=11GB
```
