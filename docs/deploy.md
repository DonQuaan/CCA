# Public demo: Render (free) and Hugging Face Spaces (paid)

CCA's public demo runs a released container image, `ghcr.io/donquaan/cca:<version>`, with
`cca play` in public mode ([simulator.md](simulator.md#public-mode)). Two hosts are prepared.
**Render's free plan is the primary one**: it costs nothing and needs no card, but gives the demo
a tenth of a CPU. A Hugging Face Docker Space is the alternative when more CPU is wanted; it
needs a paid Hugging Face plan.

| | [Render](#render-free-plan), free web service | [Hugging Face](#hugging-face-spaces-paid-alternative), Docker Space |
|---|---|---|
| Price | Free; "No credit card is required" ([Render](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026)) | A paid plan: PRO, $9 per month, for a personal account ([pricing](https://huggingface.co/pricing)) |
| CPU and memory | 0.1 CPU, 512 MB ([blueprint-spec](https://render.com/docs/blueprint-spec), plans table) | CPU Basic: 2 vCPU, 16 GB |
| When idle | Spun down after 15 minutes without traffic; about a minute to spin up | Asleep after 48 hours without visitors |
| Files | [`deploy/render/render.yaml`](../deploy/render/render.yaml), a Blueprint | [`deploy/huggingface/`](../deploy/huggingface/), Dockerfile and card templates |
| Workflow | [`deploy-render.yml`](../.github/workflows/deploy-render.yml) ("Deploy Render") | [`deploy-space.yml`](../.github/workflows/deploy-space.yml) ("Deploy Space") |
| How a deploy reaches the host | The service's deploy hook, with the image pinned by digest | A git push of the rendered Dockerfile, with the image pinned by digest |
| Shown in a frame | No | On huggingface.co only |

> **Status (2026-09-29).** Nothing is deployed on either host. The Render service needs the
> owner's one-time steps below and a release whose tag holds `deploy/render/` and whose
> `cca play` knows the Blueprint's flags (public mode, `--max-think-seconds`,
> `--log-forwarded-hops`). v0.1.0 has none of them; v0.2.0 is the first release that can be
> deployed. The run under
> the free plan's limits ([Testing under the free plan's limits](#testing-under-the-free-plans-limits))
> has not run yet either: the machine this was written on had no working Docker, so its first
> numbers will come from GitHub Actions.

Contents: [Render (free plan)](#render-free-plan): [what the service runs](#what-the-render-service-runs) ·
[setup](#render-setup-owner) · [how a deploy works](#how-a-render-deploy-works) ·
[client address](#calibrating-the-client-address) ·
[what the free plan means for visitors](#what-the-free-plan-means-for-visitors) ·
[redeploying, rolling back, stopping](#render-redeploying-rolling-back-and-stopping) ·
[testing under the limits](#testing-under-the-free-plans-limits) ·
[security](#render-security-notes) · [troubleshooting](#render-troubleshooting) ·
[sources](#render-sources-and-open-questions).
[Hugging Face Spaces (paid alternative)](#hugging-face-spaces-paid-alternative).

## Render (free plan)

### What the Render service runs

| File | What it is |
|---|---|
| [`deploy/render/render.yaml`](../deploy/render/render.yaml) | The [Blueprint](https://render.com/docs/infrastructure-as-code): one free, image-backed web service named `cca-demo`, with its command, `PORT`, health check path and region. Its image tag is the package version of the commit it is in (`docker/tests/test_deploy_render.py` checks this). On a release commit that is the release being made, whose `cca` the command was tested against; between two releases it is the previous release, whose `cca` may lack options that the command already passes. So sync the Blueprint only when `blueprint-check` passes (below). |
| [`deploy/render/service.py`](../deploy/render/service.py) | The workflow's helper: settings check, version, Blueprint check, digest, deploy hook, wait. Standard library only; it loads the registry client of `deploy/huggingface/space.py` rather than keep a second copy. `python deploy/render/service.py blueprint-check` tells whether a Blueprint sync now would start a release that knows every option of the command. |
| [`deploy/render/blueprint.py`](../deploy/render/blueprint.py) | Reads the Blueprint's command and image tag without a YAML library, for `service.py` and `docker/constrained.py` alike. |
| [`.github/workflows/deploy-render.yml`](../.github/workflows/deploy-render.yml) | The deploy workflow, *Deploy Render*. |
| [`docker/constrained.py`](../docker/constrained.py) | Runs the Blueprint's command under 0.1 CPU and 488 MiB (under 512 MB in either unit) and plays a short game; `image.yml` runs it on every image build, releases included. |

The service's command, after the image's `ENTRYPOINT ["/usr/bin/tini", "--", "cca"]`:

```text
play --public --host 0.0.0.0 --port 8765 --no-browser --human qre
     --hash 16 --threads 1 --nodes 20000 --max-sessions 12 --max-think-seconds 20
     --allowed-host cca-demo.onrender.com
     --trusted-proxies 0 --log-forwarded-hops
```

- **Port 8765, with `PORT=8765`.** "The default value of `PORT` is `10000` for all Render web
  services. You can override this value by setting the environment variable"
  ([web-services](https://render.com/docs/web-services#port-binding)). The Blueprint sets
  `PORT` to 8765, which is the image's `EXPOSE` port, the port of its `HEALTHCHECK` and the local
  default, and passes `--port 8765` literally, because the docs do not say whether Render expands
  `$PORT` inside the Docker command. If Render cannot find the bound port, "your web service's
  deploy fails and displays an error in your logs" (same page).
- **The command replaces the image's `CMD`.** Render calls it "a custom Docker `CMD`"
  ([deploying-an-image](https://render.com/docs/deploying-an-image)), and without it "Render
  uses the `CMD` defined in the `Dockerfile`"
  ([blueprint-spec](https://render.com/docs/blueprint-spec)). So it starts with `play`, the
  arguments that the image's `ENTRYPOINT` expects, like the image's own `CMD`. That Render keeps
  the `ENTRYPOINT` is not documented; [Troubleshooting](#render-troubleshooting) says what to do
  if the first deploy shows otherwise.
- **Plain words.** No quotes, `$` or other shell characters: splitting on spaces and a shell
  give the same arguments, and the docs do not say which of the two Render does.
  `docker/tests/test_deploy_render.py` keeps it that way.
- **An engine budget for 0.1 CPU.** Stockfish gets a 16 MB hash, 1 thread and 20000 nodes per
  evaluation. At most 12 games stay in memory. `--max-think-seconds 20` caps each CCA decision
  once it has the engine: "the search is shortened to meet it (not reproducible then)"
  (`cca play --help`). The human model is QRE: the Maia-2 image needs torch and a 267 MB
  checkpoint, and whether that fits in 512 MB is unknown.
- **Health checks.** `healthCheckPath: /healthz`. Render "considers a check successful if the
  instance responds with a `2xx` or `3xx` status code within five seconds"; it stops routing to
  an instance after 15 seconds of failed checks, restarts it after 60 seconds, and cancels a
  deploy that is not healthy within 15 minutes, "and continues routing traffic to your service's
  existing instances" ([health-checks](https://render.com/docs/health-checks)). `/healthz`
  answers 200 while the engine starts and once it is ready, and 503 after the engine failed, so
  a dead engine gets the instance restarted. Because it also answers 200 while the engine is
  still starting, Render may route the first visitors to the new instance before the engine is
  ready.
- **Host name.** "If your service has any verified custom domains, Render sets one of those
  domains as the value of the `Host` header for all HTTP health checks. Otherwise, Render uses
  the service's `onrender.com` subdomain" (same page). That name must be an `--allowed-host`:
  public mode answers any other name with 421, the health checks fail, and the deploy never
  goes live. The subdomain "incorporates" the service name
  ([web-services](https://render.com/docs/web-services)); whether Render adds a suffix to keep
  it unique is not documented, so check it after the first deploy
  ([setup step 4](#render-setup-owner)). `RENDER_EXTERNAL_HOSTNAME` holds it inside the
  container ([environment-variables](https://render.com/docs/environment-variables)).
- **No framing.** Without `--frame-ancestor`, public mode forbids showing the page in a frame.
- **Client address.** `--trusted-proxies 0` ignores `X-Forwarded-For`, and
  `--log-forwarded-hops` counts its entries in the request log. Calibrate it after the first
  deploy ([Calibrating the client address](#calibrating-the-client-address)).
- **Region.** `singapore`, the Render region nearest to Vietnam. The region cannot be changed
  once the service exists ([blueprint-spec](https://render.com/docs/blueprint-spec)): edit it
  before the first sync if the visitors are elsewhere (`oregon`, `ohio`, `virginia`,
  `frankfurt`).

### Render setup (owner)

These steps need a person: they create an account and handle a secret. Nothing here needs a
card.

1. **Render account.** Sign up at <https://dashboard.render.com> with **GitHub** (Render also
   offers Google, GitLab, Bitbucket, or an email address and a password;
   [login-settings](https://render.com/docs/login-settings)). "No credit card is required"
   ([Render](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026)).
   Without a payment method, what would cost money stops the free services instead of being
   billed: using up the month's outbound bandwidth, for example ("If you haven't added a
   payment method, Render instead suspends all of your Free services for the remainder of the
   month", [free](https://render.com/docs/free)).
2. **Check the Blueprint** in `deploy/render/render.yaml` on `main`: the `region` (fixed once
   the service exists), the service `name` (it gives the host name), and the image tag, which
   must be a release that ships `deploy/render/` (the first release after v0.1.0). In a
   checkout of `main` with its tags (`git fetch --tags`), run
   `python deploy/render/service.py blueprint-check`: it passes when the Blueprint of the tag
   `v<image tag>` passes every option that the command on `main` passes. That release's own
   tests parsed its Blueprint's command with its own `cca` before its image was pushed, so its
   `cca play` knows those options. Between two releases `main` may already carry options of the
   next one: the check then fails, and the Blueprint must wait for that release.
3. **Create the service**, preferably from the Blueprint
   ([infrastructure-as-code](https://render.com/docs/infrastructure-as-code)):
   - *New > Blueprint*, then *Connect* next to `DonQuaan/CCA` (Render asks to connect the
     GitHub account first).
   - Name the Blueprint, choose the branch `main`, and set *Blueprint Path* to
     `deploy/render/render.yaml` (the default is a `render.yaml` at the repository's root).
   - Review the changes (a web service `cca-demo`, plan free) and click *Deploy Blueprint*.
   - Then, on the Blueprint's *Settings* page, set *Auto Sync* to *No*. With Auto Sync on,
     "Render automatically updates affected resources every time you push Blueprint changes to
     your linked branch" (same page). A release commit bumps the Blueprint's image tag on `main`
     before `release.yml` has pushed that image, so an automatic sync would try to deploy an
     image that does not exist yet, and a commit that adds an option for the next release
     would start the current one with it. With Auto Sync off, click *Manual Sync* after a
     change to the Blueprint (for example step 4 or a calibration), once the image it names
     exists and `blueprint-check` passes on `main`.

   Without a Blueprint, *New > Web Service*, then under *Source Code* *Existing Image*
   ([deploying-an-image](https://render.com/docs/deploying-an-image)): the image URL
   `ghcr.io/donquaan/cca:<version>` (public, no credentials), *Connect*, the name `cca-demo`, the
   region and the **Free** compute plan; under *Advanced*, the Docker command (the command above
   on one line), the environment variable `PORT` = `8765` and the health check path `/healthz`;
   then *Deploy*. Such a service does not follow `render.yaml`: keep the two in step by hand.
4. **Host name.** The service's page shows its URL. If it is not
   `https://cca-demo.onrender.com`, the health checks get 421 and the first deploy is canceled
   after 15 minutes: that is expected. Put the real name on the `--allowed-host` line of
   `render.yaml`, commit it to `main`, and sync the Blueprint (or edit the Docker command in the
   service's settings, for a service made without a Blueprint). For a custom domain, add its name
   to that line (`--allowed-host` repeats) **before** you verify the domain: once it is
   verified, the health checks send it as `Host`.
5. **Deploy hook.** On the service's *Settings* page, copy the *Deploy Hook*. "Your deploy hook
   URL is a secret! [...] If you believe a deploy hook URL has been compromised, replace it by
   clicking *Regenerate Hook*" ([deploy-hooks](https://render.com/docs/deploy-hooks)).
6. **GitHub environment `render`**, in *Settings > Environments* of `DonQuaan/CCA`, as for the
   Space ([One-time setup](#one-time-setup-owner), step 5):
   - *Deployment branches and tags:* the branch `main` and the tag pattern `v*`; optionally a
     required reviewer.
   - **Environment secret** `RENDER_DEPLOY_HOOK_URL`: the deploy hook. (A repository secret
     works too, but reaches every workflow run of the repository that names it.)
   - **Variable** `RENDER_URL`: the service's URL, for example `https://cca-demo.onrender.com`
     (an environment or a repository variable), without a path.
7. **Deploy.** *Actions > Deploy Render > Run workflow* from `main`, with `version` set to
   `latest` or to a version such as `0.2.0`.
8. **Calibrate the client address** ([next section but one](#calibrating-the-client-address)).

### How a Render deploy works

`deploy-render.yml` has one job, in the GitHub environment `render`. It holds no Render API key.
Its steps run in this order; a failing step stops the run with a GitHub error annotation, which
the helper writes to stderr, so it shows even when a step keeps the helper's stdout
(`x=$(service.py ...)`).

1. **Check the settings.** The secret `RENDER_DEPLOY_HOOK_URL` must be set, and the variable
   `RENDER_URL` must be `https://` and a host name, without a path. This step only learns
   whether the secret is set.
2. **Version.** The release's tag on a `release` event, otherwise the `version` input; `latest`
   is the latest GitHub release. `v0.2.0` and `0.2.0` both become the image tag `0.2.0`
   (`space.py`'s rule: a normalised PEP 440 version).
3. **The version knows the command.** A deploy through the hook changes the image, not the
   command: the service runs the Docker command of its settings, the one of its last Blueprint
   sync (the docs describe `imgURL` as replacing the image's tag or digest and mention no
   command). So `service.py blueprint-check <version>` requires that the tag `v<version>` holds
   `deploy/render/render.yaml` and that its command passes every option the Blueprint of this
   run's ref passes (values may differ: host names, the proxy count). Otherwise that
   version's `cca play` may not know an option, and its container would stop at start with
   `unrecognized arguments`; it is refused before Render is asked. That covers a version
   released before the Render deploy and a rollback past an added option. The check cannot see
   the command the service really runs, only the Blueprint of the ref: run the workflow from
   `main`, the branch the Blueprint syncs from, and keep the service synced with it. On a
   `release` event the ref is the release's own tag, so the check passes by construction.
4. **Digest.** The manifest of `ghcr.io/donquaan/cca:<version>`, pulled anonymously and hashed
   (retried through rate limits and outages). A missing tag stops the run here.
5. **Trigger.** A `POST` to the deploy hook with `imgURL` set to
   `ghcr.io/donquaan/cca@sha256:...`: "you can optionally specify a tag or digest by appending
   an `imgURL` query parameter to the deploy hook URL"
   ([deploying-an-image](https://render.com/docs/deploying-an-image)), and "All components of
   `imgURL` *besides* the tag or digest must match your service's default image URL"
   ([deploy-hooks](https://render.com/docs/deploy-hooks)). The helper reads the hook from its
   environment, checks that it is `https://api.render.com/deploy/srv-...?key=...` with no other
   parameter, and never prints it, its key or Render's answer body. It prints the deploy's id.
   Render's answers: 200 started, 202 queued behind another deploy, 400 bad `imgURL`, 401 bad
   key, 404 no such service (the deploying-an-image page gives 404 for a bad `imgURL` as well),
   409 suspended ([deploy-hooks](https://render.com/docs/deploy-hooks)). 429, 5xx and network
   errors are tried three times in all; a second deploy of the same digest does no harm.
6. **Wait.** For up to 10 minutes, every 15 seconds, `GET $RENDER_URL/healthz`, one log line
   each, until it answers `status: ok`, `ready: true` and the deployed `version`. The first
   request also wakes a spun-down service. The step fails at once on 421 (the URL's host is not
   an `--allowed-host`) or on 503 with `status: error` from the new version (its engine failed).

What the helper cannot see: `/healthz` reports the version, not the image digest. A redeploy of
the version that is already live is therefore confirmed by the running instance at once; the
service's *Events* page in the Render dashboard shows the deploy itself.

The deploy hook changes the running image only: "for *future* deploys, your service continues to
use the tag or digest in its settings" ([deploying-an-image](https://render.com/docs/deploying-an-image)).
A Blueprint sync or a manual deploy from the dashboard uses the service's own tag, the
Blueprint's, which is why that tag moves with every release. The command is the other way
round: only a Blueprint sync (or an edit in the service's settings) changes it, whatever image
a deploy names.

**Deploying after a release.** `release: published` does not fire for a release that
`release.yml` publishes with `GITHUB_TOKEN` (see [Deploying a release](#deploying-a-release)):
run the workflow by hand after such a release, or add a job to `release.yml` that calls
`deploy-render.yml` (`workflow_call`, `secrets: inherit`), like the sketch for the Space. The
release event never deploys a pre-release.

### Calibrating the client address

Public mode keys each visitor's limits (games, decisions per minute, requests at once) by
address. Behind Render's proxies the TCP peer is a proxy, and the visitor's address is in
`X-Forwarded-For`. Render's documentation names no client-address header: its full docs corpus
(`https://render.com/docs/llms-full.txt`, generated 2026-09-26) mentions none of
`X-Forwarded-For`, `True-Client-IP`, `X-Real-IP` or `CF-Connecting-IP`. It says only that Render
uses "Cloudflare's [...] DDoS protection infrastructure"
([ddos-protection](https://render.com/docs/ddos-protection)). Evidence from outside the docs, not
authoritative:

- A header dump from a Render service in April 2025 shows
  `x-forwarded-for: <client>, 172.71.195.123, 10.226.90.65`, three entries, with
  `cf-connecting-ip` equal to the client
  ([arcjet-js#3899](https://github.com/arcjet/arcjet-js/issues/3899)).
- A community thread of March 2025 reports that Express's `trust proxy 3` worked
  ([archived](https://web.archive.org/web/20250421133701/https://community.render.com/t/what-number-of-proxies-sit-in-front-of-an-express-app-deployed-on-render/35981)).
- A 2024 comment on Render's feedback board says that Render appends to an
  `X-Forwarded-For` the client sent instead of replacing it
  ([feedback board](https://feedback.render.com/features/p/send-the-correct-xforwardedfor)).
  Entries on the left can therefore be forged; `cca play` counts from the right.

So the Blueprint ships `--trusted-proxies 0 --log-forwarded-hops`: the header is ignored,
visitors behind the same proxy address (possibly all) share one set of limits (too strict, but
nobody can dodge them), and each request-log
line carries `xff=N`, the number of `X-Forwarded-For` entries, never an address
(`cca play --help`). After the first deploy:

1. Open the demo from two or three networks (for example home Wi-Fi and a phone on mobile data)
   and play a move each time.
2. In the Render dashboard, open the service's *Logs*
   ([troubleshooting-deploys](https://render.com/docs/troubleshooting-deploys)) and read the
   `xff=` values of those requests (`/healthz` lines are Render's health checks: skip them).
3. Take the **smallest** count as N: a client can only add entries on the left, so the smallest
   count is the number the proxies append.
4. Replace the CLIENT-IP line of `render.yaml` with `--trusted-proxies N` (keep
   `--log-forwarded-hops` to go on watching), commit to `main`, and sync the Blueprint.
5. Check that forged entries do not help. This sends 30 position-lab analyses, 10 at a time, each
   with another forged address from 192.0.2.0/24 (reserved for documentation). With N right they
   all count against your one address, and some are answered 429; if none is, N is too large:
   go back to 0. (Not run yet: there is no service.)

   ```bash
   url=https://cca-demo.onrender.com   # your RENDER_URL
   fen='rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
   seq 1 30 | xargs -P 10 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
     -X POST "$url/api/analyse" -H 'Content-Type: application/json' \
     -H 'X-Forwarded-For: 192.0.2.{}' -d "{\"fen\": \"$fen\"}" | sort | uniq -c
   ```

What a wrong N does: too small, and the address read is a proxy's, so visitors share limits (too
strict, not spoofable); too large, and it is an entry the client wrote, so limits can be dodged;
fewer entries than N, and the TCP peer is used. Calibrate again after Render changes its edge, or
after putting a proxied ("orange cloud") Cloudflare record in front of a custom domain, which
Render allows once the certificate is issued
([configure-cloudflare-dns](https://render.com/docs/configure-cloudflare-dns)) and which adds
entries.

### What the free plan means for visitors

- **It sleeps.** "Render spins down a Free web service that goes 15 minutes without receiving
  any inbound traffic [...] This process takes about one minute. Render displays a loading page
  to connecting browsers while a service is spinning up" ([free](https://render.com/docs/free)).
  The first visitor after a quiet spell waits about a minute.
- **Games are lost** when the service spins down, restarts or is redeployed: games live in
  memory only, and "Render might restart a Free web service at any time" (same page).
- **Decisions are slow.** The service has 0.1 CPU. Estimate: a whole `cca analyse` run with the
  Blueprint's engine settings (process start and engine start included) took 0.9 to 2.0 seconds
  on an i9-14900HX (three positions, 2026-09-29); at a tenth of a CPU a decision should take
  roughly ten times its CPU time, several seconds, and `--max-think-seconds 20` caps it. The
  constrained run in `image.yml` measures it on GitHub's runners.
- **One small instance for everyone.** Free services cannot scale beyond one instance
  ([scaling](https://render.com/docs/scaling)); all visitors share the 0.1 CPU, 12 games fit in
  memory, and the public-mode limits apply per visitor (until the client address is
  calibrated, visitors behind the same proxy address, possibly all, count as one).
- **Monthly allowances.** 750 free instance hours per workspace per month; a spun-down service
  uses none, and when they run out, "Render suspends all of your Free web services until the
  start of the next month" ([free](https://render.com/docs/free)). One service that never sleeps
  needs at most 744 hours in a 31-day month (arithmetic). The Hobby workspace includes 5 GB of
  outbound bandwidth per month ([outbound-bandwidth](https://render.com/docs/outbound-bandwidth));
  without a payment method, using it up suspends the free services for the rest of the month.
  Render "may suspend a Free web service that initiates an uncommonly high volume of traffic"
  ([free](https://render.com/docs/free)).
- **A demo, not a service.** "Do not use them for production applications" (same page). Do not
  ping the service to keep it awake: Render's
  [Acceptable Use Policy](https://render.com/acceptable-use) forbids an "unreasonable or
  disproportionately large load on our infrastructure [...] especially for the purpose of
  evading payment", and keep-alive traffic is at best a grey area under it.

### Render: redeploying, rolling back and stopping

- **Redeploy or roll back:** run *Deploy Render* from `main` with the version. The service
  keeps the command of its last Blueprint sync, so a release can be deployed when its own
  Blueprint passes every option of `main`'s ([step 3](#how-a-render-deploy-works)); any other
  is refused, and the running service is untouched. To roll back past an option added since,
  first put that release's command back on `main` (keeping the host and client-address lines),
  check that `blueprint-check` passes, sync the Blueprint, then run the workflow. Render's own
  rollbacks on the free plan go "only to the two most recent previous
  deploys" ([free](https://render.com/docs/free)). "Render does not store previously pulled
  Docker images": it pulls for every deploy, and also when a restarted or rescheduled instance
  lands on another machine, and "the image and digest used for the deploy you roll back to must
  be available in the container registry"
  ([deploying-an-image](https://render.com/docs/deploying-an-image)). Do not delete old versions
  from ghcr.io.
- **A changed command** (host name, calibration): edit `render.yaml` on `main`, check that
  `blueprint-check` passes, and sync the Blueprint. The sync deploys the Blueprint's tag with
  that command.
- **Stop deploying:** disable the workflow under *Actions*, or delete the variable `RENDER_URL`:
  every run then stops at the settings check. To shut the demo down, suspend or delete the
  service in the Render dashboard (the exact labels are not checked).
- **Delete:** first remove the service from the Blueprint and sync, then delete it: "If you
  delete a Blueprint-managed resource, Render recreates it the next time you sync your
  Blueprint!" ([infrastructure-as-code](https://render.com/docs/infrastructure-as-code)). Then
  delete the secret `RENDER_DEPLOY_HOOK_URL` and the variable `RENDER_URL` on GitHub.

### Testing under the free plan's limits

`image.yml` builds the default image on every pull request, every push to `main` and every
release (`release.yml` calls it), smoke-tests it, then runs:

```bash
python3 docker/constrained.py cca:ci --blueprint deploy/render/render.yaml --cpus 0.1 --memory 488m
```

The script starts the image with `docker run --cpus 0.1 --memory 488m --memory-swap 488m`
(no swap). Render gives "512 MB" and does not say whether a MB is 10^6 or 2^20 bytes; 488 MiB
(511,705,088 bytes) is under both. The container runs without the image's `HEALTHCHECK`
(Render's docs never mention it, and it would take from the same 0.1 CPU), with the Blueprint's
command after the image's `ENTRYPOINT`, and with the port published on 127.0.0.1 only. When `/healthz` says ready, it plays through the HTTP API as
the page does: a new game as White, three moves (Nf3, Nc3, e3, legal whatever Black answers) and
three CCA decisions. Meanwhile it samples `docker stats` and times `/healthz` every second, as
Render's health checks would. Afterwards it replays the game with the image's own python-chess,
in a container without network, and compares every position. The job summary gets a table of
the latencies, the peak memory (the larger of the sampled `docker stats` and the cgroup's own
`memory.peak`), the highest CPU sample and the slowest `/healthz`.

The step fails if the container is OOM-killed or stops (or the kernel OOM-kills a process in
it, from `memory.events`), if a decision takes longer than `--max-think-seconds` plus 10 seconds,
if a move is illegal or a position differs from the replay, or if the server refuses a request of
the game. A `/healthz` during the game slower than 5 seconds, or answered with another status
than 200, is a warning: Render would count it as a failed check. Nothing is pushed. The same command works on any machine with Docker, for example against
a local build (`docker build -t cca:local .`, then `python docker/constrained.py cca:local`).
The runners' CPUs are not Render's: treat the measured times as an order of magnitude.

**On a release this step is a gate.** `image.yml`'s `publish` job needs `build-test`, so if the
step fails, neither image variant is pushed and the release stops there. Its limits are meant
to catch an image that does not fit the free plan, not a slow runner: a decision may take the
cap plus 10 seconds, and the cap itself shortens the search. A runner far slower than usual
could still trip it, so re-run the failed job once before looking for a cause in the image.

### Render security notes

- The service adds nothing to the release image: Render pulls the image that the release
  pipeline built, smoke-tested, attested and pushed ([docker.md](docker.md)), with the command
  above. Public mode's guards (`Host` check, per-visitor limits, POST origin check, no framing)
  are those of [Security model](#security-model), with the Render host name and no frame
  ancestor. Render's edge is Cloudflare, and how it buffers slow clients is not documented.
- The workflow has `permissions: contents: read`; its only action is `actions/checkout`, pinned
  to the same commit as in `ci.yml`, with `persist-credentials: false`. It holds no Render API
  key. The job runs in the environment `render`, whose deployment rules decide which refs may
  use the hook.
- The hook is in the environment of the trigger step only. The helper checks it before use
  (`https://api.render.com/deploy/srv-...?key=...`, nothing else), so a mistyped secret is never
  sent elsewhere, and prints neither the hook, its key nor Render's answer. No `${{ }}`
  expression is pasted into a script, and no step traces its commands.
- Someone who learns the hook can deploy any tag or digest of `ghcr.io/donquaan/cca` to the
  service, for example an old version, but no other image (Render rejects an `imgURL` for
  another image). Regenerate it if in doubt, and update the secret.
- `docker/tests/test_deploy_render.py` checks the Blueprint's command against its contract, the
  workflow's guards and the helper, and runs the workflow's steps under bash with a scripted
  network; `docker/tests/test_constrained.py` checks the constrained run against a stand-in
  server.

### Render troubleshooting

| Message or symptom | Likely cause and fix |
|---|---|
| "the secret RENDER_DEPLOY_HOOK_URL is missing" or "the variable RENDER_URL is missing" | Add them (setup step 6). An environment secret is only visible to runs its deployment rules allow. |
| "the secret RENDER_DEPLOY_HOOK_URL is not a Render deploy hook" | The secret holds something other than the hook URL of the service's Settings (another host, extra parameters, spaces). Copy it again. |
| "the tag vX has no deploy/render/render.yaml" | That version predates the Render deploy: deploy a newer one (or, from `blueprint-check` without a version, wait for a release before syncing). |
| "deploy/render/render.yaml passes --x, which the Blueprint of vX does not" | The command on the run's ref (or on your checkout) has an option that release may not know. For a deploy: pick a release whose Blueprint has it, or put that release's command back on `main` and sync first ([rolling back](#render-redeploying-rolling-back-and-stopping)). Before a sync: wait for the release that ships the command. |
| "there is no tag vX in this checkout" | Fetch the tags (`git fetch --tags`), or the version is not released yet. |
| "ghcr.io/donquaan/cca:X does not exist" | A typo, or the release's image job did not push. |
| "the deploy hook answered HTTP 401" | The hook was regenerated: copy the new one into the secret. |
| "the deploy hook answered HTTP 400" or "HTTP 404" | The service's image is not `ghcr.io/donquaan/cca` (Render refuses an `imgURL` for another image), or the service no longer exists. |
| "the deploy hook answered HTTP 409" | The service is suspended: free instance hours or bandwidth used up, or suspended by hand. See the dashboard. |
| "answered 421: the service does not accept the host name" | `RENDER_URL`'s host is not an `--allowed-host` of the running command: fix the line (setup step 4) and sync. |
| "the engine of version X failed" | The new container started but its Stockfish did not; see the service's *Logs*. |
| "did not serve version X ... within 600s" | The deploy failed or took longer. See the service's *Events* and *Logs*: health checks answered 421 mean a wrong `--allowed-host` (setup step 4). If the service becomes live later, nothing needs to be done. |
| The first deploy's log shows `play` could not be executed (for example "executable file not found") | Render replaced the image's `ENTRYPOINT` instead of appending to it. Start the command with `/usr/bin/tini -- cca play ...` (in `render.yaml`, then sync) and tell the maintainers, so that the Blueprint and its test change. |
| The log shows `cca: error: unrecognized arguments` | The service's command, from its last Blueprint sync, has an option the running image's `cca play` does not know: a sync while `main`'s command was ahead of its image tag (`blueprint-check` catches that), or a deploy of an older version started outside the workflow. Sync a Blueprint whose `blueprint-check` passes, or deploy a release that knows the options. |
| An `Image Pull Failed` event ([deploying-an-image](https://render.com/docs/deploying-an-image)) | The image is not in the registry: for example a Blueprint sync to a release tag that `release.yml` has not pushed yet (keep Auto Sync off), or a deleted version. |
| The deploy fails with a port error | Render did not find the port: check that `PORT` is `8765` and the command has `--port 8765`. |
| Visitors all hit the same limits | The client address is not calibrated yet (`--trusted-proxies 0`). |

### Render sources and open questions

Render's documentation, read on 2026-09-29 through its Markdown pages and the corpus
`https://render.com/docs/llms-full.txt` (generated 2026-09-26):
[free](https://render.com/docs/free),
[web-services](https://render.com/docs/web-services),
[deploying-an-image](https://render.com/docs/deploying-an-image),
[deploy-hooks](https://render.com/docs/deploy-hooks),
[blueprint-spec](https://render.com/docs/blueprint-spec),
[infrastructure-as-code](https://render.com/docs/infrastructure-as-code),
[health-checks](https://render.com/docs/health-checks),
[environment-variables](https://render.com/docs/environment-variables),
[scaling](https://render.com/docs/scaling),
[outbound-bandwidth](https://render.com/docs/outbound-bandwidth),
[login-settings](https://render.com/docs/login-settings),
[ddos-protection](https://render.com/docs/ddos-protection),
[configure-cloudflare-dns](https://render.com/docs/configure-cloudflare-dns),
[troubleshooting-deploys](https://render.com/docs/troubleshooting-deploys), the
[Acceptable Use Policy](https://render.com/acceptable-use) and the article on
[free tiers](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026).
`render.yaml` was also checked offline against Render's published schema,
`https://render.com/schema/render.yaml.json` (downloaded 2026-09-29): valid, and eight broken
variants (plan, region, runtime, a missing image URL, a list as command, an unknown key,
`numInstances: 0`, an environment variable without a key) were all rejected.

UNVERIFIED:

- Whether the Docker command keeps the image's `ENTRYPOINT`, and whether Render splits it on
  spaces or runs it through a shell. The plain words make the second question moot; the first
  is settled by the first deploy (see [Render troubleshooting](#render-troubleshooting)).
- Whether Render sets `PORT` in a Docker service without being told (the Blueprint sets it).
- What happens when a free service uses more than 512 MB (restart or OOM kill), and the
  demo's actual peak: the constrained run measures it on GitHub. Whether Render's MB is
  10^6 or 2^20 bytes: the constrained run's 488 MiB is under both.
- That a deploy through the hook keeps the service's Docker command. The docs say `imgURL`
  replaces the tag or digest of the service's settings for that deploy and mention no command;
  `blueprint-check` and the rollback steps assume the command stays.
- Whether `/healthz` answers within five seconds at 0.1 CPU while Stockfish searches: the
  constrained run times it.
- Whether Render's health checks count as traffic for the 15-minute spin-down (every free
  service gets health checks, and free services do spin down, so presumably not).
- How the `onrender.com` subdomain is chosen, and whether the loading page is also shown to
  API requests during a spin-up.
- The number of `X-Forwarded-For` entries Render appends (three, per the evidence above), and
  whether it replaces client-sent `X-Forwarded-For`, `True-Client-IP` or `CF-Connecting-IP`
  values.
- The right encoding of a tag in `imgURL` (the docs' example `docker.io%2Flibrary%2Fnginx%401.26`
  uses the digest separator `@` for the tag `1.26`) and the status of a bad `imgURL` (404 or
  400): the workflow deploys by digest, where `@` is right.
- Whether a failed image pull (a tag not pushed yet) leaves the running instance serving, and
  which image a restarted instance pulls after a deploy by `imgURL`: the service's tag (the
  Blueprint's) or the deployed digest. The two agree once the Blueprint's tag is the release
  that was deployed.
- Whether the Maia-2 image fits in 512 MB.
- The dashboard's labels for suspending and deleting a service.

## Hugging Face Spaces (paid alternative)

This part keeps the Space deploy for when the demo should have more CPU than Render's free
plan gives. Creating a Docker Space needs a paid Hugging Face plan (see
[One-time setup](#one-time-setup-owner) and [Costs](#costs)); the Space's hardware, CPU Basic, is
then free.

Here the demo is a [Docker Space](https://huggingface.co/docs/hub/spaces-sdks-docker) on
Hugging Face that runs a released container image, `ghcr.io/donquaan/cca:<version>`, pinned by
digest, with `cca play` in public mode. The Space's repository holds two files, both rendered
from this repository:

| File in this repository | Pushed to the Space as | What it is |
|---|---|---|
| [`deploy/huggingface/Dockerfile`](../deploy/huggingface/Dockerfile) | `Dockerfile` | A template. `FROM` the release image pinned by digest, then `USER 1000:1000`, `WORKDIR /`, `ENV HOME=/tmp` and the public-mode `CMD`. |
| [`deploy/huggingface/README.md`](../deploy/huggingface/README.md) | `README.md` | The Space card: YAML metadata (`sdk: docker`, `app_port: 8765`, `license: apache-2.0`...) and what visitors should know. |
| [`deploy/huggingface/space.py`](../deploy/huggingface/space.py) | (not pushed) | The helper that validates, resolves, renders and checks. Standard library only. |
| [`.github/workflows/deploy-space.yml`](../.github/workflows/deploy-space.yml) | (not pushed) | The deploy workflow. |

A deploy renders the two templates as the deployed version's tag (`v<version>`) holds them, so
a Space always runs the command line that its image understands. The Space builds nothing from
source: its image is the release image that the release pipeline already built, smoke-tested,
attested and pushed ([docker.md](docker.md)).

> **Status (2026-09-29).** Nothing is deployed yet. The first deploy needs the owner's one-time
> steps below, including a paid Hugging Face plan, and a release whose `cca play` has public
> mode (`--public`, `--allowed-host`, `--trusted-proxies`, `--frame-ancestor`) and whose tag
> holds `deploy/huggingface/`. v0.1.0 has neither; v0.2.0 is the first release that has both.

Contents of this part: [How a deploy works](#how-a-deploy-works) ·
[One-time setup](#one-time-setup-owner) · [Deploying a release](#deploying-a-release) ·
[Redeploying and restarting](#redeploying-and-restarting) · [Rolling back](#rolling-back) ·
[Pausing, stopping and deleting](#pausing-stopping-and-deleting) · [Costs](#costs) ·
[Security model](#security-model) · [Trying the Space image locally](#trying-the-space-image-locally) ·
[Manual fallback](#manual-fallback-web-ui) · [Troubleshooting](#troubleshooting) ·
[Sources and open questions](#sources-and-open-questions)

### How a deploy works

`deploy-space.yml` has one job, which runs in the GitHub environment `huggingface-space`. Its
steps run in this order. A failing step stops the run with a GitHub error annotation: the
helper writes its errors to stderr, which the runner reads for workflow commands just as it
reads stdout ([ScriptHandler.cs](https://github.com/actions/runner/blob/main/src/Runner.Worker/Handlers/ScriptHandler.cs),
[OutputManager.cs](https://github.com/actions/runner/blob/main/src/Runner.Worker/Handlers/OutputManager.cs)),
while a step's `x=$(space.py ...)` keeps only stdout. The message therefore shows in the log
and on the run's summary page.

1. **Check the settings.** The secret `HF_TOKEN` must be set, the variable `HF_SPACE` must be
   `OWNER/NAME`, and the optional variable `HF_USERNAME` must be a user name if it is set. This
   step only learns whether the token is set (`true` or `false`); it never holds the token.
2. **Version.** The release's tag on a `release` event, otherwise the `version` input. `latest`
   resolves to the tag of the latest GitHub release. `v0.2.0` and `0.2.0` both become the image
   tag `0.2.0`, and anything that is not a normalised PEP 440 version is refused.
3. **Digest.** The helper pulls the manifest of `ghcr.io/donquaan/cca:<version>` anonymously and
   computes its SHA-256 itself. A `Docker-Content-Digest` header that disagrees stops the run,
   and so does a missing tag. Rate limits, server errors and network errors are retried (three
   tries, 10 seconds apart).
4. **Host name.** The Space's own host, `<subdomain>.hf.space`, read from the Hub's public API
   (`GET https://huggingface.co/api/spaces/OWNER/NAME`, field `host`; retried like the digest).
   `cca play --public` must be told this name with `--allowed-host`, or its Host guard answers
   the Space's visitors with 421.
5. **Render.** The checkout fetches every tag (`fetch-tags: true`), and the step reads
   `deploy/huggingface/Dockerfile` and `README.md` from the tag `v<version>`. A tag without them
   (a version released before this deploy existed) stops the run before anything is pushed. A
   manual run can choose `templates: checkout` to use the files of the branch it runs on
   instead (see [Rolling back](#rolling-back)). `space.py render` then fills the placeholders:
   version, digest and host in the Dockerfile, version in the card's links. The rendered
   Dockerfile is shown in the run's summary.
6. **Push.** The step clones the Space's git repository, replaces everything except `.git` and
   the Hub's `.gitattributes` with the two rendered files, and commits and pushes to `main` if
   anything changed. "Each time a new commit is pushed, the Space will automatically rebuild
   and restart" ([spaces-overview](https://huggingface.co/docs/hub/spaces-overview)). The step
   outputs the commit the Space must end up running: the one it pushed, or the unchanged HEAD.
7. **Wait.** For up to 15 minutes, every 15 seconds, the step reads the Space's runtime
   (`GET https://huggingface.co/api/spaces/OWNER/NAME/runtime`) and `https://<host>/healthz`,
   and prints both. It succeeds only when the runtime reports stage `RUNNING` with a container
   built from that commit (field `sha`), **and** `/healthz` answers `status: ok`, `ready: true`
   and the deployed `version` (the image's own HEALTHCHECK condition plus the version). While
   a new commit builds and starts, the previous container keeps serving, possibly with the
   same version, and the runtime shows the stages `RUNNING_BUILDING` and `RUNNING_APP_STARTING`
   with the previous commit (observed, see [Sources](#sources-and-open-questions)); the commit
   is what tells the two containers apart. The step fails early when:
   - the runtime reaches `BUILD_ERROR`, `RUNTIME_ERROR`, `CONFIG_ERROR`, `NO_APP_FILE`,
     `PAUSED` or `DELETING` after a build or start was seen, or stays at one of them for 120
     seconds (a stage left over from before the push gets that long to clear). The message
     carries the Hub's `errorMessage`;
   - the container built from the commit answers `/healthz` with 503 and `status: error`: its
     engine failed. The message carries the app's `error` text.

What the Space runs (after the image's inherited `ENTRYPOINT ["/usr/bin/tini", "--", "cca"]`):

```text
play --public --allowed-host <subdomain>.hf.space --host 0.0.0.0 --port 8765 --no-browser
     --human qre --trusted-proxies 1 --frame-ancestor https://huggingface.co --max-sessions 48
```

- **uid 1000.** Hugging Face says "The container runs with user ID 1000"
  ([spaces-sdks-docker](https://huggingface.co/docs/hub/spaces-sdks-docker)). The release image
  runs as uid 10001, whose home `/home/cca` is mode 0700. The Space's Dockerfile therefore sets
  `USER 1000:1000` itself, so a local `docker run` behaves like the Space, and starts in `/`.
  All the other runtime paths are readable by any uid. This was checked statically on the 0.1.0
  layers (every layer replayed from the registry, whiteouts included: 5538 paths): under
  `/opt/cca`, `/usr/local`, `/usr/bin` (tini), `/usr/lib`, `/lib` and `/etc`, the only paths
  that "other" cannot read are `/etc/shadow`, `/etc/gshadow` (and their backups),
  `/etc/.pwd.lock`, `/etc/security/opasswd` and `/etc/ssl/private`. No runtime path belongs to
  uid 10001, and `/tmp` is mode 1777. The release pipeline smoke-tests the image as uid 10001
  only; the Space's combination (uid 1000 without a passwd entry, `WORKDIR /`, public mode) has
  not been run yet. [Trying the Space image locally](#trying-the-space-image-locally) shows how.
- **Nothing is written.** Games live in memory. `HOME=/tmp` is set only in case a library
  looks for a home directory.
- **QRE only.** The Maia-2 image is not used: its 267 MB checkpoint would be downloaded again
  after every restart, because Space disk is not persistent
  ([spaces-overview](https://huggingface.co/docs/hub/spaces-overview)). Its `/data/maia2` also
  belongs to uid 10001.

### One-time setup (owner)

These steps need a person: they create an account, pay for a plan and handle a token.

1. **Hugging Face account.** Create one, or choose an organisation. As of 2026-09-28,
   `https://huggingface.co/api/users/DonQuaan/overview` answered 404, so no account with that
   name existed then.
2. **Plan.** "Gradio and Docker Spaces run on compute and require a paid plan to create: PRO
   for personal accounts, Team or Enterprise for organizations"
   ([spaces-overview](https://huggingface.co/docs/hub/spaces-overview)). PRO costs $9 per month
   and includes "Host ZeroGPU, Gradio & Docker Spaces"
   ([pricing](https://huggingface.co/pricing), read 2026-09-28). The hardware itself (CPU Basic)
   is free; see [Costs](#costs).
3. **Create the Space** at <https://huggingface.co/new-space>:
   - Owner and name. The name gives `HF_SPACE`, for example `DonQuaan/CCA`.
   - SDK: **Docker**, with the blank template. The template's exact label is UNVERIFIED.
   - Hardware: **CPU basic**.
   - Visibility: **Public**. The host lookup and the wait are anonymous, so a private Space
     stops the run at step 4 ("create it first, and make it public"). For a Space that did not
     exist, the Hub answered 401 to an anonymous request (observed 2026-09-29).
   - Licence: optional. The card that the workflow pushes sets `license: apache-2.0`.

   The Space may stay empty or keep the files the Hub created: the first deploy replaces
   them.
4. **Token.** At <https://huggingface.co/settings/tokens>, create a **fine-grained** token with
   write access to this one Space. Hugging Face recommends fine-grained tokens for production
   use, and a token can be used "in place of a password" with git
   ([security-tokens](https://huggingface.co/docs/hub/security-tokens)). The labels of the
   fine-grained permission checkboxes are UNVERIFIED.
5. **GitHub environment** `huggingface-space`, in *Settings > Environments* of `DonQuaan/CCA`
   ([manage-environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)):
   - **Deployment branches and tags:** *Selected branches and tags*, with the branch `main` and
     the tag pattern `v*`. "The deployment branch or tag rule is matched against the
     `GITHUB_REF` of the workflow run"
     ([deployments-and-environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)):
     a manual run from `main`, a release event (ref `refs/tags/v...`) and a call from
     `release.yml` on a tag pass; a run from any other branch cannot use the token.
   - Optionally, a **required reviewer**: every deploy then waits for an approval.
   - **Environment secret** `HF_TOKEN`: the token from step 4. "Secrets stored in an
     environment are only available to workflow jobs that reference the environment. If the
     environment requires approval, a job cannot access environment secrets until one of the
     required reviewers approves it" ([deployments-and-environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)).
     Do not also store it as a repository secret: a repository secret reaches every workflow
     run of the repository that references it, whatever branch it runs on.
   - **Variables** (environment or repository variables, both work): `HF_SPACE`, the Space as
     `OWNER/NAME`; `HF_USERNAME` (optional), your Hugging Face user name when the Space belongs
     to an organisation. By default git sends the owner part of `HF_SPACE` as the user name.
     Whether the Hub checks this user name when a token is the password is UNVERIFIED.

   Environments and their secrets and rules "are available in public repositories for all
   current GitHub plans" ([manage-environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)).
   If the environment does not exist, the first run creates it without any rule ("Running a
   workflow that references an environment that does not exist will create an environment with
   the referenced name", same page), and that run stops at the settings check for want of the
   secret.
6. **Deploy.** *Actions > Deploy Space > Run workflow* from `main`, with `version` set to
   `latest` or to a version such as `0.2.0`.

### Deploying a release

- **Manual run.** *Actions > Deploy Space > Run workflow*. Use this after every automated
  release (see the next point) and for any other version.
- **`release: published` does not fire after an automated release.** `release.yml` publishes
  the GitHub Release with `GITHUB_TOKEN`, and "events triggered by the `GITHUB_TOKEN` [...] will
  not create a new workflow run" (except `workflow_dispatch` and `repository_dispatch`;
  [GitHub docs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)).
  The trigger is still useful for a release that a person publishes, for example from a draft.
  The release event never deploys a pre-release.
- **Automatic deploys** need a job in `release.yml` after `publish`, which calls this workflow
  (it also accepts `workflow_call`). A called workflow uses the caller repository's variables
  ([variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables)).
  Because the deploy job names its environment, it reads the environment's `HF_TOKEN` itself:
  "If you include `environment` in the reusable workflow at the job level, the environment
  secret will be used, and not the secret passed from the caller workflow"
  ([reuse-workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)).
  A sketch for the release owner:

  ```yaml
  deploy-space:
    name: public demo
    needs: publish
    # the same pre-release rule as the publish job
    if: ${{ !(contains(github.ref_name, 'a') || contains(github.ref_name, 'b') || contains(github.ref_name, 'rc')) }}
    uses: ./.github/workflows/deploy-space.yml
    with:
      version: ${{ github.ref_name }}
    secrets: inherit
  ```

### Redeploying and restarting

- **Same version, same Space files.** Deploying the version that the Space already runs, with
  the same templates, changes no file: the push step prints "nothing to push" and outputs the
  Space's unchanged HEAD, and the wait step checks that the running container is built from
  that HEAD and healthy. If the previous deploy is still building, or failed, the wait sees it.
- **Same version, changed Space files** (`templates: checkout` with edited templates, or a
  Space whose files someone changed by hand): the push step commits the difference, the Space
  rebuilds, and the wait step holds out for a container built from the new commit. The old
  container's answers, same version or not, do not count.
- For a sleeping Space, the Hub says "Anyone visiting your Space will restart it
  automatically" ([spaces-gpus](https://huggingface.co/docs/hub/spaces-gpus)); whether the wait
  step's plain GET of `/healthz` counts as a visit is UNVERIFIED.
- To rebuild the same files from scratch, restart the Space from its settings page, or call
  `HfApi().restart_space("OWNER/NAME", factory_reboot=True)` from `huggingface_hub`
  ([manage-spaces](https://huggingface.co/docs/huggingface_hub/guides/manage-spaces)). The
  label of the factory-rebuild button is UNVERIFIED.
- "Any change in your Space configuration (secrets or hardware) will trigger a restart of your
  app" ([manage-spaces](https://huggingface.co/docs/huggingface_hub/guides/manage-spaces)).

### Rolling back

Run the workflow with the older version. It renders that version's own templates (from its tag)
with its digest and pushes them; the Space's git history keeps every deploy as one commit
("Deploy CCA <version>").

- The oldest version you can roll back to is the first release whose tag holds
  `deploy/huggingface/` (and whose `cca play` has public mode). For an older version the render
  step stops with "the tag v... has no deploy/huggingface/Dockerfile", before anything is pushed,
  so the running Space is untouched.
- `templates: checkout` (an input of manual runs, which a calling workflow can pass too) deploys
  a version with the templates of the ref the run checked out, for example a fix to the card
  that should not wait for a release. That ref's `CMD` must then suit the version's image: a
  flag that the image does not know makes the container exit at start, and the wait step fails
  with the Hub's error message.

### Pausing, stopping and deleting

- **Sleep.** On CPU Basic, a Space "will go to sleep if inactive for more than a set time
  (currently, 48 hours). Anyone visiting your Space will restart it automatically"
  ([spaces-gpus](https://huggingface.co/docs/hub/spaces-gpus)). The first visitor after that
  waits for a cold start. How long a cold start takes is UNVERIFIED.
- **Pause.** "You can `pause` a Space from the repo settings [...] only the owner of a paused
  Space can restart it. Paused time is not billed"
  ([spaces-gpus](https://huggingface.co/docs/hub/spaces-gpus#pause)). From Python:
  `HfApi().pause_space(...)` and `HfApi().restart_space(...)`
  ([manage-spaces](https://huggingface.co/docs/huggingface_hub/guides/manage-spaces)). A deploy
  to a paused Space fails after 120 seconds if the Space stays paused.
- **Stop deploying.** Disable the workflow under *Actions*, or delete the variable `HF_SPACE`:
  every run then stops at the settings check.
- **Delete.** Delete the Space from its settings page. This cannot be undone, and the exact
  flow is UNVERIFIED. Afterwards, revoke the token on Hugging Face, and delete the secret
  `HF_TOKEN` and the variable `HF_SPACE` on GitHub.

### Costs

| Item | Cost | Source |
|---|---|---|
| Creating a Docker Space | Needs a paid plan: PRO ($9 per month) for a personal account, Team or Enterprise for an organisation | [spaces-overview](https://huggingface.co/docs/hub/spaces-overview), [pricing](https://huggingface.co/pricing) |
| CPU Basic hardware: 2 vCPU, 16 GB RAM, 50 GB disk (not persistent) | Free ("no hourly cost") | [spaces-overview](https://huggingface.co/docs/hub/spaces-overview), [spaces-gpus](https://huggingface.co/docs/hub/spaces-gpus) |
| CPU Upgrade: 8 vCPU, 32 GB (not needed) | $0.03 per hour | [spaces-gpus](https://huggingface.co/docs/hub/spaces-gpus) |
| A custom sleep time or never sleeping | Needs paid hardware | [spaces-gpus](https://huggingface.co/docs/hub/spaces-gpus) |
| Custom domain | PRO or Team only | [spaces-custom-domain](https://huggingface.co/docs/hub/spaces-custom-domain) |

Every visitor shares the 2 vCPU. The Hub's rate limits cover the Hub API, resolvers and pages,
not traffic to the app ([rate-limits](https://huggingface.co/docs/hub/rate-limits)). The
per-visitor and global limits of `cca play --public` are the only protection against a busy
Space (see the residual risks below).

### Security model

`cca play` has no authentication and no TLS of its own. The Hub's edge terminates HTTPS in
front of it. Public mode's flags, defaults, limits and guards are described in
[simulator.md: Public mode](simulator.md#public-mode), including
[threads and request bodies](simulator.md#threads-and-request-bodies) and its
[residual risks](simulator.md#residual-risks); the guards of a local run are in
[simulator.md: Security model](simulator.md#security-model). The deploy relies on these parts
of public mode:

| Guard | In the Space |
|---|---|
| `Host` check (DNS-rebinding guard) | Answers `<subdomain>.hf.space` (`--allowed-host`), IP addresses (the HEALTHCHECK uses `127.0.0.1`), `localhost` and the container's own host name (a non-loopback bind also answers `socket.gethostname()`); anything else gets 421. Renaming the Space changes the host: redeploy. |
| Framing | `Content-Security-Policy: frame-ancestors https://huggingface.co` and no `X-Frame-Options`. The Space page embeds the app in an iframe whose `src` is `https://<subdomain>.hf.space/?__theme=system` (observed). |
| Client address | `--trusted-proxies 1`: the last `X-Forwarded-For` entry. The edge was observed to keep any client-supplied entries and to append exactly one, the client's address. `X-Real-IP`, `Forwarded` and `X-Forwarded-Host` pass through from the client unchanged, so they are never read. |
| Limits | Per-visitor games and decisions, a bounded queue and idle expiry (public-mode defaults), `--max-sessions 48` games in memory. |
| POST origin check | Still holds. The edge answers every `OPTIONS` preflight itself, reflecting the requested method and headers, but the real POST reaches the app with its `Origin` and `Sec-Fetch-Site`. |

Observed edge behaviour to keep in mind (probed on a public httpbin Space on 2026-09-28; not
documented, so it may change):

- The edge adds `access-control-allow-origin: <the request's Origin>` to responses. Any web
  site can therefore read the app's GET JSON (`/api/info`, a game whose id it knows) without
  credentials. CORS is not a defence here.
- Game ids are the only secret of a game; they are random and never listed.

Residual risks of public mode, which apply to the Space's 48 game slots and 64 connections as
to any public instance (the fixes belong to `cca play`, not to this deploy):

- **Game slots.** A visitor below its quota of 3 games keeps an unfinished game only while
  playing it: on a full table, a game in which nothing was played for 15 minutes on the move of
  a player who has moved in it (5 minutes before the player's first move or while CCA's move
  waits) goes to a newcomer. Someone with many addresses who keeps playing every game (24
  addresses with 2 games each fill 48 slots), or who takes each slot as soon as it may be
  reclaimed, can still keep new visitors out: see
  [simulator.md: Game slots](simulator.md#game-slots) and
  [Residual risks](simulator.md#residual-risks).
- **Connections.** Public mode bounds its threads (`--max-connections`, 64 by default, with
  request heads and bodies due within 10 seconds); `--max-connections-per-peer` is not for the
  Space (every connection comes from the edge, and `cca play` refuses it with
  `--trusted-proxies 1`): see
  [simulator.md: Threads and request bodies](simulator.md#threads-and-request-bodies). How much
  the Hub's edge buffers slow clients is UNVERIFIED.
- If the Space is overwhelmed, pause it (see above); nothing is lost but games in progress.

The deploy pipeline:

- `permissions: contents: read`; the only action is `actions/checkout`, pinned to the same
  commit as in `ci.yml`, with `persist-credentials: false`.
- The job runs in the environment `huggingface-space`, whose deployment rules decide which
  refs may use its secret `HF_TOKEN`.
- The token is in the environment of the push step only. git reads it from a credential
  helper that takes it from that environment, so it never appears on a command line, in a URL,
  in `.git/config` or in the log. The settings check sees only whether it is set.
- No `${{ }}` expression is pasted into a script: inputs, the release tag and the variables
  reach the shell as environment variables, and the helper validates the version, the digest,
  the host name, the Space id and the commit before they are used. No step traces its
  commands (`set -x`).
- The image is pinned by a digest that the helper computes from the manifest bytes. The
  helper's HTTPS client follows no redirect and reads at most 4 MiB per answer.
- The Space adds nothing to the release image except `USER`, `WORKDIR`, `ENV HOME` and `CMD`;
  `docker/tests/test_deploy_space.py` fails if another instruction appears. The same file runs
  the workflow's steps under bash against a local git server that demands the user name and
  token, with a scripted network.
- A token-free alternative: Hugging Face's
  [Trusted Publishers](https://huggingface.co/docs/hub/trusted-publishers) exchange GitHub's
  OIDC token for a short-lived token scoped to one repository. It would replace the stored
  `HF_TOKEN` and needs `id-token: write` and the `hf` CLI; it is not used yet.

### Trying the Space image locally

The rendered files build the Space's exact image, on any machine with Docker. This has not been
run yet (the machine this was written on had no working Docker):

```bash
version=0.2.0                                     # a release with public mode (planned)
digest=$(python deploy/huggingface/space.py digest "$version")
python deploy/huggingface/space.py render "$version" "$digest" example.hf.space space-local
docker build -t cca-space space-local
docker run --rm -p 127.0.0.1:8765:8765 cca-space  # runs as uid 1000, as on the Space
```

Then open <http://127.0.0.1:8765/> (an IP address, which the Host guard accepts) and check
<http://127.0.0.1:8765/healthz>. `example.hf.space` only fills `--allowed-host`.

### Manual fallback (web UI)

If GitHub Actions is unavailable, render the same two files locally. The commands only read
from the network. Use the templates of the version's tag, as the workflow does:

```bash
version=0.2.0                                     # a release with public mode (planned)
git fetch --tags
mkdir space-templates
git show "v$version:deploy/huggingface/Dockerfile" > space-templates/Dockerfile
git show "v$version:deploy/huggingface/README.md" > space-templates/README.md
digest=$(python deploy/huggingface/space.py digest "$version")
host=$(python deploy/huggingface/space.py host OWNER/NAME)
python deploy/huggingface/space.py render --source space-templates "$version" "$digest" "$host" space-files
```

Then, on the Space page, open **Files**, choose **Add file** and upload `space-files/Dockerfile`
and `space-files/README.md`
([repositories-getting-started](https://huggingface.co/docs/hub/repositories-getting-started)).
Delete any other file except `.gitattributes`. Finally, wait for the Space to run the new commit
and be healthy. Without `--commit`, the helper expects the Space's HEAD when it starts, so run
it after the last upload or deletion:

```bash
python deploy/huggingface/space.py wait OWNER/NAME "$version"
```

### Troubleshooting

| Message or symptom | Likely cause and fix |
|---|---|
| "the secret HF_TOKEN is missing" or "the variable HF_SPACE is missing" | Add them (step 5 of the setup). An environment secret is only visible to runs its deployment rules allow. |
| A run waits for, or is refused by, the environment | Deployment rules of `huggingface-space`: run from `main` or a `v*` tag, and approve it if a reviewer is required. |
| "not a release version" | The `version` input, or the release's tag, is not a version such as `0.1.1` or `v0.1.1`. |
| "ghcr.io/donquaan/cca:X does not exist" | The version has no image: a typo, or the release's image job did not push. |
| "no digest for ... after 3 tries" or "no host name ... after 3 tries" | ghcr.io or the Hub API kept failing (rate limit, outage). Run again later. |
| "the Hub does not show the Space ... (HTTP 401/404)" | The Space does not exist, is private, or `HF_SPACE` is misspelt. |
| "the tag vX has no deploy/huggingface/Dockerfile" | That version predates the Space deploy and cannot run it: deploy a newer one. |
| `git push` is rejected with 401 or 403 | The token has no write access to this Space, or has expired. For an organisation's Space, set `HF_USERNAME`. |
| "the Space ... is at stage BUILD_ERROR" or "RUNTIME_ERROR" | See the Space's logs; the message carries the Hub's `errorMessage`. An image without public mode prints an argument error, and so does `--public` without `--allowed-host`. |
| "the Space ... is at stage PAUSED (for 120s)" | Restart the Space from its settings (owner only), then run the workflow again. |
| "the new container's engine failed" | The container built from the pushed commit started, but its Stockfish did not. See the Space's logs. |
| "did not serve version X from commit Y ... within 900s" | The build or start took longer than 15 minutes, or the Space still runs another commit (the log line shows `running=` and `want=`). Look at the Space's logs; if it becomes healthy later, run the workflow again (nothing is pushed a second time). |
| "the runtime API reports no commit" | The Hub's runtime answer lost its `sha` field (it is not documented). Check the Space by hand; the wait cannot confirm the new build. |
| The Space answers 421 | The Space was renamed or moved, so its host changed: redeploy. |
| The Space page shows an empty frame | `--frame-ancestor https://huggingface.co` is missing, or the release predates it. |
| "Launch timed out, workload was not healthy after 30 min" | The Hub's start-up limit (30 minutes by default, `startup_duration_timeout` in the card; [spaces-config-reference](https://huggingface.co/docs/hub/spaces-config-reference)). The error text was observed on another Space. |

### Sources and open questions

Documentation read on 2026-09-28 and 2026-09-29:
[spaces-overview](https://huggingface.co/docs/hub/spaces-overview),
[spaces-sdks-docker](https://huggingface.co/docs/hub/spaces-sdks-docker),
[spaces-config-reference](https://huggingface.co/docs/hub/spaces-config-reference),
[spaces-gpus](https://huggingface.co/docs/hub/spaces-gpus),
[spaces-github-actions](https://huggingface.co/docs/hub/spaces-github-actions),
[security-tokens](https://huggingface.co/docs/hub/security-tokens),
[rate-limits](https://huggingface.co/docs/hub/rate-limits),
[manage-spaces](https://huggingface.co/docs/huggingface_hub/guides/manage-spaces),
[pricing](https://huggingface.co/pricing), and GitHub's pages on
[triggering workflows](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables),
[reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows),
[managing environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
and [deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

Source code read on 2026-09-29:

- The Space stages (`BUILDING`, `RUNNING_BUILDING`, `APP_STARTING`, `RUNNING_APP_STARTING`,
  `RUNNING`, `BUILD_ERROR`, `RUNTIME_ERROR`, `CONFIG_ERROR`, `NO_APP_FILE`, `STOPPED`, `PAUSED`,
  `DELETING`): `SpaceStage` in huggingface_hub's
  [`_space_api.py`](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/_space_api.py).
  The library's own `wait_for_space` stops at the first stage that is not intermediate
  ([`hf_api.py`](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/hf_api.py)),
  so right after a push it can still see the previous `RUNNING`; the wait step also compares
  the commit.
- `actions/checkout` at the pinned commit adds the refspec `+refs/tags/*:refs/tags/*` when
  `fetch-tags` is true
  ([`ref-helper.ts`](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/src/ref-helper.ts)).
- The runner feeds a script's stdout and stderr to two `OutputManager`s that share one command
  manager, and each line goes through `TryProcessCommand` (links under
  [How a deploy works](#how-a-deploy-works)).

Observed, not documented (2026-09-28 and 2026-09-29):

- The Hub API returns `host` (for example `https://baw-appie-httpbinorg.hf.space`), `subdomain`,
  `sha` (the repository's HEAD) and `runtime.stage`; `/api/spaces/OWNER/NAME/runtime` returns
  `stage`, `sha` and `errorMessage`, among others.
- What the runtime's `sha` means, from the 80 most recently modified public Spaces read on
  2026-09-29 (repository `sha` against runtime `sha`): all 42 Docker, Gradio and Streamlit
  Spaces at `RUNNING` showed the repository's HEAD; all 6 at `RUNNING_BUILDING` or
  `RUNNING_APP_STARTING` showed another commit, the one still serving; Spaces at `BUILDING`,
  `CONFIG_ERROR`, `PAUSED` and `RUNTIME_ERROR` (9) and static Spaces (23) showed none.
- The registry serves `ghcr.io/donquaan/cca:0.2.0` anonymously as one OCI manifest,
  `sha256:7f79355e7b13d120d0678327081543044640b1ca2defc1c448dbf918d0c8d5d0`.
- The `X-Forwarded-For`, CORS and preflight behaviour of the edge described above.
- `♟️` is used as the `emoji` of several public Spaces, and no public Space seen had a
  `short_description` longer than 60 characters. The card stays within 60.

UNVERIFIED:

- Whether the Hub really forces uid 1000 on an image that sets another user. The Space sets
  1000 itself, so both cases behave the same.
- Whether the Space image runs as uid 1000 without a passwd entry: checked statically only.
- Whether the Hub honours the image's HEALTHCHECK. The wait step and the start-up limit do not
  depend on it.
- That the runtime's `sha` keeps the meaning observed above; if the field disappears, the wait
  step fails safe (it never reports an unconfirmed deploy as done).
- Cold-start and build durations, and the build time limit.
- The stage a sleeping CPU Basic Space reports.
- Whether the Hub checks the git user name when a token is the password.
- Whether a plain GET of `/healthz` wakes a sleeping Space.
- The labels of the web UI (blank template, token permissions, factory rebuild, delete).
- Whether the Hub rejects a push whose card metadata is invalid.
