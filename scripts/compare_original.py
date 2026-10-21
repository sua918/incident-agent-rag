"""Compare the project with the supplied original ZIP without modifying source."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("archive", type=Path)
args = parser.parse_args()
unchanged, changed, patches = [], [], []
with ZipFile(args.archive) as archive:
    for info in archive.infolist():
        if info.is_dir(): continue
        relative = Path(*Path(info.filename).parts[1:])
        target = (ROOT / relative).resolve()
        if ROOT not in target.parents: raise ValueError("Invalid archive path")
        before = archive.read(info)
        after = target.read_bytes() if target.exists() else b""
        record = {"file": relative.as_posix(), "original_sha256": hashlib.sha256(before).hexdigest(), "current_sha256": hashlib.sha256(after).hexdigest()}
        if before == after:
            unchanged.append(record)
        else:
            changed.append(record)
            try:
                patches.extend(difflib.unified_diff(before.decode("utf-8").splitlines(True), after.decode("utf-8").splitlines(True), fromfile="original/"+relative.as_posix(), tofile="improved/"+relative.as_posix()))
            except UnicodeDecodeError: pass
result = {"archive_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(), "original_file_count": len(unchanged)+len(changed), "unchanged_count": len(unchanged), "changed_count": len(changed), "unchanged": unchanged, "changed": changed}
destination = ROOT / "doc/improvement"
destination.mkdir(parents=True, exist_ok=True)
(destination/"original-comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
(destination/"changes.patch").write_text("".join(patches), encoding="utf-8")
print(json.dumps({key:result[key] for key in ["original_file_count", "unchanged_count", "changed_count"]}, ensure_ascii=False))
