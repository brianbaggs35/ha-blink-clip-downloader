# Contribing to Development of Blink Clip Downloader

## We welcome community contributions!

If you have ideas for improvements, bug fixes, or new features,
feel free to open a pull request with the changes and it will
be reviewed by the repository owner and if accepted, it will
be merged into the main branch and a new release will be cut
with the contributions included. Make sure to include what the
contribution does and how it can improve the overall app.

Please note that this app is meant for home assistant OS,
not the containerized version or any other version of Home Assistant.
However, the app may work just fine with the other version types
but the main target of this app is the home assistant operating
system.

## Instructions for contributions

To contribute, fork the repository, make your changes and then
push them up. Then create a pull request and your changes will be
reviewed and approved or rejected.

## Local testing without a Home Assistant OS install

Running `docker run` against the built image directly fails with
`FileNotFoundError: Options file not found: /data/options.json` — under real
HA OS, the Supervisor writes that file and bind-mounts `/data`/`/share`
before starting the container; a bare `docker run` doesn't do either. Use
`blink_clip_downloader/local-test/run.sh` instead: it builds the image and
runs it with local `data`/`share` directories mounted the same way HA would.
First run creates `local-test/data/options.json` from
`local-test/options.json.example` for you to fill in with real Blink
credentials; re-run the script after editing it. Ingress and
Supervisor/Core-API-dependent features (HA notifications, `watch_ha_events`)
aren't available this way, but the web UI (`http://localhost:8099`),
polling/downloads, and AI analysis all work identically. This is a quick
inner-dev-loop check, not a substitute for testing on a real HA OS VM before
opening a PR.
