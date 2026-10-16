# Tracked Baritone build input

`baritone-api-fabric-1.15.0-9-gd93f1582.jar` is the Baritone Fabric API build
used by the Minecraft 26.2 bridge. It corresponds to upstream short commit
[`d93f1582`](https://github.com/cabaletta/baritone/commit/d93f1582) and is
licensed under Baritone's LGPL-3.0 license.

The complete upstream license text is tracked as
[`LICENSE-Baritone.txt`](LICENSE-Baritone.txt). Corresponding source is the
linked exact commit. From that checkout, use its Gradle wrapper
(`.\gradlew.bat build` on Windows or `./gradlew build` elsewhere) and select the Fabric API
artifact for Minecraft 26.2. Preserve the license and source reference when
redistributing this JAR.

The file is tracked so a clean or isolated worktree has the exact compile-time
API — and the **same JAR is what goes into the client's `mods/` directory**;
there is no separate runtime Baritone to download. The bridge depends on this
version exactly (`=1.15.0-9-gd93f1582`), so any other build fails at startup
with `Incompatible mods found`. Its SHA-256 is
`86d06fce44e8c2da1a46eb44fb7d934fe3712ce63738ecd5be5c68f03f71a9cf`.
`scripts/check_repository_hygiene.py` verifies both the index and worktree
copy.

To update it, build the Fabric artifact from a reviewed exact upstream commit,
replace the JAR, then update the filename, hash, bridge dependency, this note,
license/source notice, and the repository hygiene check together. Run the Java 25 bridge build before
handoff. A compile-time dependency update does not authorize installation into
Minecraft.
