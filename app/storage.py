"""Per-lecture isolated file storage: data/lectures/<lecture_id>/{sources,images,pipeline,exports}."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .config import LECTURES_DIR

SUBDIRS = ("sources", "images", "pipeline", "exports")


def lecture_dir(lecture_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", lecture_id):
        raise ValueError("bad lecture id")
    d = LECTURES_DIR / lecture_id
    for sub in SUBDIRS:
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^\w.\- ]+", "_", name).strip() or "file"
    return name[:120]


def store_source(lecture_id: str, filename: str, data: bytes) -> tuple[str, Path]:
    d = lecture_dir(lecture_id) / "sources"
    name = safe_name(filename)
    target = d / name
    i = 1
    while target.exists():
        target = d / f"{Path(name).stem}_{i}{Path(name).suffix}"
        i += 1
    target.write_bytes(data)
    return str(target.relative_to(lecture_dir(lecture_id))), target


def store_image(lecture_id: str, filename: str, data: bytes) -> tuple[str, Path]:
    d = lecture_dir(lecture_id) / "images"
    name = safe_name(filename)
    target = d / name
    i = 1
    while target.exists():
        target = d / f"{Path(name).stem}_{i}{Path(name).suffix}"
        i += 1
    target.write_bytes(data)
    return str(target.relative_to(lecture_dir(lecture_id))), target


def write_pipeline(lecture_id: str, name: str, data: Any) -> None:
    p = lecture_dir(lecture_id) / "pipeline" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(p)


def read_pipeline(lecture_id: str, name: str) -> Any | None:
    p = lecture_dir(lecture_id) / "pipeline" / f"{name}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None
    return None


def delete_lecture_files(lecture_id: str, originals: bool = True, generated: bool = True) -> None:
    d = LECTURES_DIR / lecture_id
    if not d.exists():
        return
    if originals and generated:
        shutil.rmtree(d, ignore_errors=True)
        return
    if originals:
        shutil.rmtree(d / "sources", ignore_errors=True)
    if generated:
        for sub in ("pipeline", "exports"):
            shutil.rmtree(d / sub, ignore_errors=True)


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def read_drafts(lecture_id: str) -> dict:
    p = lecture_dir(lecture_id) / "drafts.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def write_drafts(lecture_id: str, drafts: dict) -> None:
    p = lecture_dir(lecture_id) / "drafts.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(drafts, ensure_ascii=False, indent=1))
    tmp.replace(p)
