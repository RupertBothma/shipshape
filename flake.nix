{
  description = "Shipshape development environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
              default = pkgs.mkShell {
                packages = with pkgs; [
                  python314
                  python314Packages.pip
                  uv

              # Kubernetes tooling
              kubectl
              kustomize
              kind
              kubeconform
              kubernetes-helm
              istioctl
              tilt

              # Linting and formatting (also installed via uv for CI parity)
              ruff
              mypy

              # Container
              docker-client

              # Utilities
              yq-go
              jq
              curl
              git
            ];
            shellHook = ''
              export PIP_DISABLE_PIP_VERSION_CHECK=1
              echo "Shipshape dev shell ready. Run: uv sync --extra dev"
            '';
          };
        }
      );
    };
}
