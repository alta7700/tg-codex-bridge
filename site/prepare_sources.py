#!/usr/bin/env python3
"""Export only the reviewed, allowlisted source pages for the static reader."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
DIST = SITE / "dist"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest = json.loads((ROOT / "output/court-package/manifest.json").read_text())
    config = json.loads((SITE / "content/ui.json").read_text())
    rows = manifest["attachment_manifest"]
    assert len(rows) == len({r["page_id"] for r in rows}) == config["expected_source_pages"]
    assert all(r["page_id"] not in config["excluded_source_page_ids"] for r in rows)
    (DIST / "sources").mkdir(parents=True, exist_ok=True)
    (DIST / "assets/reports").mkdir(parents=True, exist_ok=True)
    (SITE / "build").mkdir(exist_ok=True)
    reports = []
    for i, item in enumerate([manifest["main"], *manifest["source_parts"]]):
        name = "analysis.pdf" if i == 0 else f"sources-{i}.pdf"
        target = DIST / "assets/reports" / name
        source = ROOT / item["file"]
        assert sha(source) == item["sha256"]
        if not target.exists() or target.stat().st_size != source.stat().st_size or sha(target) != item["sha256"]:
            shutil.copyfile(source, target)
        reports.append({"title": "Полный аналитический разбор" if i == 0 else f"Исходные страницы, часть {i}",
                        "url": "assets/reports/" + name, "pages": item["pages"],
                        "bytes": target.stat().st_size, "sha256": item["sha256"]})
    sources = []
    for n, row in enumerate(rows, 1):
        pid = row["page_id"]
        image = DIST / "sources" / (pid + ".jpg")
        src = ROOT / row["package_attachment_file"]
        page = row["package_attachment_page"]
        # Exactly one renderer is active; full-page images, no crops or enhancement.
        if not image.exists():
            subprocess.run(["/opt/homebrew/bin/pdftoppm", "-f", str(page), "-l", str(page),
                            "-singlefile", "-scale-to", "2400", "-jpeg",
                            "-jpegopt", "quality=88", str(src), str(image.with_suffix(""))],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        part = int(Path(row["package_attachment_file"]).stem[-2:])
        sources.append({"id": pid, "volume": row["volume"], "page": row["original_pdf_page"],
                        "local_page": row["part_pdf_page"], "case_sheet": row.get("case_sheet"),
                        "original_part": Path(row["source_pdf"]).name,
                        "image": "sources/" + image.name,
                        "pdf": f"assets/reports/sources-{part}.pdf#page={page}",
                        "attachment_page": page,
                        "image_sha256": sha(image)})
        if n % 10 == 0 or n == len(rows):
            print(json.dumps({"source_images_ready": n, "total": len(rows)}), flush=True)
    (SITE / "build/source-index.json").write_text(json.dumps({"sources": sources, "reports": reports},
                                                            ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"complete": True, "images": len(sources),
                      "exported_bytes": sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file())}))


if __name__ == "__main__":
    main()
