import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


TRANSCRIPTION_DIAGNOSTICS_DIR = Path(__file__).resolve().parent / "transcription_diagnostics"
MAX_SAVED_TRANSCRIPTION_FAILURES = 5
_ARCHIVE_KIND = "rejected_transcription"
_ARCHIVE_NAME = re.compile(r"\d{8}_\d{6}_\d{6}_[0-9a-f]{8}")


def _json_default(value):
    # WhisperX may leave NumPy scalars in its alignment results.
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _write_json(path, data):
    with path.open("w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2, default=_json_default)


def _prune_old_archives(root, keep):
    managed = []
    for folder in root.iterdir():
        if (
            not folder.is_dir() or folder.is_symlink()
            or not _ARCHIVE_NAME.fullmatch(folder.name)
            or folder.resolve().parent != root
        ):
            continue
        try:
            with (folder / "metadata.json").open(encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
        except (OSError, ValueError):
            continue
        if isinstance(metadata, dict) and metadata.get("kind") == _ARCHIVE_KIND:
            managed.append(folder)

    for folder in sorted(managed)[:-keep]:
        children = list(folder.iterdir())
        # Keep folders containing manually added files or subdirectories intact.
        if any(
            child.is_symlink() or not child.is_file()
            or (
                child.name not in {"metadata.json", "transcription.json", "alignment.json", "subtitles.ass"}
                and not re.fullmatch(r"audio\.[a-z0-9]{1,10}", child.name)
            )
            for child in children
        ):
            continue
        for child in children:
            if child.name != "metadata.json":
                child.unlink()
        (folder / "metadata.json").unlink()
        folder.rmdir()


def save_rejected_transcription(
    audio_path,
    transcription,
    alignment,
    ass_content,
    issues,
    *,
    title=None,
    settings=None,
    archive_dir=None,
    keep=None,
):
    snapshot = None
    created = False
    try:
        keep = MAX_SAVED_TRANSCRIPTION_FAILURES if keep is None else keep
        if not isinstance(keep, int) or keep < 1:
            raise ValueError("keep must be at least 1")
        root = Path(archive_dir if archive_dir is not None else TRANSCRIPTION_DIAGNOSTICS_DIR).resolve()
        source = Path(audio_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Audio file not found: {source}")

        root.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc)
        name = created_at.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8]
        snapshot = root / name
        snapshot.mkdir()
        created = True

        extension = source.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            extension = ".bin"
        audio_name = "audio" + extension
        shutil.copyfile(source, snapshot / audio_name)
        _write_json(snapshot / "transcription.json", transcription)
        _write_json(snapshot / "alignment.json", alignment)
        (snapshot / "subtitles.ass").write_text(ass_content, encoding="utf-8")
        # Write this last: only complete archives participate in retention.
        _write_json(snapshot / "metadata.json", {
            "kind": _ARCHIVE_KIND,
            "created_at_utc": created_at.isoformat(),
            "title": title,
            "source_audio": str(source),
            "audio_file": audio_name,
            "issues": list(issues),
            "settings": settings or {},
        })
    except Exception as error:
        if created:
            try:
                if snapshot.resolve().parent == root and not snapshot.is_symlink():
                    shutil.rmtree(snapshot, ignore_errors=True)
            except OSError:
                pass
        print(f"Could not save transcription diagnostics: {error}")
        return None

    print(f"Rejected transcription saved for diagnosis: {snapshot}")
    try:
        _prune_old_archives(root, keep)
    except OSError as error:
        print(f"Could not clean up old transcription diagnostics: {error}")
    return snapshot
