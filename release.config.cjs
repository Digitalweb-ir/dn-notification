/**
 * semantic-release configuration for the dn-notification project.
 *
 * The project itself is a Python application; this config exists so that
 * `npx semantic-release` (run from CI) can own version management on main:
 *
 *   1. Analyze commits since the last release (Conventional Commits).
 *   2. Pick the next semver version.
 *   3. Tag the release commit (vX.Y.Z) and create a GitHub Release
 *      with auto-generated notes.
 *   4. Hand the new version off to the next CI job via
 *      @semantic-release/exec's `successCmd` (writes to $GITHUB_ENV
 *      and to a step output, which the workflow exposes as a job
 *      output that downstream jobs consume).
 *   5. The downstream "docker" workflow job then builds and publishes
 *      the multi-arch image, with the version baked in as a build-arg
 *      and stamped on the OCI image labels.
 *
 * Why successCmd, not prepareCmd / publishCmd
 * -------------------------------------------
 * The semantic-release lifecycle is:
 *
 *   verifyRelease   -> runs once a release is *going* to happen,
 *                      before the tag is pushed. Good for sanity checks.
 *   prepare         -> runs after verifyRelease, before the tag.
 *                      Good for "write this version into a file in the
 *                      repo" patterns. The previous setup used this and
 *                      it created a stale-cache problem (the repo file
 *                      was updated on the remote but the developer's
 *                      local checkout kept the old value), so we no
 *                      longer mutate the repo at all.
 *   publish         -> runs after the GitHub Release exists.
 *   success         -> runs after publish, after the GitHub Release
 *                      has been created. This is the right hook for
 *                      "tell the rest of CI what version was just
 *                      released" — by the time we run, the tag exists,
 *                      the GitHub Release exists, and the version is
 *                      final. A failure here does not roll back the
 *                      release, but for our flow (writing a single
 *                      line to $GITHUB_ENV) that is fine: a missing
 *                      output simply means the docker job does not run.
 *
 *   ^ This is the recommended pattern in the semantic-release docs and
 *     in the @semantic-release/exec README: a single successCmd is the
 *     conventional way to make the release version available to the rest
 *     of CI without going through git describe or the GitHub API again.
 *
 * If no release is published (no qualifying commits), successCmd does
 * not run, so RELEASE_VERSION is never written, and the workflow's
 * docker job — which gates on `needs.release.outputs.released` — is
 * skipped automatically. This is semantic-release's documented
 * "skip" behavior; we are not adding a separate gate.
 *
 * The "break:" keyword (used by the previous hand-rolled system) is mapped to
 * a major release in `releaseRules` so existing commit habits keep working.
 */
module.exports = {
    branches: ['main'],
    tagFormat: 'v${version}',

    // Preserve the legacy `break:` keyword as a synonym for a breaking change.
    // The default Conventional Commits analyzer accepts `BREAKING CHANGE:`
    // (body) and `!` after the type (e.g. `feat!:`); this rule adds the old
    // `break:` prefix as a third option.
    releaseRules: [
        { breaking: true, release: 'major' },
        { type: 'break', release: 'major' },
    ],

    plugins: [
        '@semantic-release/commit-analyzer',
        '@semantic-release/release-notes-generator',
        [
            '@semantic-release/exec',
            {
                // Runs after the tag is pushed and the GitHub Release
                // is created. We do two things:
                //   1. `RELEASE_VERSION=<x.y.z>` goes to $GITHUB_ENV.
                //      GitHub Actions picks it up as a process env var
                //      for all subsequent steps in this job, and the
                //      workflow YAML can also read it via the step
                //      output we set below.
                //   2. We `echo ... >> "$GITHUB_OUTPUT"`. The
                //      workflow's `release` job declares this step as
                //      `id: success` and exposes `success.outputs.version`
                //      as a job-level output, which the `docker` job
                //      then reads via `needs.release.outputs.version`.
                // This is the only version-propagation mechanism the
                // project uses in CI: no git describe, no GitHub API
                // round-trip, no file in the repo. The version is
                // produced and consumed in the same workflow run.
                successCmd: 'echo "RELEASE_VERSION=${nextRelease.version}" >> "$GITHUB_ENV"',
            },
        ],
        '@semantic-release/npm',
        '@semantic-release/github',
    ],
};
