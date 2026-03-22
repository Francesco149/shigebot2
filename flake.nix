{
  description = "Shigebot — Twitch chat bot with community-driven gist scripts";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-parts,
      ...
    }@inputs:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      flake = {
        nixosModules.shigebot = import ./module.nix;
        nixosModules.default = self.nixosModules.shigebot;
      };

      perSystem =
        { pkgs, system, ... }:
        let
          # twitchio 3.x requires Python >= 3.11 and in practice targets 3.12.
          # Using 3.12 also avoids the sphinx-9.x incompatibility that shows up
          # when Nix tries to build docs extras against python311.
          python = pkgs.python312;
          pyPkgs = python.pkgs;

          # ── twitchio 3.2.1 ──────────────────────────────────────────────
          # Not in nixpkgs. Pure-Python wheel; only runtime dep is aiohttp,
          # which is in nixpkgs. The sphinx/dev extras are not included.
          #
          # v3 is a full rewrite around EventSub + the Twitch HTTP API.
          # It replaced the IRC backend that v2 used, and requires a registered
          # Twitch application (client_id + client_secret) plus a numeric bot_id.
          twitchio = pyPkgs.buildPythonPackage {
            pname = "twitchio";
            version = "3.2.1";
            format = "wheel";

            src = pkgs.fetchurl {
              url = "https://files.pythonhosted.org/packages/7e/a1/2f7e31066eb2c6e78cecd95cf1dd66773cf8f90738339320e12df5e2da2d/twitchio-3.2.1-py3-none-any.whl";
              hash = "sha256-V20TH9+wN47gv9fzX4OBY2+akRw5YVia3ZcFWleNpVU=";
            };

            dependencies = with pyPkgs; [
              aiohttp
            ];

            doCheck = false;
            pythonImportsCheck = [ "twitchio" ];

            meta = with pkgs.lib; {
              description = "Async Python library for the Twitch EventSub API";
              homepage = "https://github.com/TwitchIO/TwitchIO";
              license = licenses.mit;
            };
          };

          # ── shigebot ────────────────────────────────────────────────────
          shigebot = pyPkgs.buildPythonPackage {
            pname = "shigebot";
            version = "1.0.4";
            pyproject = true;

            src = ./.;

            build-system = with pyPkgs; [
              setuptools
              wheel
              pkgs.makeWrapper
            ];

            dependencies = with pyPkgs; [
              twitchio
              httpx

              # Runtime deps for community scripts — scripts run as subprocesses
              # using sys.executable so they share this package's Python environment.
              numpy
              pandas
              scipy
              requests
              pyowm
              yt-dlp
            ];

            doCheck = false;

            meta = with pkgs.lib; {
              description = "Twitch chat bot with community-driven gist scripts";
              license = licenses.gpl3Only;
              mainProgram = "shigebot";
            };
          };

        in
        {
          packages.twitchio = twitchio;
          packages.shigebot = shigebot;
          packages.default = shigebot;

          apps.default = {
            type = "app";
            program = "${shigebot}/bin/shigebot";
          };

          apps.shigebot-auth = {
            type = "app";
            program = "${shigebot}/bin/shigebot-auth";
          };

          devShells.default = pkgs.mkShell {
            packages = [
              (python.withPackages (ps: [
                shigebot
                ps.ipython
                ps.mypy
                ps.ruff
              ]))
            ];
            shellHook = ''
              echo "Shigebot dev shell (twitchio 3.2.1 / python312)"
              echo "Required env vars:"
              echo "  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET"
              echo "  TWITCH_BOT_TOKEN, TWITCH_BOT_REFRESH"
              echo ""
              echo "First time setup: shigebot-auth"
              echo "Run bot:          shigebot shigebot.toml.example"
            '';
          };
        };
    };
}
