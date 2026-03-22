{ config, lib, pkgs, ... }:

let
  cfg = config.services.shigebot;
in
{
  options.services.shigebot = {

    enable = lib.mkEnableOption "Shigebot Twitch chat bot";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The shigebot package to use.";
    };

    configFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to shigebot.toml. Must not contain secrets — use environmentFile
        for credentials.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Path to a file of KEY=VALUE pairs loaded into the service environment.
        Must contain at least:
          TWITCH_CLIENT_ID
          TWITCH_CLIENT_SECRET
          TWITCH_BOT_TOKEN
          TWITCH_BOT_REFRESH
        Optionally:
          GITHUB_TOKEN  (raises gist API rate limit from 60 to 5000 req/hour)
      '';
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "shigebot";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "shigebot";
    };

  };

  config = lib.mkIf cfg.enable {

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      description = "Shigebot service user";
    };

    users.groups.${cfg.group} = {};

    systemd.services.shigebot = {
      description = "Shigebot Twitch chat bot";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/shigebot ${cfg.configFile}";
        User = cfg.user;
        Group = cfg.group;
        Restart = "on-failure";
        RestartSec = "10s";

        EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;

        # State: scripts are downloaded here and pickle files live here.
        # The service gets a private /var/lib/shigebot owned by its user.
        StateDirectory = "shigebot";
        StateDirectoryMode = "0700";

        # Hardening
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectHostname = true;
        ProtectClock = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = false; # Python JIT needs this off
        SystemCallFilter = [ "@system-service" "~@privileged" "~@mount" ];

        # The state directory is the only writable path the service needs.
        # Scripts are downloaded and run from there.
        ReadWritePaths = [ "/var/lib/shigebot" ];

        # Network access is required for Twitch EventSub and GitHub API.
        # Everything else is denied.
        IPAddressAllow = [ "any" ];

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "shigebot";
      };
    };
  };
}
