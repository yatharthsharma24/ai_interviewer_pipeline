# Google Cloud setup (one-time, ~10 minutes)

The agent creates Google Forms, reads their responses, and emails the link out. That needs
an OAuth client so Google knows which app is asking, and your consent so it can act on your
account.

## 1. Create a project

1. Go to <https://console.cloud.google.com/>.
2. Project picker (top bar) → **New project** → name it e.g. `interview-pipeline` → **Create**.
3. Make sure the new project is selected before continuing.

## 2. Enable the three APIs

Go to **APIs & Services → Library** and enable each of these:

| API | Used for |
|---|---|
| **Google Forms API** | creating the form, reading responses |
| **Google Drive API** | locating/sharing the form file |
| **Gmail API** | sending the form link |

If you never want the agent sending email, skip Gmail and remove the
`gmail.send` scope from `app/google_api/auth.py`.

## 3. Configure the consent screen

**APIs & Services → OAuth consent screen**

- User type: **External** (unless you are on Google Workspace, where **Internal** is simpler).
- App name, support email, developer email — anything sensible.
- **Scopes**: you can leave this empty; the app requests scopes at runtime.
- **Test users**: add the Google account that will own the forms. This matters — while the
  app is in *Testing* mode only listed test users can authorise it.
- Save. You do **not** need to submit for verification for personal/internal use.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID**

- Application type: **Desktop app**
- Name: `interview-pipeline-cli`
- **Create**, then **Download JSON**.

Save that file as:

```
interview_pipline/secrets/credentials.json
```

(`secrets/` is already in `.gitignore` — never commit it.)

## 5. Sign in

```bash
python -m app.cli google-login
```

A browser opens, you pick the account and approve. The resulting token is cached at
`secrets/token.json` and refreshed automatically from then on.

## Troubleshooting

**"Access blocked: app has not completed verification"**
The signing-in account is not on the test-user list. Add it under OAuth consent screen →
Test users.

**"insufficient authentication scopes" after adding a feature**
Scopes are baked into the cached token. Delete `secrets/token.json` and run
`google-login` again.

**`link-form` returns 403 or 404 on someone else's form**
The Forms API can only read forms the signed-in account can *edit*. Get edit access to the
form, or have its owner run this tool.

**Refresh token expired (app in Testing mode)**
Google expires refresh tokens for unverified apps after 7 days. Delete
`secrets/token.json` and re-run `google-login`, or publish the app to Production on the
consent screen.
