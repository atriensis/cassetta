# Security policy

## Reporting a vulnerability

Email **security@atriensis.ai**. Please do not open a public issue for a suspected vulnerability —
an issue discloses the defect at the moment it is filed, to everyone, including before there is
anything to upgrade to.

Useful things to include, none of them required:

- what an attacker gains, in one sentence;
- the version you were running (`GET /health` and the package metadata both report it, and
  [CHANGELOG.md](CHANGELOG.md) says what changed between versions);
- how to reproduce it, or the smallest thing that shows the behaviour;
- whether the deployment was exposed beyond `localhost`, and what was in front of it.

You will get an acknowledgement that a human has read it. This is a single-author project, so please
read that as a genuine best effort rather than as a service level: there is no on-call rotation
behind this address.

Please give a fix a reasonable chance to ship before disclosing publicly. If you intend to publish
on a schedule, say so in your first message and it will be worked to.

## Which versions are supported

There is no hand-maintained table here, because a table is a promise that goes stale silently. The
answer follows from the licence, which already has a per-version clock in it.

Every released version is published under FSL-1.1-ALv2 and **converts automatically to Apache-2.0 on
the second anniversary of its release**. That two-year window is the honest boundary of support:

- **Within two years of its release date** — a version is current enough that a security report
  against it is meaningful. In practice a fix lands on the newest release, and upgrading is the
  route to it.
- **After that** — the version has converted to Apache-2.0 and is yours to patch and redistribute
  under those terms. Reports are still read, but no fix is undertaken for a version that old.

The conversion is explained in plain English in [docs/LICENSE_FAQ.md](docs/LICENSE_FAQ.md), and the
authoritative text is [LICENSE](LICENSE). Where this file and the licence disagree, the licence wins.

In every case the practical advice is the same: **the fix ships on the newest release.** There is no
backporting to older lines.

## Before you deploy beyond localhost

Two things in this repository are deliberately insecure defaults for a clone-and-run quickstart, and
both are called out in the README:

- `CASSETTA_JWT_KEY` in `.env.example` is a fixed development placeholder. Anyone who has read this
  repository can mint a token against a server that still uses it. Replace it — or set
  `CASSETTA_JWT_KEY_FILE` — before the service is reachable from anywhere but the machine it runs on.
- The service terminates plain HTTP. TLS is the job of a reverse proxy in front of it.

Reports about either of these as shipped defaults are already known and documented; reports that a
deployment guide is *wrong or missing* about them are welcome and useful.
