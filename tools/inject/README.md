# MTGA collection-dump injector

A .NET payload injected into MTG Arena's Mono runtime to dump the
in-memory card collection (`WrapperController.Instance.InventoryManager.Cards`)
to disk. Backs `tools/mtg collection dump`.

## Why injection rather than reading game files

MTGA's on-disk `Player.log` only logs **collection deltas** —
acquisitions, crafts, drafts, single-card edits. The full card
snapshot lives in process memory inside the running game. A fresh
install or a wiped log file therefore cannot be reconstructed from
disk; the only canonical source is the live `InventoryManager`. The
injector reads that object directly via Mono's reflection API.

## Build

From this directory:

```bash
dotnet build -c Release
```

Requires `dotnet-sdk_8`, already provided by the repo's `flake.nix`
(`nix develop` / direnv).

## Run

```bash
tools/mtg collection dump
```

The CLI invokes the built injector executable inside the **same Wine
prefix** as the running MTGA process. Linux/Proton specific — there is
no native Windows or macOS path here.

## Layout

- `payload/` — the MonoBehaviour DLL loaded into MTGA. Walks
  `WrapperController.Instance.InventoryManager.Cards` and writes the
  snapshot JSON.
- `injector/` — the loader exe. Calls into Mono's C API
  (`mono_runtime_invoke`) to surface `payload.dll` inside the game's
  AppDomain.
- `vendor/` — pinned third-party DLLs (mono.dll signatures,
  P/Invoke helpers). Vendored to keep the build reproducible.
- `build/` — `bin/` and `obj/` output from `dotnet build`. Gitignored.

## Sidecar config

The two processes communicate via a JSON file at
`Path.GetTempPath()/mtg-toolkit-inject/config.json`. The injector
writes the desired output path before launching the payload; the
payload reads the path on load and writes the snapshot there.

## Wine path translation

Both the injector exe and the payload DLL run inside the MTGA Wine
prefix, so paths must be in Windows form. `Z:\home\hybridz\...\out.json`
inside the prefix resolves to `/home/hybridz/.../out.json` on the host
because Wine maps `Z:\` to the Linux root and the prefix is shared.
The CLI takes care of translating the configured Linux path before
writing the sidecar config.
