# super-fetch

An MCP server that lets coding agents (Claude Code, Codex, OpenCode, …) fetch web pages the way a
normal browser user sees them, and returns the main content as Markdown.

## How it works

1. **HTTP tier** (~0.5 s): fetch with a real Chrome TLS/HTTP2 fingerprint (`curl_cffi`). This gets
   through many sites that block ordinary HTTP clients.
2. **Browser tier** (~3–8 s): if that returns a bot challenge (Cloudflare, DataDome, PerimeterX, …), a
   JavaScript-only app shell, or the connection is refused, the page is loaded in
   [Camoufox](https://camoufox.com), a Firefox build with a consistent real-user fingerprint. It waits
   for the challenge to clear (clicking a Cloudflare Turnstile checkbox if one appears) and the
   page's content to stop changing, then extracts it.

The browser starts on first use, is shared by later fetches, and shuts down after 5 idle minutes.

**Passed challenges are reused.** The browser keeps one fingerprint and one cookie jar, saved to
disk. Once it has passed a site's challenge, later fetches of that site go over plain HTTP again,
sending the browser's User-Agent and clearance cookie with a matching Firefox TLS fingerprint:
~1 s instead of ~10 s, across restarts too. Sites that only work in the browser (JavaScript apps,
or protections that reject the reused cookie) go straight to the browser for the next 30 minutes.

Pages that refuse access outright (Cloudflare "you have been blocked", a DataDome ban) are
reported immediately with a "retrying won't help" warning instead of being waited on.

PDFs are returned as text with `[Page n]` markers (up to 500 pages), whichever tier fetched them.

At most two fetches run against the same host at once (bursts are what get clients flagged); more
wait their turn, and parallel calls for the same page share one download.

## Tools

`fetch(url, format="markdown", start_index=0, max_length=20000, render="auto", wait_for=None)`

- `format`: `markdown` (main content), `text`, or `html` (raw source).
- `render`: `auto` (browser only when needed), `always`, or `never` (HTTP only).
- `wait_for`: CSS selector to wait for before reading (uses the browser).
- Long content is returned in chunks; the output ends with the `start_index` to request next.
- The header lists the final URL, HTTP status, title, which tier fetched it, and warnings (challenge
  not passed, JS app shell, HTTP error, truncated body).

## Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/ginkcodex/super-fetch-mcp   # or: uv tool install /path/to/checkout

# Download the browser (~100 MB, once per machine) and check everything works
super-fetch install
super-fetch doctor
```

On Linux, also install Xvfb (`sudo apt install xvfb`) so the browser runs on a virtual display
instead of headless mode, which some sites detect.

## Connect an agent

Each agent launches `super-fetch` itself and talks to it over stdio. To run straight from a
checkout instead (picks up code changes, no reinstall), use
`uv run --directory /path/to/super-fetch super-fetch` wherever `super-fetch` appears below.

From a checkout, `make register-all` registers it with all three agents at once
(`SERVER=installed` or `SERVER=docker` for the other ways of running it; `make` lists every task).

**Claude Code**

```bash
claude mcp add --scope user super-fetch -- super-fetch
```

**Codex**

```bash
codex mcp add super-fetch -- super-fetch
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.super-fetch]
command = "super-fetch"

