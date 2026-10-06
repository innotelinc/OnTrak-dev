# A local Authentik

The range's sign-in is Authentik's: the portal keeps no password and is a
relying party, not an identity provider (`ontrak/oidc.py`, `docs/operations.md`
"Sign-in"). In the estate that Authentik belongs to Cerulean, shared with every
other application.

This directory is the **local** one — a single-purpose Authentik for a laptop, a
demo host or CI, so a range can be signed into without the estate being
reachable. It is a separate compose project with its own database, its own
Redis and its own secret key, and it is deliberately not part of
`docker-compose.yml`: taking the range down must not take the identity provider
down with it, and a range wired to this one is a range no estate user can sign
into.

It is not for production. It serves plain HTTP, its admin password is in a file
next to it, and its seed accounts share one password.

## Use it

```bash
deploy/authentik/setup.sh              # generate secrets, start, provision OnTrak
deploy/authentik/setup.sh --connect    # ... and point the local portal at it
deploy/authentik/setup.sh --down       # stop it (the database is kept)
```

`setup.sh` is idempotent: the second run reuses `.env` and changes only what is
missing. Re-run it after `--down`, or after editing `.env` to add students.

It leaves three things behind:

| file | what it is |
| --- | --- |
| `.env` | the stack's secrets — Postgres, the Authentik secret key, the akadmin password and API token, the OIDC client secret, the seed password. Gitignored. Delete it for fresh secrets. |
| `ontrak-local.env` | a compose env file: the four `ONTRAK_PORTAL__OIDC_*` values, the instructor group, and the address students are sent to. Gitignored — it holds a client secret. |
| the `ontrak-authentik-*` volumes | the database, cache and uploads. `docker volume rm` them to start over. |

## Why the issuer is the host address

The issuer URL has to resolve, and give the same answer, for two different
clients: the student's browser and the portal container. `localhost` is the
*container* to the container and the Windows/desktop host to the browser, and an
issuer is not allowed to depend on who is asking. So `setup.sh` uses the
machine's own address (`ONTRAK_LOCAL_HOST` to force a name) and registers the
callbacks the range answers on:

```
http://localhost:8080/oidc/callback
http://127.0.0.1:8080/oidc/callback
http://<host>:8080/oidc/callback
```

Every one of those has to be on the provider as well — a callback Authentik was
not told about is refused before anyone types a password, which is the same rule
the estate's three names follow.

## Wiring the range by hand

`--connect` is a convenience; the underlying move is one command, and it never
writes to the operator's `.env`. The checkout's env file goes first so the range
keeps everything else it has, and the local one second so it wins where the two
disagree:

```bash
docker compose --env-file .env --env-file deploy/authentik/ontrak-local.env \
  up -d portal gateway
```

Both services, not just the portal: the portal reads the issuer from its
environment, and the gateway is what publishes the address students reach it on
(`ONTRAK_BIND_ADDR`), so recreating one without the other gives a range that
signs in and cannot be reached.

It has to be an env *file* and not `env_file:` on the service: `env_file` is read
after interpolation and loses to the compose file's own `environment:` block,
which declares every one of these variables. `--env-file` is read while
`${...}` is still being resolved, so it is the one that takes effect.

Then open the portal and use **Sign in with Authentik**. The accounts, and the
password they share, are printed by `setup.sh` and live in `deploy/authentik/.env`.

Going back to the estate's Authentik is running compose *without* the second
file — a bare `docker compose up -d` — which reads `ONTRAK_PORTAL__OIDC_*` from
`.env` again. That cuts both ways: any plain `docker compose up -d` while this is
wired up returns the range to the estate's IdP, so re-run `--connect` after one.

## Pieces

| file | what it does |
| --- | --- |
| `compose.yaml` | postgres + redis + Authentik server and worker, one published port |
| `provision.py` | creates the OnTrak application, its OIDC provider, the `groups` scope mapping, `range-instructors` / `range-students`, and the seed accounts, through Authentik's REST API |
| `setup.sh` | detects the host address, generates secrets, starts the stack, waits for readiness, runs the provisioner, writes `ontrak-oidc.env` |

## The one setting the estate also makes

`provision.py` attaches a scope mapping for the scope name `groups`, because
that is the scope the portal asks for (`ontrak/oidc.py`). Authentik's default
`profile` mapping returns a `groups` claim, but the scope name itself has to be
on the provider or the authorize request is refused. Cerulean's
`scripts/authentik-setup.py` does the same thing for the same reason.
