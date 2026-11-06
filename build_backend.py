"""Validate source before delegating to setuptools in isolated package builds."""

from pathlib import Path
import os
from tools.check_public_source import audit, load_policy


def _validated_root():
    root = Path(__file__).resolve().parent
    report = audit(
        root,
        load_policy(os.environ.get("LIVE_TRANSCRIPT_PRIVATE_POLICY")),
        allow_build_metadata=True,
    )
    if not report["ok"]:
        raise RuntimeError(
            "Source privacy/allowlist check failed; run tools/check_public_source.py locally"
        )
    return root


def _delegate(operation, *arguments):
    _validated_root()
    from setuptools import build_meta

    return getattr(build_meta, operation)(*arguments)


def get_requires_for_build_wheel(config_settings=None):
    return _delegate("get_requires_for_build_wheel", config_settings)


def get_requires_for_build_editable(config_settings=None):
    return _delegate("get_requires_for_build_editable", config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return _delegate("get_requires_for_build_sdist", config_settings)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _delegate("prepare_metadata_for_build_wheel", metadata_directory, config_settings)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _delegate("prepare_metadata_for_build_editable", metadata_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    import tempfile
    from live_transcript.core.source_files import approved_paths, read_source

    root = _validated_root()
    output = str(Path(wheel_directory).absolute())
    metadata = str(Path(metadata_directory).absolute()) if metadata_directory else None
    # Setuptools can retain deleted modules in build/lib across incremental
    # builds. Stage only reviewed bytes; never ship a previous build's files.
    with tempfile.TemporaryDirectory(prefix="reviewed-wheel-") as directory:
        staging = Path(directory)
        for name in approved_paths(root):
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(read_source(root, name))
        report = audit(staging, load_policy(os.environ.get("LIVE_TRANSCRIPT_PRIVATE_POLICY")))
        if not report["ok"]:
            raise RuntimeError("Staged wheel source privacy check failed")
        previous = Path.cwd()
        try:
            os.chdir(staging)
            return _delegate("build_wheel", output, config_settings, metadata)
        finally:
            os.chdir(previous)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _delegate("build_editable", wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    import shutil
    import tempfile
    from live_transcript.core.files import atomic_destination

    destination = Path(sdist_directory)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="source-distribution-") as directory:
        filename = _delegate("build_sdist", directory, config_settings)
        candidate = Path(directory) / filename
        _normalize_sdist(candidate)
        with atomic_destination(destination / filename) as temporary:
            shutil.copyfile(candidate, temporary)
    return filename


def _normalize_sdist(path):
    """Do not expose local usernames, numeric IDs or file timestamps in tar headers."""
    import gzip
    import tarfile
    from live_transcript.core.files import atomic_destination
    from live_transcript.core.source_files import validate_name

    with tarfile.open(path, "r:gz") as original:
        with atomic_destination(path) as temporary:
            with temporary.open("wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w") as archive:
                        for member in sorted(original.getmembers(), key=lambda item: item.name):
                            validate_name(member.name.rstrip("/"))
                            if not (member.isdir() or member.isfile()):
                                raise ValueError("Unsupported source archive member")
                            safe = tarfile.TarInfo(member.name)
                            safe.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
                            safe.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
                            safe.uid = safe.gid = safe.mtime = 0
                            safe.uname = safe.gname = ""
                            safe.size = member.size if member.isfile() else 0
                            if member.isfile():
                                with original.extractfile(member) as contents:
                                    archive.addfile(safe, contents)
                            else:
                                archive.addfile(safe)
