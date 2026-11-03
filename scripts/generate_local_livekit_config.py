"""Generate LOCAL-ONLY self-hosted LiveKit test credentials/configuration.

This creates no account, downloads no model, launches no server, and incurs
no API charges. Never use the generated loopback-only configuration in a
public production deployment without TLS, network, TURN and capacity review.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets


def write_private(path: Path, content: str) -> None:
    # Create new files exclusively, mode 0600; refuse to overwrite any existing
    # credentials or follow a symlink at the destination.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def generate(directory: Path) -> tuple[Path, Path]:
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("invalid_local_directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = directory / "livekit-local.yaml"
    envfile = directory / "local-media.env"
    if config.exists() or envfile.exists() or config.is_symlink() or envfile.is_symlink():
        raise FileExistsError("refusing_to_overwrite_credentials")
    # Both outputs must use the same generated key/secret.
    api_key = "pb_" + secrets.token_hex(14)
    media_secret = secrets.token_hex(32)
    identity_secret = secrets.token_hex(32)
    yaml = (
        "port: 7880\n"
        "log_level: info\n"
        "rtc:\n"
        "  udp_port: 7882\n"
        "  use_external_ip: false\n"
        "keys:\n"
        f"  {api_key}: {media_secret}\n"
    )
    env = (
        "# PRIVATE TEST CREDENTIALS. Local machine only; never commit.\n"
        "LIVEKIT_URL=ws://127.0.0.1:7880\n"
        f"LIVEKIT_API_KEY={api_key}\n"
        f"LIVEKIT_API_SECRET={media_secret}\n"
        f"PB_CALL_IDENTITY_SECRET={identity_secret}\n"
    )
    write_private(config, yaml)
    try:
        write_private(envfile, env)
    except BaseException:
        config.unlink(missing_ok=True)
        raise
    return config, envfile


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate private local LiveKit credentials without paid services"
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="New or empty private directory for non-production local test credentials",
    )
    args = parser.parse_args()
    config, env = generate(args.output.expanduser().resolve())
    print("Created local-only LiveKit config:", config)
    print("Created private environment file:", env)
    print("No server was started; no paid provider was contacted.")


if __name__ == "__main__":
    main()
