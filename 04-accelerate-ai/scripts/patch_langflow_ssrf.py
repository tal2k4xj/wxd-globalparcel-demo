#!/usr/bin/env python3
"""Build a Langflow-patched ADK compose file after pip install.

Stock `compose-lite.yml` only forwards a few LANGFLOW_* keys into the container.
This copies the installed ADK compose into `.adk/docker-compose.yml` (gitignored)
and adds LANGFLOW_SSRF_PROTECTION_ENABLED. `scripts/start_wxo.sh` points
`orchestrate server start --compose-file` at that copy, so a new
`pip install ibm-watsonx-orchestrate` is picked up on the next start.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ENV_KEY = "LANGFLOW_SSRF_PROTECTION_ENABLED"
COMPOSE_LINE = f"      {ENV_KEY}: ${{{ENV_KEY}:-false}}\n"
DOTENV_BLOCK = (
    f"\n# Allow Langflow API Request to call the host ops API on :8081 "
    f"(private Docker IPs).\n{ENV_KEY}=false\n"
)


def _stock_compose() -> Path:
    try:
        import ibm_watsonx_orchestrate
    except ImportError as exc:
        raise SystemExit(
            "ibm-watsonx-orchestrate is not installed in this environment. "
            "Activate the repo venv and run: pip install -r requirements.txt"
        ) from exc

    path = (
        Path(ibm_watsonx_orchestrate.__file__).resolve().parent
        / "developer_edition"
        / "resources"
        / "docker"
        / "compose-lite.yml"
    )
    if not path.is_file():
        raise SystemExit(f"ADK compose file not found: {path}")
    return path


def absolutize_paths(text: str, stock_dir: Path) -> str:
    """The copy lives in .adk/, so point build contexts and bind mounts
    (`context: ./minio`, `- ./sdk:/packages`) back at the ADK's resource folder."""
    text = re.sub(
        r"^([ \t]+context:[ \t]+)\./(\S+)",
        lambda m: f"{m.group(1)}{stock_dir / m.group(2)}",
        text,
        flags=re.M,
    )
    return re.sub(
        r"^([ \t]+-[ \t]+)\./([^:\s]+):",
        lambda m: f"{m.group(1)}{stock_dir / m.group(2)}:",
        text,
        flags=re.M,
    )


def native_minio_builder(text: str, stock_dir: Path, dest_dir: Path) -> str:
    """Dockerfile.minio compiles MinIO in an amd64 Go stage; on Apple Silicon that
    runs under emulation and the Go compiler crashes. Copy the build context into
    .adk/minio with the builder on the native platform (GOARCH=amd64 still
    cross-compiles an amd64 binary), and point the compose copy at it."""
    src = stock_dir / "minio"
    dockerfile = src / "Dockerfile.minio"
    if not dockerfile.is_file():
        return text
    dst = dest_dir / "minio"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    df = dst / "Dockerfile.minio"
    df.write_text(
        re.sub(
            r"^FROM --platform=linux/amd64 (golang:\S+ AS builder)",
            r"FROM --platform=$BUILDPLATFORM \1",
            df.read_text(encoding="utf-8"),
            flags=re.M,
        ),
        encoding="utf-8",
    )
    return text.replace(f"context: {src}\n", f"context: {dst}\n")


ENABLED_PROFILES = {"langflow"}  # start_wxo.sh runs --with-langflow


def drop_optional_disabled_deps(text: str) -> str:
    """podman-compose ignores `required: false` in depends_on and crashes
    (KeyError) when the target sits in a profile that is not enabled
    (e.g. wxo-opensearch-knowledge, langfuse-worker). Drop just those entries."""
    import yaml

    services = yaml.safe_load(text).get("services", {})
    off = {
        name
        for name, svc in services.items()
        if svc.get("profiles") and not set(svc["profiles"]) & ENABLED_PROFILES
    }
    for target in off:
        text = re.sub(
            rf"^([ \t]+){re.escape(target)}:\n\1[ \t]+condition:.*\n\1[ \t]+required:[ \t]*[Ff]alse[ \t]*\n",
            "",
            text,
            flags=re.M,
        )
    return text


def patched_compose_text(stock: str) -> str:
    if re.search(rf"^[ \t]+{re.escape(ENV_KEY)}:", stock, flags=re.M):
        return stock
    updated, n = re.subn(
        r"^([ \t]+LANGFLOW_AUTO_LOGIN:.*\n)",
        rf"\1{COMPOSE_LINE}",
        stock,
        count=1,
        flags=re.M,
    )
    if n != 1:
        raise SystemExit(
            "Could not find LANGFLOW_AUTO_LOGIN in the ADK compose file. "
            "The layout may have changed — add "
            f"{ENV_KEY} under the langflow service environment by hand."
        )
    return updated


def ensure_dotenv(path: Path) -> str:
    if not path.is_file():
        return "missing"
    text = path.read_text(encoding="utf-8")
    if re.search(rf"^{re.escape(ENV_KEY)}=", text, flags=re.M):
        return "already set"
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + DOTENV_BLOCK, encoding="utf-8")
    return "added"


def main() -> int:
    chapter = Path(__file__).resolve().parent.parent
    dest_dir = chapter / ".adk"
    dest = dest_dir / "docker-compose.yml"
    dest_dir.mkdir(parents=True, exist_ok=True)

    stock_path = _stock_compose()
    text = absolutize_paths(
        patched_compose_text(stock_path.read_text(encoding="utf-8")),
        stock_path.parent,
    )
    text = drop_optional_disabled_deps(text)
    dest.write_text(
        native_minio_builder(text, stock_path.parent, dest_dir),
        encoding="utf-8",
    )
    print(f"wrote: {dest}")
    print(f"source: {stock_path}")

    for name in (".env", ".env.example"):
        print(f"{ensure_dotenv(chapter / name)}: {chapter / name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
