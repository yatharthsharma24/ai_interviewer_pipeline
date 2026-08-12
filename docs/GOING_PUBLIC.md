# Letting real candidates join the interview

By default the interview link is `http://127.0.0.1:8000/interview/<token>`, which only works
on your own machine. To let a candidate join, the server needs a **public https address**.

## Why https specifically

Not a preference — a browser rule. `getUserMedia()`, which grants access to the camera and
microphone, is only available in a *secure context*. Over plain `http` from any origin other
than `localhost`, the browser refuses outright.

So a public `http://your-ip:8000` link fails in a particularly confusing way: the page loads,
looks fine, and then the call never starts. It has to be https.

---

## The quick way: a Cloudflare tunnel

Free, no account, about two minutes. Cloudflare gives you a public https address that
forwards to your local server.

### 1. Install it (once)

```powershell
winget install --id Cloudflare.cloudflared
```

Close and reopen your terminal afterwards so `cloudflared` is on your PATH.

### 2. Set an admin password (once)

Exposing the server exposes the dashboard too. Add this to `.env`:

```bash
ADMIN_PASSWORD=pick-something-long-and-random
```

**The server refuses to start with a public URL unless this is set.** Without it, anyone who
learns the tunnel URL could open `/admin` and read every candidate's name, email, phone,
resume link and interview transcript — and send real invitations.

Candidate links keep working regardless; they are authorised by the secret token in each
personal link, not by this password.

### 3. Start the tunnel

In its own terminal — **leave it running**:

```powershell
cloudflared tunnel --url http://localhost:8000
```

It prints a URL:

```
https://brave-otter-lamp-fix.trycloudflare.com
```

### 4. Start the server with that URL

In a second terminal:

```powershell
python -m app.cli serve --public-url https://brave-otter-lamp-fix.trycloudflare.com
```

That's it. `--public-url` overrides `INTERVIEW_BASE_URL` for this run, so the tunnel URL —
which changes constantly — never gets written into a file.

### 5. Prepare and send, in that order

```powershell
python -m app.cli interview-prepare 1     # mints links using the public URL
python -m app.cli notify 1 --send
```

**Order matters.** `interview-prepare` must run *after* the server knows its public URL, and
*before* `notify`, or the invitation goes out with the wrong address or none at all.

---

## The catch worth understanding

A free Cloudflare quick tunnel gets a **new random URL every time you restart it**. Interview
links you have already emailed contain the old one, and they stop working the moment the
tunnel restarts.

Practically:

| Situation | Does the quick tunnel work? |
|---|---|
| Send links and run interviews in one sitting | Yes — keep both terminals open throughout |
| Send links today, interview tomorrow | **No** — the URL will have changed |
| Laptop sleeps between sending and interviewing | **No** |

Join tokens themselves never change and are stored in the database; it is only the *base
address* that moves. So after a tunnel restart you can re-run `notify --retry-all` to send
corrected links — but a candidate who clicks the old email gets nothing.

For anything beyond a single session you need a stable address:

- **Cloudflare named tunnel** — permanent subdomain on a domain you own, still running on
  your machine. `cloudflared tunnel create`, then point a DNS record at it.
- **A deployment** — Render, Railway, or Fly.io. Permanent URL, but a free host usually
  wipes its disk on redeploy, so the SQLite files need a persistent volume or a move to
  Postgres (`DATABASE_URL` already supports it).

---

## Checking it worked

Open `/admin` → **System check**. Two rows tell you whether you are ready:

| Check | What you want |
|---|---|
| **Interview join URL** | `ok` with your https address, not a warning about localhost |
| **Admin access** | `ok — Password protected` |

Then open a candidate's join link on a **different device** — your phone on mobile data is
the honest test, since it proves the link works from outside your network. You should see the
interview page and a camera permission prompt.

---

## Troubleshooting

**"Refusing to start: the server is publicly reachable but the admin dashboard has no
password"** — set `ADMIN_PASSWORD` in `.env`. This is the guard doing its job.

**Candidate sees the page but the interview never starts** — almost always an http link. Check
the address bar actually says `https://`.

**Candidate gets 404** — the tunnel restarted and the URL changed. Restart the server with the
new `--public-url` and re-send.

**Tunnel URL loads but shows a Cloudflare error** — your local server is not running, or is on
a different port than the tunnel expects.

**Dashboard keeps asking for a password** — username is `admin` unless you changed
`ADMIN_USERNAME`. Basic auth prompts once per browser session.

**Camera works locally but not through the tunnel** — check for a mixed-content error in the
browser console. Every asset must be https, which the tunnel handles automatically.

**The call connects but nobody speaks, and the console shows a WebSocket error** — this only
affects the Gemini path, which relays audio over a WebSocket to `/interview/<token>/live`.
Cloudflare tunnels proxy WebSockets without any extra configuration, but a corporate proxy
or a reverse proxy you put in front yourself may not. If you are running nginx, it needs the
upgrade headers:

```nginx
location /interview/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;   # an interview is longer than the 60s default
}
```

The OpenAI path is unaffected — it is WebRTC straight from the browser to OpenAI and never
touches your server. Run `python -m app.cli interview-backend` to see which one is primary.
