# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deterministic release metadata changes to an already-built Helm archive."""

import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import subprocess
import tarfile

MAX_ARCHIVE = 128 * 1024 * 1024
MAX_FILES = 4096


class InvalidChart(ValueError):
    """A chart cannot be promoted without changing unapproved content."""


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def yaml_object(data):
    result = subprocess.run(
        ["yq", "eval-all", "-o=json", "[.]",
         "-"], input=data, capture_output=True, check=False, timeout=30,
    )
    if result.returncode:
        raise InvalidChart("chart YAML could not be parsed")
    try:
        docs = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise InvalidChart("invalid chart YAML") from None
    if not isinstance(docs, list) or len(docs) != 1 or not isinstance(docs[0], dict):
        raise InvalidChart("chart YAML must contain one mapping")
    return docs[0]


def encoded(value):
    # JSON is a YAML subset and avoids serializer-dependent archive bytes.
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def assign(data, assignments, expected):
    # Keep untouched YAML nodes: a JSON round-trip changes Helm's no/off scalars.
    expression = " | ".join(
        "." + "".join(f"[{json.dumps(key)}]" for key in keys) +
        f" = strenv(DSX_VALUE_{index})"
        for index, (keys, _) in enumerate(assignments)
    )
    env = {**os.environ, **{
        f"DSX_VALUE_{index}": value for index, (_, value) in enumerate(assignments)
    }}
    result = subprocess.run(
        ["yq", "eval", "-p=yaml", "-o=yaml", expression, "-"],
        input=data, capture_output=True, check=False, timeout=30, env=env,
    )
    if result.returncode:
        raise InvalidChart("chart YAML could not be updated")
    if yaml_object(result.stdout) != expected:
        raise InvalidChart("chart update changed undeclared content")
    return result.stdout


def unpack(data, name):
    if len(data) > MAX_ARCHIVE:
        raise InvalidChart("chart archive exceeds the size limit")
    files = {}
    modes = {}
    total = 0
    entries = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                entries += 1
                if entries > MAX_FILES:
                    raise InvalidChart("excessive chart archive entries")
                parts = PurePosixPath(member.name).parts
                if (not parts or parts[0] != name or
                        any(part in {".", ".."} for part in member.name.split("/")) or
                        member.name.startswith("/") or "\\" in member.name or
                        len(parts) > 32 or not (member.isfile() or member.isdir())):
                    raise InvalidChart("unsafe chart archive member")
                if member.isdir():
                    continue
                total += member.size
                if total > MAX_ARCHIVE or member.size < 0:
                    raise InvalidChart("expanded chart exceeds the size limit")
                content = archive.extractfile(member).read()
                if member.name in files:
                    if files[member.name][0] != content or modes[member.name] != member.mode:
                        raise InvalidChart("conflicting duplicate chart file")
                    continue
                modes[member.name] = member.mode
                files[member.name] = (content, member.mode & 0o777)
    except (tarfile.TarError, EOFError, OSError):
        raise InvalidChart("invalid chart archive") from None
    if f"{name}/Chart.yaml" not in files:
        raise InvalidChart("chart metadata is missing")
    return files


def validate(data, name, version, commit):
    files = unpack(data, name)
    metadata = yaml_object(files[f"{name}/Chart.yaml"][0])
    if (metadata.get("name") != name or metadata.get("version") != version or
            metadata.get("appVersion") != version or
            not isinstance(metadata.get("annotations"), dict) or
            metadata["annotations"].get("dsx.nvidia.com/source-revision") != commit):
        raise InvalidChart("chart metadata does not match the RC and commit")
    return files, metadata


def promote(data, name, rc, stable, commit, value_paths, manifest_digest, destination_images):
    files, metadata = validate(data, name, rc, commit)
    metadata = copy.deepcopy(metadata)
    metadata["version"] = stable
    metadata["appVersion"] = stable
    annotations = {
        "dsx.nvidia.com/promoted-from": rc,
        "dsx.nvidia.com/source-chart-digest": digest(data),
        "dsx.nvidia.com/rc-manifest-digest": manifest_digest,
    }
    metadata["annotations"].update(annotations)
    entry = f"{name}/Chart.yaml"
    assignments = [(("version",), stable), (("appVersion",), stable)] + [
        (("annotations", key), value) for key, value in annotations.items()
    ]
    files[entry] = (assign(files[entry][0], assignments, metadata), files[entry][1])
    if value_paths:
        entry = f"{name}/values.yaml"
        if entry not in files:
            raise InvalidChart("declared chart values are missing")
        values = yaml_object(files[entry][0])
        for keys in value_paths:
            target = values
            for key in keys[:-1]:
                if not isinstance(target, dict) or key not in target:
                    raise InvalidChart("declared chart value path is missing")
                target = target[key]
            if not isinstance(target, dict) or target.get(keys[-1]) != rc:
                raise InvalidChart("declared chart value is not the selected RC")
            repository = target.get("repository")
            registry = target.get("registry", "")
            if (keys[-1] != "tag" or not isinstance(repository, str) or not repository or
                    not isinstance(registry, str)):
                raise InvalidChart("version paths require sibling repository and optional registry")
            reference = (f"{registry}/" if registry else "") + f"{repository}:{stable}"
            if reference not in destination_images:
                raise InvalidChart("declared chart image is not a verified promotion destination")
            target[keys[-1]] = stable
        files[entry] = (
            assign(files[entry][0], [(keys, stable) for keys in value_paths], values),
            files[entry][1],
        )
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path, (content, mode) in sorted(files.items()):
                info = tarfile.TarInfo(path)
                info.size = len(content)
                info.mode = mode
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
    return output.getvalue()
