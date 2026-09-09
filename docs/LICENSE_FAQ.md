# Licence FAQ — FSL-1.1-ALv2 in plain English

Cassetta's core is released under the **Functional Source License, Version 1.1,
with an Apache 2.0 future grant** (FSL-1.1-ALv2). The FSL is uncommon, so this FAQ
explains it in everyday terms.

**This FAQ is a convenience summary, not legal advice. The authoritative text is
[`LICENSE`](../LICENSE).** If anything here and the licence disagree, the
licence wins.

## Can my company use Cassetta for free?

Almost certainly yes. The FSL grants you the right to **use, copy, modify, create
derivative works, and redistribute** the software for any **Permitted Purpose** —
which is *anything except a Competing Use*. The licence explicitly calls out these
as permitted:

- **Internal use** — running and building on Cassetta inside your organisation.
- **Non-commercial education** and **non-commercial research**.
- **Professional services** you provide to someone who is themselves using Cassetta
  under this licence.

So self-hosting Cassetta for your own agents, modifying it, and deploying it
internally are all Permitted Purposes, and all free of charge. Free of charge is
not the same as free of terms: the licence still asks you to keep the notices when
you redistribute, still ends your patent licence if you sue over patents, and still
draws the line at a Competing Use — which the next section defines.

## What is a "Competing Use" (a.k.a. competitive use) — the one thing you can't do?

The restriction people usually mean by "competitive use" is the licence's
**Competing Use** clause. A **Competing Use** means making Cassetta available **to
others** in a commercial product or service that:

1. **substitutes for** Cassetta itself; or
2. substitutes for any other product or service the licensor offers using Cassetta
   that exists when the version is released; or
3. offers **the same or substantially similar functionality** as Cassetta.

In short: you may not take Cassetta and sell it (or a near-clone of it) as a
competing hosted/commercial offering. Building your own product *on top of* Cassetta
for your own purposes is fine; reselling Cassetta-as-a-service is not.

## The catch that makes this generous: automatic Apache-2.0 conversion

Every released version **automatically converts to the Apache License, Version 2.0
on the second anniversary** of the date that version is made available. On and after
that date, the restriction above disappears and you may use that version under plain
Apache-2.0 — including for what would previously have been a Competing Use.

So the FSL is "source-available now, fully open-source in two years," version by
version.

## Other terms in brief

- **Patents** — you get a patent licence for permitted use; it terminates if you
  sue claiming Cassetta infringes a patent.
- **Redistribution** — if you share copies/modifications, include these terms (or a
  link) and keep the copyright notices.
- **Warranty** — none; the software is provided "as is".

## Still unsure?

Read [`LICENSE`](../LICENSE) and, for a commercial/competing use case
before the two-year conversion, consult your own counsel or contact the maintainer.
