/**
 * semantic-release configuration for the dn-notification project.
 *
 * The project itself is a Python application; this config exists so that
 * `npx semantic-release` (run from CI) can own version management on main:
 *
 *   1. Analyze commits since the last release (Conventional Commits).
 *   2. Pick the next semver version.
 *   3. Run scripts/write-version.sh to write the new version into the
 *      repo-root VERSION file and the app/__init__.py header.
 *   4. Commit those file changes, tag the commit (vX.Y.Z), and create
 *      a GitHub Release with auto-generated notes.
 *   5. The downstream "docker" workflow job then builds and publishes
 *      the multi-arch image.
 *
 * The default plugin set is preserved (commit-analyzer, release-notes-generator,
 * npm, github) per the semantic-release docs, even though the npm plugin is a
 * no-op for a private project. Only one extra plugin is added:
 * @semantic-release/exec, to write the version files.
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

    // Default plugin pipeline plus @semantic-release/exec. Order matters: exec
    // must run after analyze (we need the next version) and before
    // @semantic-release/github (so the version files land in the release
    // commit, not after it).
    plugins: [
        '@semantic-release/commit-analyzer',
        '@semantic-release/release-notes-generator',
        [
            '@semantic-release/exec',
            {
                // Run before the release commit. semantic-release invokes each
                // plugin in order; "verifyRelease" runs after analysis but
                // before prepare/publish, which is exactly where we want to
                // write the version files so they are included in the commit
                // that gets tagged.
                prepareCmd: 'bash write-version.sh ${nextRelease.version}',
            },
        ],
        '@semantic-release/npm',
        '@semantic-release/github',
    ],
};
