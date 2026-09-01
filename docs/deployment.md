# Deployment

Bragi runs on `sagepi` as a [Komodo](https://komo.do) git-sourced Stack,
not plain `docker compose`. Komodo pulls `main` from
`github.com/NickMarcha/bragi`, builds the image on the host, and brings the
container up. The `docker-compose.yml` in this repo is what the Stack
mirrors, so `docker compose up -d --build` still works for local testing.

The full history is deck-assistant issue #061. This is the operational
summary.

## The Stack

| Field | Value |
|---|---|
| Stack name | `bragi` |
| Stack id | `6a7a58b600e582efdd9c5383` |
| Server | `sagepi` (Komodo Server `6a7a48d900e582efdd9c51ae`) |
| Repo / branch | `NickMarcha/bragi` / `main` |
| `run_build` | `true` (no prebuilt image, Komodo builds from the `Dockerfile`) |
| `auto_pull` | `false` (see below) |
| `poll_for_updates` / `auto_update` | `true` |
| `webhook_enabled` / `webhook_force_deploy` | `true` (see below) |
| Clone path on host | `/home/sage/.local/share/komodo-periphery/stacks/bragi` |

`sagepi` also runs a Komodo Periphery agent (systemd `--user`, no Docker
needed for it) registered as a Server for host monitoring: CPU, memory,
disk, network, plus Ntfy alerting through the fleet's existing alerter.

### auto_pull must be false

Komodo's `auto_pull` defaults to `true`, which runs `docker compose pull`
before deploying. For a `build:`-only service with no registry image that
fails outright:

```
pull access denied for bragi, repository does not exist
```

Set `auto_pull = false` for this Stack. Any build-from-source Stack needs
the same.

### run_build must be set at creation

`run_build` defaults to `false`, which silently defeats redeploy-on-change:
Komodo pulls the new commit but never rebuilds the image. It was set to
`true` when the Stack was created.

## Exposure

The container publishes `127.0.0.1:8000` only. `tailscale serve` puts an
HTTPS listener in front of it:

```
tailscale serve --bg --https=443 http://127.0.0.1:8000
```

Live at `https://sagepi.tail08dfa.ts.net/`, tailnet only, with a valid
Tailscale-issued cert. This matches the `127.0.0.1` + `tailscale serve`
pattern used for Komodo and Portainer on HomeServer.

One-time setup so `tailscale serve` does not need sudo:

```
sudo tailscale set --operator=sage
```

## Deploy webhook

A push to `main` redeploys the Stack in seconds instead of waiting for the
poll interval.

Komodo Core on HomeServer is tailnet only, so GitHub cannot reach it
directly. deck-assistant #074 stood up a public proxy for exactly this: a
`cloudflared` service inside Komodo's own compose that forwards
`hooks.nickmarcha.com/listener/*` to `http://core:9120`, with three
independent controls:

1. Path-scoped ingress. The Cloudflare hostname only routes `^/listener/.*`
   (a regex). Everything else returns 404.
2. WAF allowlist to GitHub's published hook IP ranges
   (`api.github.com/meta`).
3. HMAC via Core's `KOMODO_WEBHOOK_SECRET`. The Bragi Stack's own
   `webhook_secret` is left blank, so it uses that global secret, the same
   one `dgg-radio` uses.

Webhook URL:

```
https://hooks.nickmarcha.com/listener/github/stack/6a7a58b600e582efdd9c5383/deploy
```

Configured on the repo as a `push` event, content type `application/json`,
secret = `KOMODO_WEBHOOK_SECRET`.

### webhook_force_deploy is mandatory here

A `/deploy` webhook actually runs `DeployStackIfChanged`, which compares the
contents of `docker-compose.yml`, not the commit hash. Bragi builds its
image from `app/`, so a normal code change never touches the compose file
and would never trigger a rebuild under the default.

`webhook_force_deploy = true` makes the webhook run an unconditional
`DeployStack`. Set it on this Stack. Any build-from-source Stack needs it;
the default only makes sense for Stacks that pull published images.

### Testing the webhook

HomeServer shares the household public IP, so a signed test payload sent
from any machine on the home network hits the WAF allowlist and gets a 403.
You cannot probe the endpoint from inside the house. Verify a real push
instead:

```
.assistant/scripts/komodo_api.sh list-updates      # or POST /read/ListUpdates
```

A webhook-triggered deploy shows a different operator id than a manual one.

## Manual deploy

From deck-assistant (has the Komodo API credentials):

```
.assistant/scripts/komodo_api.sh deploy bragi
```

For a brand-new Stack, `refresh-cache bragi` first.

## Dashboard latency history

The first deployed version took 6.76s per page load. `list_nodes()` was
calling `wpctl get-volume` in a subprocess for every node in the graph (21
on `sagepi`), and the route handlers called `list_nodes()` three times per
peer. That was roughly 189 `wpctl` spawns for one page load.

Fixed by fetching the node list once per request and threading it through,
and calling `wpctl get-volume` only for the one to six nodes a request
actually needs. 6.76s to 0.5s, about 13x.

Reading volume straight from `pw-dump`'s `channelVolumes` was considered and
rejected: `wpctl` applies a cube-root scaling curve that the raw value does
not (0.40 displayed is 0.064 raw), so raw reads would drift from what
`wpctl set-volume` writes. The fix kept `wpctl` for reads and just called it
far less often.

## References

- deck-assistant #061, the full deployment and audio-bridge history.
- deck-assistant #074, the `hooks.nickmarcha.com` webhook proxy design and
  the `webhook_force_deploy` finding.
