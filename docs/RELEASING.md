# Releasing BattleSpades

This runbook publishes the six portable dedicated-server archives defined by
`BattleSpades.spec` and `.github/workflows/release.yml`.

## Release contract

- Canonical version: root `VERSION` file.
- First prerelease: `0.0.1-alpha.1` / tag `v0.0.1-alpha.1`.
- Targets: Windows, Linux, and macOS on x86_64 and arm64.
- Payload: PyInstaller `onedir` runtime, `config.toml`, maps, prefabs, plugins,
  `LICENSE`, third-party notices, quick start, and `VERSION`.
- Trust: unsigned Windows/macOS binaries; the first macOS alpha is not
  notarized.
- Publication is atomic: all six zips and `SHA256SUMS.txt`, or no release.

## Before tagging

1. Stop every local server so compiled extensions are not locked.
2. Confirm `VERSION` contains the intended SemVer prerelease on one line.
3. Review current changes and ensure development executables, logs, dumps, and
   local configuration are not tracked for release.
4. Run:

   ```powershell
   py -m pytest tests -q
   py run_server.py --version
   py run_server.py --check
   ```

5. Trigger **Build portable alpha release** manually from GitHub Actions. A
   manual run builds and retains artifacts but never creates a GitHub release.
6. Download and inspect at least the Windows x86_64 archive. Extract it outside
   the repository and run its `--version` and `--check` commands.

## Publishing

After the changes intended for release are reviewed and pushed, create and push
an annotated tag matching `v` plus the exact `VERSION` value:

```bash
git tag -a v0.0.1-alpha.1 -m "BattleSpades 0.0.1 alpha 1"
git push origin v0.0.1-alpha.1
```

The tag workflow:

1. validates the tag against `VERSION`;
2. installs the pinned Python 3.12 release toolchain;
3. compiles ENet, Cython, and Recast/Detour on each native runner;
4. runs the source test suite;
5. builds and stages the portable archive;
6. runs packaged `--version` and `--check` outside the archive directory;
7. uploads the six matrix artifacts;
8. rejects missing, extra, or mislabeled zips;
9. creates `SHA256SUMS.txt` and publishes one GitHub prerelease.

Do not create the tag from a dirty or partially pushed checkout: GitHub builds
the committed tag, not local files.

## Post-release verification

1. Confirm the GitHub release is marked **Pre-release**.
2. Confirm it contains six zips plus `SHA256SUMS.txt`.
3. Recalculate one downloaded archive's SHA-256 and compare it with the
   manifest.
4. Extract—not run from inside—the zip and execute `--check`.
5. Confirm the archive contains all tracked `.vxl` and `.kv6` files and no
   tests, traces, dumps, helper executables, or source-only fallback paths.
6. Record failures with the workflow run URL, target name, runner image,
   compiler/Python diagnostics, and packaged check output.

## Failed or bad release

If any matrix job fails, fix the code or build environment and publish a new
prerelease version such as `0.0.1-alpha.2`; do not move an already published
tag to different source.

If publication succeeded but the release is unsafe, mark it as a draft or
delete the GitHub release assets, document the reason, and publish a new version.
Deleting a GitHub release does not delete its Git tag; remove the tag only when
it was never intended to identify a real source state.

## Future signing

Apple Developer ID signing, hardened-runtime validation, notarization, and
stapling belong between PyInstaller output and archive staging. Windows
Authenticode belongs at the same stage. Until credentials and validation steps
exist, release notes must continue to describe the binaries as unsigned and
macOS archives as unnotarized.
