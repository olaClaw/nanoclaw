#!/usr/bin/env python3
"""Install the host-only Infomaniak broker and its hardened user service."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

FOLDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--group-folder", required=True)
    args = parser.parse_args()

    if not FOLDER_RE.fullmatch(args.group_folder):
        parser.error("group-folder must contain only letters, digits, dot, underscore, or hyphen")
    project_root = Path(args.project_root).expanduser().resolve()
    group_root = (project_root / "groups" / args.group_folder).resolve()
    if not group_root.is_dir() or group_root.parent != (project_root / "groups").resolve():
        parser.error(f"group folder does not exist: {group_root}")

    home = Path.home()
    install_dir = home / ".local" / "lib" / "nanoclaw" / "infomaniak-mail-readonly"
    service_dir = home / ".config" / "systemd" / "user"
    config_path = home / ".config" / "nanoclaw" / "infomaniak-mail-readonly.json"
    download_dir = group_root / "downloads" / "infomaniak"
    install_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    service_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    download_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    download_dir.chmod(0o700)

    source = Path(__file__).with_name("infomaniak_mail_broker.py")
    destination = install_dir / "broker.py"
    shutil.copyfile(source, destination)
    destination.chmod(0o755)

    unit = f"""[Unit]
Description=NanoClaw Infomaniak mail read-only broker
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {quote(str(destination))} serve --config {quote(str(config_path))}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ProtectControlGroups=true
ProtectKernelTunables=true
LockPersonality=true
RestrictSUIDSGID=true
ReadWritePaths={quote(str(download_dir))}

[Install]
WantedBy=default.target
"""
    unit_path = service_dir / "nanoclaw-infomaniak-mail.service"
    temporary = unit_path.with_suffix(".service.tmp")
    temporary.write_text(unit, encoding="utf-8")
    os.replace(temporary, unit_path)
    unit_path.chmod(0o644)

    print(f"BROKER={destination}")
    print(f"CONFIG={config_path}")
    print(f"DOWNLOAD_DIR={download_dir}")
    print(f"UNIT={unit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
