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
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.uv
              pkgs.jq
              pkgs.curl
            ];

            shellHook = ''
              unset PYTHONPATH
              export MTG_ROOT="$PWD"
              export PATH="$PWD/tools:$PATH"
            '';
          };
        }
      );
    };
}
