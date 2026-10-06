# Cutting a Mine Monitor release

A release is a git tag `vMAJOR.MINOR.PATCH`. Pushing it runs `.github/workflows/release.yml`,
which publishes the images and the bundles that `deploy/INSTALL.md` installs. Nothing is built
on the mine's box.

## 1. Prepare

1. Set the version in both `pyproject.toml` (`[project] version`) and
   `src/minemonitor/__init__.py` (`__version__`), then run `uv lock` so `uv.lock` agrees. The
   workflow refuses a tag that differs from either, and a test keeps the two equal.
2. Merge to `main` with CI green. CI's `package` job already builds bundles from the change and
   installs, upgrades, rolls back and restores them on real Docker (`release/test-install.sh`).
3. Work through [`RELEASE_CHECKLIST.md`](./RELEASE_CHECKLIST.md).

## 2. Tag

```bash
git switch main && git pull
git tag -a v0.2.0 -m "Mine Monitor 0.2.0"
git push origin v0.2.0
```

## 3. What the workflow does

1. Runs the whole of CI on the tagged commit (`ci.yml`, called as a reusable workflow).
2. Builds the core and vision edge images and pushes them to
   `ghcr.io/yellowcustard/mine-monitor:<version>` and `.../mine-monitor-vision-edge:<version>`,
   labelled with the version, commit and repository. The packages inherit the repository's
   (private) visibility.
3. Pulls every image the compose file names, by digest, for `linux/amd64`, and builds the
   bundles with `release/build-bundle.sh`. Each image is pinned to its registry digest
   (`images.lock`) and tagged on the box as `<repository>:<tag>-<first 12 hex of the digest>`.
   The bundle's `docker-compose.yml` uses only those tags and `pull_policy: never`, so a site runs
   exactly what was tested even if an upstream tag moves (`caddy:2` does, with every Caddy
   release).
4. Installs the online bundle on a clean runner, pulling from the registries, and logs in to the
   dashboard over HTTPS.
5. Creates the GitHub release with the offline bundle, the online bundle, `SHA256SUMS`, and the
   image list in the notes.

## 4. Ship it

Download the offline bundle and `SHA256SUMS`, check them (`sha256sum -c SHA256SUMS
--ignore-missing`), and carry the bundle to site. Install or upgrade per `deploy/INSTALL.md`.
Record the version and the time of the post-upgrade dump in the site log: that dump, not the
older ones, is what restores onto this release when it moved TimescaleDB.

## 5. When something goes wrong

- **The workflow failed before publishing.** Fix on `main`, then move the tag:
  `git tag -fa v0.2.0 -m ... && git push -f origin v0.2.0`. Only do this while no box runs the
  release.
- **A published release is bad.** Do not rebuild it under the same version: boxes that installed
  it keep its images. Cut the next patch version. A site that already upgraded runs
  `sudo mmctl rollback` until then.
- **Rebuild a release unchanged** (for example a lost asset): delete the GitHub release (keep the
  tag) and re-run the workflow's failed jobs, or delete and re-push the tag. The images are
  rebuilt, so their digests change; any box installed from the first build keeps working with the
  images it has.
