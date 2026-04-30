# SharpMonoInjector (vendored)

Cross-process Mono runtime injection library used to load our payload DLL
into MTGA.exe and call its entry point.

- **Upstream:** https://github.com/warbler/SharpMonoInjector
- **Commit:** 73566c1be1e8e1bb25ab60683557958368b2cd47 (2019-03-23)
- **License:** MIT (see LICENSE in this directory)
- **Vendored on:** 2026-04-30
- **Target framework:** netstandard2.0 (consumable from .NET Framework 4.8
  and .NET 6+)

Why vendored: pinning the exact version we tested against, avoiding a
NuGet network dependency at build time, and keeping the build
reproducible from the Nix dev shell. Single source of truth per
CLAUDE.md.

The library is unmodified from upstream. Only this `VENDORED.md` was
added.
