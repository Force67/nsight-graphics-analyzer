{
  description = "Nsight Graphics analyzer CLI, with an FHS-wrapped Nsight Graphics for NixOS";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: nixpkgs.lib.getName pkg == "nsight-graphics";
      };
    in
    {
      packages.${system} = rec {
        nsight-graphics = pkgs.callPackage ./nix/nsight-graphics.nix { analyzerSrc = self; };
        default = nsight-graphics;
      };
    };
}