[mcp_servers.super-fetch.env]
SUPER_FETCH_TIMEOUT = "30"
```

**OpenCode**

```bash
opencode mcp add --global super-fetch -- super-fetch
```

or in `~/.config/opencode/opencode.json` (or a project's `opencode.json`):

```json
{
  "mcp": {
    "servers": {
      "super-fetch": {
        "type": "local",
        "command": ["super-fetch"],
        "environment": { "SUPER_FETCH_TIMEOUT": "30" }
      }
    }
  }
}
```

Older OpenCode versions expect servers directly under `"mcp"` (no `"servers"` level); current
ones still accept that form.

Environment variables (see [Configuration](#configuration)) go in `-e KEY=value` for Claude Code,
`--env KEY=value` for Codex and OpenCode.

Agents also have their own built-in web fetch. To have them use this one, add a line to your
`CLAUDE.md` / `AGENTS.md`, e.g. "Fetch web pages with the super-fetch `fetch` tool."

## Docker

The image includes the browser, Xvfb and fonts, so the machine needs nothing but Docker:

```bash
git clone https://github.com/ginkcodex/super-fetch-mcp.git && cd super-fetch-mcp
make docker-build      # or: docker build -t super-fetch .
make docker-doctor     # check both fetch tiers inside the image
```

### Register it with agents

Each agent starts its own container (`docker run -i`, MCP over stdio) when a session opens, and
the container is removed when the session ends. There is no long-running container to manage. (To
run one long-running server for several machines instead, see [Run it on a server](#run-it-on-a-server).)

```bash
make register-all SERVER=docker      # Claude Code, Codex and OpenCode
make register-claude SERVER=docker   # or just one: register-codex, register-opencode
```

This replaces any existing `super-fetch` entry (e.g. one running from a checkout); run
`make register-all` to switch back. By hand, the command is the same for every agent:

```bash
claude mcp add --scope user super-fetch -- docker run -i --rm --shm-size 1g -v super-fetch-data:/data super-fetch
codex mcp add super-fetch -- docker run -i --rm --shm-size 1g -v super-fetch-data:/data super-fetch
opencode mcp add --global super-fetch -- docker run -i --rm --shm-size 1g -v super-fetch-data:/data super-fetch
```

Or in a config file, e.g. Codex's `~/.codex/config.toml`:

```toml
[mcp_servers.super-fetch]
command = "docker"
args = ["run", "-i", "--rm", "--shm-size", "1g", "-v", "super-fetch-data:/data", "super-fetch"]
```

Check it with `claude mcp get super-fetch` (or `/mcp` in Claude Code): the status should be
"Connected".

### Options

- `-i` keeps stdin open for the MCP connection. Don't add `-t`: a terminal corrupts the protocol
  stream.
- `--rm` deletes the container when the agent session ends.
- `--shm-size 1g`: the browser crashes on heavy pages with Docker's default 64 MB.
- `-v super-fetch-data:/data` keeps the browser identity and passed challenges between sessions.
  Several agents can use the same volume at once.
- Settings go in as `-e` flags before the image name, e.g.
  `… -v super-fetch-data:/data -e SUPER_FETCH_PROXY=socks5://host:1080 super-fetch`.
- The container uses the host's DNS servers. If fetches are slow, or fail with "resolves to
  non-public address" (a filtering resolver), add `--dns 1.1.1.1` before the image name.
- The browser always presents as Windows (`SUPER_FETCH_BROWSER_OS=windows`), so only Windows'
  fonts are included. That keeps the image near 1 GB compressed instead of 1.7 GB.

Compared with running from a checkout, Docker needs nothing else on the host and always runs the
browser on Xvfb. In exchange, each session takes a second or two to start the container, and code
changes only apply after `make docker-build`.

## Run it on a server

`super-fetch serve --http` serves MCP over HTTP (the streamable HTTP transport, at `/mcp`) instead
of stdio, so one long-running server can be shared by agents on every machine you use. A process
serves one or the other; stdio stays the default.

### With Docker Compose, behind Cloudflare

`docker-compose.yml` runs the super-fetch container on plain HTTP, port 8000. Cloudflare provides
HTTPS on the public domain, so agents connect to `https://fetch.ginklab.cv/mcp`.

1. On the server:

   ```bash
   git clone https://github.com/ginkcodex/super-fetch-mcp.git && cd super-fetch-mcp
   echo "SUPER_FETCH_HTTP_ALLOWED_HOSTS=fetch.ginklab.cv" > .env   # the public domain
   echo "SUPER_FETCH_HTTP_TOKEN=$(openssl rand -hex 32)" >> .env
   docker compose up -d --build
   docker compose logs -f
   ```

   Check it locally: `curl -i -X POST http://127.0.0.1:8000/mcp` should answer `401`.

