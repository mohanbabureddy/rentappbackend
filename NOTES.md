# Operational notes

Things learned the hard way while running this app on Render/Supabase/Resend.
Read this before debugging something that looks like it "should just work."

## Render env var changes need a real deploy, not just a restart

Changing an environment variable in Render's dashboard (or via API) does
**not** get picked up by a plain restart (`render restart`, or the restart
API endpoint) -- the running container keeps using whatever env vars it was
originally provisioned with. Only a real deploy (new commit, or "Manual
Deploy" in the dashboard) re-injects the current env var values.

Symptom: you update an env var, restart the service, `/api/version` shows a
fresh `startedAt`, but the app still behaves like the old value is set. This
cost real debugging time once already (MAIL_SENDER kept showing the old
sandbox sender for ~20 minutes across two restarts, until an actual code
push forced a real deploy).

**Rule of thumb:** after changing an env var on Render, always follow up
with a real deploy (push any commit, even a trivial one) rather than
trusting a restart alone.

## GitHub repo renames leave a working redirect

Both `rentapp-python-backend` -> `rentappbackend` and `Rentapp` ->
`Rentappfrontend` got renamed at some point. Git pushes to the old remote
URL still work (`git push origin main` succeeds) via GitHub's redirect, but
it prints a warning each time. Not urgent to fix, but if `git remote -v`
ever looks stale, that's why -- update with `git remote set-url origin
<new-url>` if it gets annoying.

## Cloudflare DNS: new records default to "Proxied"

Every time a new CNAME record needs to be added for domain verification
(Resend's DKIM/SPF records, Render's custom domain CNAMEs), Cloudflare
defaults it to "Proxied" (orange cloud). Proxied records return Cloudflare's
own IPs to external resolvers instead of the real target, which silently
breaks verification for both Resend and Render. Always flip new
verification-related records to **"DNS only" (grey cloud)** immediately
after creating them. Apex/root domain CNAMEs get auto-flattened to A
records by Cloudflare -- checking with `nslookup -type=CNAME` on an apex
domain will show nothing even when it's working; check with a plain
lookup (no -type flag) instead.

## Supabase connection strings need the psycopg (v3) driver, not psycopg2

`postgresql://` URLs default to the old `psycopg2` driver in SQLAlchemy.
`psycopg2-binary` has no prebuilt wheel for newer Python versions (build
fails without local PostgreSQL dev headers). `app/database.py` rewrites
plain `postgresql://`/`postgres://` URLs to `postgresql+psycopg://`
automatically, so Supabase/Neon connection strings can be pasted in as-is.

Use the **Session pooler** connection string from Supabase (not
"Transaction" mode) for a long-running Flask app with its own connection
pool.

## Resend's sandbox sender only delivers to the account owner

`onboarding@resend.dev` (Resend's shared test domain) can only deliver to
the email address the Resend account itself was signed up with. It returns
`200 OK` for sends to *any* address, but silently fails delivery (or
`403`s) for anyone else -- looks like success in the app logs, but nothing
arrives. A verified custom domain (DKIM + SPF records) is required before
real users can receive email.

## Occupant document storage: forward slashes only

`Occupant.aadhar_storage_path` is a URL/object-key path
(`aadhaar/<tenant>/<file>`), not a filesystem path. Building it with
`str(Path(...))` uses backslashes on Windows, which silently breaks both
Supabase Storage object-key lookups and the `/uploads/...` URL scheme.
Always build this field with an explicit forward-slash f-string.

## Local vs production settings (database is the only difference)

| | Local (your PC) | Production (Render) |
|---|---|---|
| Where settings live | `Python/.env` (git-ignored) and `frontend/.env.development` (git-ignored) | Render service, **Environment** tab |
| `APP_ENV` | `local` | `production` |
| `DB_URL` | `mysql+pymysql://root:<password>@localhost:3306/rent_app` | the hosted Supabase Postgres URL |
| Razorpay, Resend, JWT, LLM keys | same keys | same keys |
| Uploaded documents | saved in `Python/uploads/` (no `SUPABASE_*` set) | Supabase Storage (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`) |

- `app/database.py` refuses to start if `APP_ENV=local` points at a remote database, or if
  `APP_ENV=production` points at this machine / SQLite. If `APP_ENV` is unset nothing is enforced.
- Never put the Supabase URL in the local `Python/.env`; use the guard error as the reminder.
- Frontend: `npm start` reads the API address from `frontend/.env.development`; the production
  build takes `REACT_APP_API_BASE` from Render.
- Running the local backend: `python -m flask --app app run --port 5000` (needs MySQL running).

## Owner details in every email (set on Render too)

The name/address/phone in the footer of every email come from three env vars,
not from code (both repos are public, so a personal address and phone must
not be committed): `MAIL_OWNER_NAME`, `MAIL_ADDRESS` (lines separated by `|`)
and `MAIL_PHONE`. Locally they live in `Python/.env`. On Render they must be
added to the backend service's Environment and then followed by a real deploy
(see the env-var note above) -- until then production emails simply have no
footer. Anything left unset is just omitted.
