# Move the kinby coder onto this package

Until this migration, the coder ran the factory built into kinby: `kinby.factory`, with its settings in the routines' `arguments` and its lifecycle in Compose and `docker/update.sh`. After it, the coder runs this package pinned to a commit, keeps every setting in `package.yaml`, and belongs to the hub. A merge here or in kinby rolls out to it through CI.

The migration keeps the coder's identity (manifest id `coder`), its edited prompts and permissions, memory and transcripts, the workspace and Codex volumes, its secrets, and the webhook URL GitHub already calls.

## Why the directory moves

The instance directory moves out of the kinby checkout into the hub directory. The volumes stay where they are. Two reasons:

- Adoption writes the hub's control token into the instance's `.env`, and the hub mounts the kinby checkout read-only.
- kinby no longer tracks `instances/coder`, so a later `git pull` in the checkout would delete the coder's configuration.

## Before you start

- kinby includes adoption with a package ([kinby#272](https://github.com/jorgesolerrr/kinby/pull/272)).
- A commit of this repository is on GitHub, and its CI built the image and passed `kinby package check coder` inside it. Call it `$FACTORY`.
- No factory ticket is labeled `ready-for-agent` in kinby, and none will be until the coder runs from the package.
- `$KINBY` is a kinby commit the box's checkout has fetched. The hub builds from that checkout and never fetches.

`scripts/build-image.sh` builds the image the hub would build, and `python -m kinby_code_factory.migrate` rewrites the configuration. Both are in this repository.

## Rehearse on disposable data

Run the whole procedure once against a copy of the coder, with dummy secrets, a separate hub, a separate Docker network, and new volumes. [`scripts/rehearse-migration.sh`](../scripts/rehearse-migration.sh) does it on the box and removes everything it created when it ends. It never touches the running coder, its volumes, or its Compose project.

It checks that:

- the migration moves each routine argument into `package.yaml` unchanged, and changes no other file;
- a hub adopts the copy with its package recorded, preserving the manifest id and storage;
- an update with `--package coder --package-commit $FACTORY` builds the package image, passes the candidate check, and brings the copy back up ready;
- the updated container lists both routines enabled with no arguments.

## Cut over

Each step says what it stops.

1. **Stop the old updater.** Replace the `docker/update.sh` line in the box's crontab with a fetch, so every commit CI names reaches the hub's source checkout:

   ```
   * * * * * git -C /home/jorge/dev/kinby fetch --quiet origin
   ```

2. **Start the hub in place of the coder's Caddy.** Add `KINBY_HUB_DIR=/home/jorge/kinby-hub` to `~/dev/kinby/.env`, then bring up `compose.hub.yaml`. Its Caddy replaces the old one and keeps the certificate volume. Save the access token the hub prints once to `~/kinby-hub/access-token` with mode 600. From here until step 6, GitHub's deliveries to `/signals/implement-ready-issue` fail. The hourly scan picks them up after the cutover.

3. **Stop the Compose coder.** Wait until `docker compose top coder` shows no `claude` or `codex`, then stop and remove the container. Its volumes stay.

4. **Move and migrate the instance.** Move `~/dev/kinby/instances/coder` to `~/kinby-hub/coder`, build the package image with `scripts/build-image.sh ~/dev/kinby $FACTORY kinby-coder-package`, and run the migration inside it:

   ```sh
   docker run --rm --entrypoint python \
     --mount type=bind,src=$HOME/kinby-hub/coder,dst=/instance \
     kinby-coder-package -m kinby_code_factory.migrate /instance
   ```

   Read the new `package.yaml`. It holds the routines' old arguments, babysitting off as its routine was disabled, review off, and kinby's four checks.

5. **Run the coder from the package image.** Start it on the hub's private network with the same volumes and profile mount it had under Compose:

   ```sh
   docker run --detach --name kinby-coder --network kinby_private \
     --env-file $HOME/kinby-hub/coder/.env \
     --mount type=bind,src=$HOME/kinby-hub/coder,dst=/instance \
     --mount type=volume,src=kinby_coder-workspace,dst=/instance/workspace \
     --mount type=volume,src=kinby_coder-codex,dst=/root/.codex \
     --mount type=bind,src=$HOME/.config/Anthropic,dst=/anthropic-profile,readonly \
     kinby-coder-package serve
   ```

6. **Adopt it.** Write the package selection to `coder-package.json`: id `coder`, distribution `kinby-code-factory`, version `{"url": "https://github.com/jorgesolerrr/kinby-code-factory", "sha": "$FACTORY"}`, and the text of `image/recipe.Dockerfile` as `image_recipe`. Preview, read the findings, then adopt and claim the signal path, so the registered webhook keeps its URL:

   ```sh
   adopt="--connect <hub>/ws /hub/coder kinby-coder --package coder-package.json \
     --relinquished --claim-signals --acknowledge-interrupting-stop"
   kinby hub adopt $adopt --preview
   kinby hub adopt $adopt
   ```

   The container from step 5 runs without a control token, so it cannot drain, and the preview reports a legacy runtime. It started minutes ago and the freeze leaves it no ready issue, so acknowledging the interrupting stop interrupts nothing. Check that no kinby issue carries `ready-for-agent` before you adopt.

7. **Hand updates to CI.** Rotate the hub's update token and set `KINBY_HUB_URL` (`wss://kinby.jorgesolerrr.dev/ws`), `KINBY_HUB_UPDATE_TOKEN` and `KINBY_CODER_INSTANCE_ID` as repository secrets in kinby and in this repository. Rerun this repository's CI on `main`. Its update rebuilds the coder through the hub from `$FACTORY` and proves the rollout path.

8. **Retire the old path in kinby.** Merge the kinby pull request that removes `kinby.factory`, `instances/coder`, `docker/update.sh`, the Compose files and the coding clients in the base image. Its CI then updates the coder to that kinby revision. The package selection travels along.

9. **Confirm.** Label one small kinby ticket `ready-for-agent` and watch the coder implement it and open its pull request from the package.

## If something goes wrong

Before step 3, nothing has changed for the running coder. Undo the hub with `docker compose -f compose.hub.yaml down` and bring the old Caddy back with `docker compose -f compose.yaml -f compose.public.yaml up --detach caddy`.

After step 3, the instance's data is in `~/kinby-hub/coder` and in the two volumes, whatever state the containers are in. To go back to Compose, move the directory back, restore its routine files from kinby's git history, delete `package.yaml` and the `[package]` table from `kinby.toml`, and start the Compose coder again.
