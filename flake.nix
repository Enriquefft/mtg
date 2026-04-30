{
  description = "MTG Arena deck-building toolkit";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, ... }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.uv
              pkgs.jq
              pkgs.curl
              # .NET SDK for building the MTGA Mono inventory injector
              # (tools/inject/). Pins on a single SDK so the build is
              # reproducible from the Nix dev shell.
              pkgs.dotnet-sdk_8
              # `tools/mtg collection dump` joins MTGA's pressure-vessel
              # mount namespace via nsenter to share its wineserver
              # session. util-linux ships nsenter; pinning here keeps
              # the dev shell self-contained on minimal hosts.
              pkgs.util-linux
            ];

            shellHook = ''
              unset PYTHONPATH
              export MTG_ROOT="$PWD"
              export PATH="$PWD/tools:$PATH"
              # Avoid contacting Microsoft's telemetry endpoint on every build.
              export DOTNET_CLI_TELEMETRY_OPTOUT=1
              export DOTNET_NOLOGO=1
            '';
          };
        }
      );
    };
}
