# MyKnowledge chat Backend

A small web app that lets visitors chat with the MyKnowledge agent (Copilot Studio) from a custom page.

```
browser  ->  this server (FastAPI)  ->  MyKnowledge agent  ->  SharePoint knowledge articles
             holds the service-account sign-in
```

The agent does the searching. This server only signs in as the service account, keeps one agent conversation per
visitor, forwards questions, and turns the agent's replies into simple events for the page. The browser never sees
a token.

## Try the interface first (no sign-in needed)

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
MOCK_AGENT=true uvicorn app.main:app --port 8000      # Windows PowerShell: $env:MOCK_AGENT="true"; uvicorn app.main:app --port 8000
```

Open http://localhost:8000. In mock mode, any question gets a cited answer. Type `follow up` to see a yes/no
question, and `error` to see a failed turn.

## Connect to the real agent

1. Check `.env`. The connection values are already filled in. `SERVICE_ACCOUNT_USERNAME` is the service account's
   sign-in name. **The password never goes in `.env` or any other file.**
2. Make sure `MOCK_AGENT=false`.
3. Sign in once, on the machine that will run the server:
   ```bash
   python login.py
   ```
   A browser opens. Sign in as the service account and type its password there. The script refuses to save the
   sign-in if you used a different account.
4. Check it worked, then ask the agent one real question from the command line:
   ```bash
   python login.py --check
   python smoke_test.py "your question here" --raw
   ```
   `--raw` prints each activity exactly as the agent sent it. If citations or progress events look different from
   what the page shows, that output is what to look at first.
5. Start the server: `uvicorn app.main:app --port 8000`

If the page later says the assistant is signed out, run `python login.py` again. The sign-in can expire or be
revoked by the tenant's policies.

## Deploy on Render (private, password-protected)

Render gives every web service a public URL, so the deployment is kept private with one shared password
(`APP_PASSWORD`). The browser asks for it once; any username works. Only `/healthz` is open, for Render's health
check. `REQUIRE_APP_PASSWORD=true` makes the server refuse to start without a password. Swagger UI is on at `/docs`,
behind the same password.

1. Push this folder to a **private** GitHub repository. `.env`, `.secrets/` and `.venv/` are git-ignored; check
   `git status` shows none of them before pushing.
2. In Render: **New > Blueprint**, connect the repository. Render reads `render.yaml`.
3. When Render asks for the `sync: false` values, set:
   - `SERVICE_ACCOUNT_USERNAME`: the service account's sign-in name
   - `APP_PASSWORD`: a long random password. Share it only with the people who should use the app.
4. In the service's **Environment > Secret Files**, add a file named `msal_cache.bin` with the contents of your
   local `.secrets/msal_cache.bin` (created by `python login.py --link`). `render_start.sh` copies it to a
   writable place at start-up, because MSAL rewrites it when tokens refresh.
5. Deploy. Open the `onrender.com` URL, enter the password, ask a question.

Things to know:
- The uploaded token cache is the service account's long-lived sign-in. Anyone with access to the Render service's
  settings can read it. If the deployment shows "signed out" (the refresh token expired, was revoked, or the
  tenant's Conditional Access blocks sign-ins from Render's network), run `python login.py --link` locally again
  and upload the new file.
- The free plan sleeps after 15 minutes without traffic; the first request after that takes about a minute.
- Run one instance only: sessions are kept in memory.

## API

| Endpoint | What it does |
| --- | --- |
| `POST /api/chat/start` | Starts an agent conversation. Returns `{session_id, greeting}`. |
| `POST /api/chat/message` | Body `{session_id, text}`. Streams Server-Sent Events. |
| `GET /healthz` | Returns `{status, mock}`. |

Events from `/api/chat/message`, one JSON object per `data:` line:

| `type` | Meaning |
| --- | --- |
| `thinking` | The agent is working. Show a progress state. |
| `answer` | `markdown`, `citations` (`position`, `title`, `excerpt`), and `suggestions` (buttons such as Yes and No). |
| `done` | The turn is finished. Sent once, on the agent's `isFinalPlan` event or when the stream ends. |
| `ended` | The agent closed the whole conversation. Start a new one. |
| `error` | `message` says what went wrong in plain words. |

Citations carry the document name and excerpt only. SharePoint links are dropped from citations and from the
answer text (`STRIP_LINK_HOSTS`), because visitors cannot open them.

## Using your own frontend

Replace the files in `static/` or call the API from another app. Send the question to `/api/chat/message` with
`fetch` and read the response body as a stream. `static/chat.js` shows how, including the yes/no follow-up flow
(the follow-up is just another message in the same session).

## Security notes

- **The token cache is a long-lived credential.** `login.py` saves it to `.secrets/msal_cache.bin` with owner-only
  permissions (on Windows, keep the folder private yourself). Anyone who can read it can call the agent as the
  service account. Keep it on the server, out of git, and delete it when you are done.
- Questions and answers are never written to the logs.
- Answers are sanitised in the browser before display, and the page sets a strict Content-Security-Policy.
- **Before putting this on the public internet**, add what this app does not have: HTTPS, rate limiting per visitor
  or IP, and access control if the content should not be open to everyone. Visitors share one service account, so
  anyone who can reach the page can query the knowledge base through it.
- Sessions live in memory (`app/sessions.py`). With more than one server process, swap in a shared store such as
  Redis. The interface is three methods.

## Project layout

```
app/main.py          FastAPI routes, SSE streaming, security headers
app/auth.py          token cache, silent token refresh
app/agent_client.py  real Copilot Studio client and the mock agent
app/mapping.py       agent activities -> browser events
app/sessions.py      session id -> conversation id
login.py             one-time sign-in
smoke_test.py        ask the real agent from the command line
render.yaml          Render Blueprint (deploy settings)
render_start.sh      Render start command (copies the token cache, starts uvicorn)
static/              the chat page (fonts and libraries are bundled, nothing loads from a CDN)
tests/               pytest suite: python -m pytest
```
