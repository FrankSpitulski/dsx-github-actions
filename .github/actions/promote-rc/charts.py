# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deterministic release metadata changes to an already-built Helm archive."""

import copy
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile

MAX_ARCHIVE = 128 * 1024 * 1024
MAX_FILES = 4096
MAX_RENDER = 8 * 1024 * 1024


class InvalidChart(ValueError):
    """A chart cannot be promoted without changing unapproved content."""


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def yaml_documents(data):
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
    if not isinstance(docs, list):
        raise InvalidChart("invalid chart YAML")
    return docs


def yaml_object(data):
    docs = yaml_documents(data)
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise InvalidChart("chart YAML must contain one mapping")
    return docs[0]


def encoded(value):
    # JSON is a YAML subset and avoids serializer-dependent archive bytes.
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def unpack(data, name):
    if len(data) > MAX_ARCHIVE:
        raise InvalidChart("chart archive exceeds the size limit")
    files = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                parts = PurePosixPath(member.name).parts
                if (not parts or parts[0] != name or
                        any(part in {".", ".."} for part in member.name.split("/")) or
                        member.name.startswith("/") or "\\" in member.name or
                        len(parts) > 32 or not (member.isfile() or member.isdir())):
                    raise InvalidChart("unsafe chart archive member")
                if member.isdir():
                    continue
                if member.name in files or len(files) >= MAX_FILES:
                    raise InvalidChart("duplicate or excessive chart files")
                total += member.size
                if total > MAX_ARCHIVE or member.size < 0:
                    raise InvalidChart("expanded chart exceeds the size limit")
                files[member.name] = (archive.extractfile(member).read(), member.mode & 0o777)
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


def rendered_images(data):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "chart.tgz"
        path.write_bytes(data)
        result = subprocess.run(
            ["helm", "template", "rc-promotion", str(path), "--include-crds"],
            capture_output=True, check=False, timeout=30,
        )
    if result.returncode or result.stderr or len(result.stdout) > MAX_RENDER:
        raise InvalidChart("chart image-reference render failed or exceeded the size limit")
    documents = yaml_documents(result.stdout)
    if len(documents) > 1024:
        raise InvalidChart("too many rendered chart resources")
    images = {}
    resources = set()
    visited = 0

    def walk(value, location, depth=0):
        nonlocal visited
        visited += 1
        if depth > 64 or visited > 100000:
            raise InvalidChart("rendered chart exceeds structural limits")
        if isinstance(value, dict):
            for key, child in value.items():
                child_location = (*location, key)
                if key == "image" and isinstance(child, str):
                    images[child_location] = child
                walk(child, child_location, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*location, index), depth + 1)

    for document in documents:
        if document is None:
            continue
        if not isinstance(document, dict) or not isinstance(document.get("metadata"), dict):
            raise InvalidChart("rendered chart resource has no stable identity")
        metadata = document["metadata"]
        identity = (document.get("apiVersion"), document.get("kind"),
                    metadata.get("namespace", "default"), metadata.get("name"))
        if not all(isinstance(item, str) and item for item in identity) or identity in resources:
            raise InvalidChart("rendered chart resource identity is invalid or duplicated")
        resources.add(identity)
        walk(document, identity)
    return images


def promote(data, name, rc, stable, commit, value_paths, manifest_digest, destination_images):
    files, metadata = validate(data, name, rc, commit)
    metadata = copy.deepcopy(metadata)
    metadata["version"] = stable
    metadata["appVersion"] = stable
    metadata["annotations"].update({
        "dsx.nvidia.com/promoted-from": rc,
        "dsx.nvidia.com/source-chart-digest": digest(data),
        "dsx.nvidia.com/rc-manifest-digest": manifest_digest,
    })
    files[f"{name}/Chart.yaml"] = (encoded(metadata), files[f"{name}/Chart.yaml"][1])
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
        files[entry] = (encoded(values), files[entry][1])
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path, (content, mode) in sorted(files.items()):
                info = tarfile.TarInfo(path)
                info.size = len(content)
                info.mode = mode
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
    promoted = output.getvalue()
    before = rendered_images(data)
    after = rendered_images(promoted)
    for location, reference in after.items():
        if before.get(location) != reference and reference not in destination_images:
            raise InvalidChart("changed chart image is not a verified promotion destination")
    return promoted
