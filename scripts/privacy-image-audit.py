"""Redacted check of image config and every saved layer, including deleted files."""

import argparse
import json
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED_UPSTREAM = "c313d061b0263dfbb1967ab64e4d7524c09a71a0"
MAX_CONTENT = 32 * 1024 * 1024
GENERIC_HOMES = {"me", "node", "user", "username", "test", "alice", "bob", "example", "demo", "fake", "agent"}


def candidates(data, allow_nul=False):
    if len(data) > MAX_CONTENT or (b"\0" in data and not allow_nul):
        return None
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    found = set()
    for match in re.finditer(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", source):
        octets = [int(part) for part in match.group().split(".")]
        if any(part > 255 for part in octets):
            continue
        if octets[0] == 10 or (octets[0] == 172 and 16 <= octets[1] <= 31) or octets[:2] == [192, 168]:
            found.add(("private-ipv4", match.group()))
    for match in re.finditer(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", source):
        domain = match.group(1).lower()
        reserved = domain in {"example.com", "example.net", "example.org"} or re.search(
            r"\.(?:example|test|invalid|localhost)$", domain
        )
        if not reserved and not domain.endswith(".noreply.github.com"):
            found.add(("personal-email", match.group().lower()))
    for match in re.finditer(
        r"aoc_[A-Za-z0-9_-]{16,}|[0-9]{6,}:[A-Za-z0-9_-]{20,}|(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY",
        source,
    ):
        found.add(("credential-shape", match.group()))
    for match in re.finditer(r"https?://[^\s'\"<>]+", source):
        try:
            host = (urlparse(match.group()).hostname or "").lower()
        except ValueError:
            continue
        if host.endswith((".lan", ".local", ".internal")) and host != "host.docker.internal":
            found.add(("private-url", host))
    for match in re.finditer(r"(?:/(?:home|Users)/|[A-Za-z]:\\Users\\)([A-Za-z0-9._-]+)", source, re.I):
        if match.group(1).lower() not in GENERIC_HOMES:
            found.add(("home-path", match.group()))
    return found


def accepted_upstream_candidates():
    paths = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", ACCEPTED_UPSTREAM],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.split(b"\0")
    accepted = set()
    for raw in paths:
        if not raw:
            continue
        content = subprocess.run(
            ["git", "show", ACCEPTED_UPSTREAM + ":" + raw.decode("utf-8")],
            cwd=ROOT, check=True, capture_output=True,
        ).stdout
        accepted.update(candidates(content) or ())
    return accepted


def check_content(data, accepted, counts, scope, allow_nul=False):
    found = candidates(data, allow_nul=allow_nul)
    if found is None:
        counts["binary-or-large-file-" + scope] += 1
    else:
        for category, value in found:
            if (category, value) not in accepted:
                counts[category] += 1


def image_path(raw):
    normalized = raw
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = PurePosixPath(normalized).parts
    if raw.startswith("/") or ".." in parts:
        raise ValueError("unsafe layer path")
    return parts


def application_parts(parts):
    if parts[:1] == ("app",):
        return parts[1:]
    if parts[:2] == ("srv", "nanoclaw"):
        return parts[2:]
    return None


def forbidden_app_path(parts):
    relative = application_parts(parts)
    if relative is None or not relative:
        return False
    lower = [part.lower() for part in relative]
    name = lower[-1]
    if name == ".env.example":
        return False
    return (
        (any(part in {"data", "groups", "store", "backups", ".ssh"} for part in lower) and len(relative) > 1)
        or re.match(r"^\.env(?:\.|$)", name) is not None
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def check_archive(archive, base_layers, accepted, expected_revision):
    counts = Counter()
    with tarfile.open(archive, "r:*") as outer:
        members = {member.name: member for member in outer}
        manifest = json.load(outer.extractfile(members["manifest.json"]))
        if len(manifest) != 1:
            raise ValueError("unexpected image archive")
        image = manifest[0]
        layers = image["Layers"]
        if not 0 <= base_layers < len(layers):
            raise ValueError("invalid base layer count")
        config = json.load(outer.extractfile(members[image["Config"]]))
        image_config = config.get("config", {})
        labels = image_config.get("Labels") or {}
        revision = labels.get("org.opencontainers.image.revision", "")
        if revision != expected_revision or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            counts["missing-or-wrong-revision"] += 1
        for entry in image_config.get("Env", []):
            name, separator, value = entry.partition("=")
            if separator and value and re.search(r"(?:PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|CREDENTIAL)", name, re.I):
                counts["secret-in-image-env"] += 1
        metadata = json.dumps({
            "env": image_config.get("Env", []),
            "labels": labels,
            "healthcheck": image_config.get("Healthcheck"),
            "history": [row.get("created_by", "") for row in config.get("history", [])],
        }).encode()
        check_content(metadata, accepted, counts, "metadata")
        for index, layer_name in enumerate(layers):
            with outer.extractfile(members[layer_name]) as stream:
                with tarfile.open(fileobj=stream, mode="r|*") as layer:
                    for member in layer:
                        parts = image_path(member.name)
                        if forbidden_app_path(parts) and not member.isdir():
                            counts["runtime-or-secret-path"] += 1
                        if index < base_layers or application_parts(parts) is None or "node_modules" in parts:
                            continue
                        if member.issym() or member.islnk():
                            counts["app-symlink"] += 1
                        elif member.isfile():
                            if member.size > MAX_CONTENT:
                                counts["binary-or-large-file-app"] += 1
                            else:
                                suffix = PurePosixPath(member.name).suffix.lstrip(".")
                                scope = "app-" + (suffix if re.fullmatch(r"[A-Za-z0-9]{1,12}", suffix) else "other")
                                check_content(
                                    layer.extractfile(member).read(MAX_CONTENT + 1),
                                    accepted,
                                    counts,
                                    scope,
                                    allow_nul=suffix == "js",
                                )
    return dict(sorted(counts.items()))


def image_layers(runtime, image):
    result = subprocess.run([runtime, "image", "inspect", image], check=True, capture_output=True)
    objects = json.loads(result.stdout)
    if len(objects) != 1:
        raise ValueError("image inspect failed")
    return objects[0]["RootFS"]["Layers"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--base-layers", type=int)
    parser.add_argument("--image")
    parser.add_argument("--base-image")
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        if args.archive:
            if args.base_layers is None or args.image or args.base_image:
                raise ValueError("invalid archive arguments")
            archive, base_layers = args.archive, args.base_layers
        else:
            if not args.image or not args.base_image or args.base_layers is not None:
                raise ValueError("invalid image arguments")
            base = image_layers(args.runtime, args.base_image)
            current = image_layers(args.runtime, args.image)
            if current[:len(base)] != base:
                raise ValueError("base layers do not match")
            base_layers = len(base)
        accepted = accepted_upstream_candidates()
        if args.archive:
            counts = check_archive(archive, base_layers, accepted, args.revision)
        else:
            with tempfile.TemporaryDirectory(prefix="nanoclaw-image-audit-") as temp:
                archive = Path(temp) / "image.tar"
                subprocess.run([args.runtime, "image", "save", "-o", str(archive), args.image],
                               check=True, capture_output=True)
                counts = check_archive(archive, base_layers, accepted, args.revision)
        print("privacy image audit:", "pass" if not counts else "blocked")
        print(json.dumps({"categories": counts, "new_layers": "checked"}))
        return 0 if not counts else 1
    except Exception:
        print("privacy image audit: unavailable; no source values printed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