2. Connect Cloudflare to port 8000, one of two ways:

   - **Cloudflare Tunnel** (no inbound port open): run `cloudflared` on the server and add a public
     hostname `fetch.ginklab.cv` with service `http://localhost:8000`. Add
     `SUPER_FETCH_HTTP_BIND=127.0.0.1` to `.env` and `docker compose up -d`, so port 8000 is only
     reachable from the server itself.
   - **Proxied DNS record**: an `A` record `fetch` → the server's IP, proxied (orange cloud).
     Cloudflare only connects to the origin on ports 80/443 by itself, so add an **Origin Rule**
     for the hostname that sets the destination port to 8000, and a **Configuration Rule** setting
     SSL to *Flexible* (the origin speaks HTTP). Then allow port 8000 only from
     [Cloudflare's IP ranges](https://www.cloudflare.com/ips/) in your provider's firewall.
     `ufw` doesn't work for this: Docker-published ports bypass it.

   Either way Cloudflare passes `Host: fetch.ginklab.cv` through, which is what
   `SUPER_FETCH_HTTP_ALLOWED_HOSTS` accepts.

3. On each machine with agents, with the token from `.env`:

   ```bash
   export SUPER_FETCH_HTTP_TOKEN=...
   make register-all URL=https://fetch.ginklab.cv/mcp
   ```

   or by hand:

   ```bash
   claude mcp add --scope user --transport http super-fetch https://fetch.ginklab.cv/mcp \
     --header "Authorization: Bearer $SUPER_FETCH_HTTP_TOKEN"
   codex mcp add super-fetch --url https://fetch.ginklab.cv/mcp --bearer-token-env-var SUPER_FETCH_HTTP_TOKEN
   opencode mcp add --global super-fetch --url https://fetch.ginklab.cv/mcp \
     --header "Authorization=Bearer $SUPER_FETCH_HTTP_TOKEN"
   ```

   Claude Code and OpenCode save the token in their config. Codex reads `SUPER_FETCH_HTTP_TOKEN`
   each time it starts, so export it in your shell profile.

Put other settings in `.env` and pass them through under `environment:` in `docker-compose.yml`
(`SUPER_FETCH_PROXY` already is: server IPs are what bot protection distrusts most, see
[Limits](#limits)). After pulling changes, `docker compose up -d --build` rebuilds and restarts.

All agents share one browser, cookie jar and identity, so a challenge passed for one agent is
passed for all. Up to `SUPER_FETCH_BROWSER_CONCURRENCY` (3) pages render at once; raise it on a
bigger server.

### Security

- Every request needs `Authorization: Bearer <token>`, or gets a 401. The token lets anyone use the
  server, i.e. fetch pages from its IP with its browser identity and cookies: treat it as a
  password. To change it, edit `.env`, run `docker compose up -d`, and register again.
- Agents reach Cloudflare over HTTPS. With a proxied DNS record, the hop from Cloudflare to the
  server is plain HTTP, token included; a Tunnel encrypts that hop too.
- Don't leave port 8000 open to everyone: anyone could then skip Cloudflare and try tokens directly
  against the server. Bind it to 127.0.0.1 (Tunnel) or allow only Cloudflare's IPs (proxied record).
- `serve --http` refuses to listen on anything but a loopback address without a token, and refuses
  tokens shorter than 32 characters.
- Requests must be addressed to localhost or a name in `SUPER_FETCH_HTTP_ALLOWED_HOSTS`, so a web
  page can't reach the server by pointing a domain of its own at it (DNS rebinding). A
  `421 Invalid Host header` means the name you connect with isn't listed.
- Private addresses stay blocked (`SUPER_FETCH_ALLOW_PRIVATE=0`), so the server can't be used to
  reach its own network or other containers.

### Without Docker

```bash
SUPER_FETCH_HTTP_HOST=0.0.0.0 SUPER_FETCH_HTTP_TOKEN=... SUPER_FETCH_HTTP_ALLOWED_HOSTS=fetch.ginklab.cv \
  super-fetch serve --http
```

Behind a reverse proxy (e.g. for HTTPS), leave `SUPER_FETCH_HTTP_HOST` at 127.0.0.1 and make the
proxy pass the `Host` header through.

On your own machine, `make serve-http` (or `super-fetch serve --http`) listens on
`http://127.0.0.1:8000/mcp` without a token, e.g. to share one server between local agents:
`claude mcp add --scope user --transport http super-fetch http://127.0.0.1:8000/mcp`.

### Troubleshooting

- **401**: the agent sent no token or the wrong one. Codex only sees `SUPER_FETCH_HTTP_TOKEN` if it
  was exported in the shell Codex was started from.
- **421 Invalid Host header**: the domain in the URL isn't in `SUPER_FETCH_HTTP_ALLOWED_HOSTS`, or
  the proxy rewrites `Host` (in a Tunnel, leave "HTTP Host Header" unset).
- **403 with a Cloudflare page, or a challenge**: Cloudflare's bot protection (Bot Fight Mode, WAF
  rules, "I'm Under Attack") is stopping the agent, which isn't a browser. Turn it off for this
  hostname, or skip it for `/mcp` if your plan allows.
- **403 Invalid Origin header** (plain text, from super-fetch): the request came with a browser
  `Origin` from another site. Agents don't send one.
- **521 / 522 / 523 from Cloudflare**: it can't reach port 8000: the Origin Rule is missing, the
  firewall doesn't allow Cloudflare's IPs, the Tunnel is down, or the container isn't running
  (`docker compose ps`).
- **Server won't start**: `docker compose logs super-fetch` says which setting is missing or too
  short.

### Status

Tested: the token and host checks (`tests/test_http_serve.py`), an MCP client fetching through
the server, and `docker compose up` with the checks above (401 without the token, 421 for an
unlisted host name), plus an HTTPS reverse proxy in front passing `Host` through. Not tested yet:
the Cloudflare setup itself, and Claude Code, Codex and OpenCode connecting over HTTP (only the MCP
SDK's client and curl have). Their registration commands were checked against what each CLI
writes to its config.

## Command line

```bash
super-fetch                    # run the MCP server over stdio (what agents launch)
super-fetch serve --http       # run it over HTTP instead (--host, --port; see "Run it on a server")
super-fetch fetch <url>        # print what the tool would return, for testing
                               #   (--render, --wait-for, --format, --start-index, --max-length)
super-fetch install            # download the browser
super-fetch doctor             # check both tiers; show the network fingerprint servers see
super-fetch reset              # forget the saved browser identity and cookies, start fresh
```

## Configuration

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `SUPER_FETCH_TIMEOUT` | `30` | Seconds per fetch in total: redirects, the HTTP attempt (at most half when the browser may take over) and the browser |
| `SUPER_FETCH_MAX_LENGTH` | `20000` | Default characters returned per call |
| `SUPER_FETCH_MAX_BYTES` | `10485760` | Response bodies are cut off beyond this |
| `SUPER_FETCH_MAX_REDIRECTS` | `10` | |
| `SUPER_FETCH_PROXY` | none | e.g. `http://user:pass@host:port` or `socks5://…` |
| `SUPER_FETCH_ALLOW_PRIVATE` | `0` | Allow localhost / private network addresses |
| `SUPER_FETCH_IMPERSONATE` | `chrome` | `curl_cffi` fingerprint target |
| `SUPER_FETCH_CACHE_TTL` | `300` | Seconds a page is cached so paging sees the same content |
| `SUPER_FETCH_BROWSER_MODE` | `auto` | `auto` (Xvfb if installed, else headless), `headless`, `virtual`, `headful` |
| `SUPER_FETCH_BROWSER_CONCURRENCY` | `3` | Pages rendered in the browser at once |
| `SUPER_FETCH_BROWSER_IDLE_TIMEOUT` | `300` | Seconds before an unused browser is shut down |
| `SUPER_FETCH_BROWSER_OS` | `any` | OS the browser fingerprint claims: `any`, `windows`, `macos`, `linux`; changing it starts a new identity |
| `SUPER_FETCH_DATA_DIR` | `~/.local/share/super-fetch` (Linux) | Saved browser identity and cookies; empty = keep nothing on disk |
| `SUPER_FETCH_LOG_LEVEL` | `WARNING` | `DEBUG` traces the browser's wait loop (on stderr) |
| `SUPER_FETCH_HTTP_HOST` | `127.0.0.1` | Address `serve --http` listens on |
| `SUPER_FETCH_HTTP_PORT` | `8000` | Port `serve --http` listens on |
| `SUPER_FETCH_HTTP_TOKEN` | none | Bearer token `serve --http` requires (at least 32 characters); needed unless listening on loopback |
| `SUPER_FETCH_HTTP_ALLOWED_HOSTS` | none | Comma-separated host names, besides localhost, that requests may be addressed to (the domain or IP agents connect to) |

The data directory holds `identity.json` (the browser fingerprint) and `cookies.json` (cookies from
every site the browser visited, readable only by you). Treat it like a browser profile: don't
share it. Running several servers at once (one per agent session) is fine; they share it.

Private addresses are blocked by default so a prompt-injected agent can't use the server to reach
internal services: checked on every HTTP redirect hop, on every request the browser makes, and on
the browser's final URL.

## Limits

- **IP reputation matters.** Sites that distrust your IP (cloud servers, or after many requests) may
  drop connections or keep challenging no matter how good the fingerprint is. Use
  `SUPER_FETCH_PROXY`. If one site keeps blocking you, `super-fetch reset` gives the browser a new
  identity.
- Plain HTTP only returns what the server sends: on pages that fill in more with JavaScript (e.g.
  long result lists), use `render="always"` for everything.
- Interactive CAPTCHAs (image puzzles), login walls and paywalls are not bypassed; such pages come
  back with a "Blocked by …" warning.

## Development

```bash
git clone https://github.com/ginkcodex/super-fetch-mcp.git && cd super-fetch-mcp
```

`make setup`, `make test`, `make lint`, `make bench` and `make docker-build` wrap the commands below.

```bash
uv sync
uv run pytest                              # browser tests are skipped until `super-fetch install`
SUPER_FETCH_NETWORK_TESTS=1 uv run pytest  # also run tests that need the internet
uv run ruff check . && uv run ruff format .
```

### Benchmark

`bench/run.py` fetches real sites from `bench/sites.toml` (static pages, JavaScript apps, PDFs,
Cloudflare and other challenges) and reports, per site, whether the real content came through,
which tier fetched it, and how long it took:

```bash
uv run python bench/run.py                  # fresh identity, every site
uv run python bench/run.py --passes 2       # run again in a new pipeline: are passed challenges reused?
uv run python bench/run.py --only spa,pdf   # by site name or category
uv run python bench/run.py --json out.json  # keep results to compare before/after a change
```

It exits non-zero if a site not marked `flaky` fails. Sites that judge IP reputation are marked
`flaky`: they can pass one day and block the next from the same machine.
