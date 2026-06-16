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
 *   4. The downstream "docker" workflow job builds and publishes the
 *      multi-arch image, with the version baked in as a build-arg and
 *      stamped on the OCI image labels.
 *
 * Why there is no `@semantic-release/exec` here
 * ---------------------------------------------
 * The previous setup also ran `scripts/write-version.sh` to write the
 * new version into a repo-root `VERSION` file and an `app/__init__.py`
 * header. That created a stale-cache problem: CI mutated the file on
 * `main`, but a developer's local checkout did not see the change
 * until they pulled, and the next local commit could roll the
 * version back to whatever was on disk locally. The git tag is the
 * single source of truth for the version; the app and the image read
 * it directly (the Dockerfile takes it as a build-arg, the app
 * reads `APP_VERSION` at import time), so there is no need to
 * mutate the repo at all.
 *
 * The default plugin set is preserved (commit-analyzer,
 * release-notes-generator, npm, github) per the semantic-release
 * docs, even though the npm plugin is a no-op for a private project.
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
        '@semantic-release/npm',
        '@semantic-release/github',
    ],
};
