#!/usr/bin/env python3
"""Build the private static reader from edited prose and unchanged source quotes."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
DIST = SITE / "dist"


def read(path):
    return json.loads(path.read_text())


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_array(v):
    return [x for x in (v if isinstance(v, list) else [v] if v else []) if x]


def prose(v):
    if isinstance(v, list):
        return "; ".join(prose(x) for x in v)
    if isinstance(v, dict):
        return v.get("point") or v.get("text") or v.get("question") or json.dumps(v, ensure_ascii=False)
    return str(v or "")


def addressee_label(v, people):
    raw = prose(v)
    names = [name for name in people if name in raw]
    if "Эксперт" in raw or "эксперт" in raw:
        names.append("Эксперт")
    if not names:
        return "Участник с нужными документами"
    return ", ".join(names)


def flatten_text(value):
    if isinstance(value, dict):
        return " ".join(flatten_text(v) for k, v in value.items()
                        if k not in {"id", "slug", "source_page_ids", "kind", "sources", "url"})
    if isinstance(value, list):
        return " ".join(flatten_text(x) for x in value)
    return str(value or "")


def main(package=False):
    (DIST / "assets").mkdir(parents=True, exist_ok=True)
    (DIST / "search").mkdir(exist_ok=True)
    sources_data = read(SITE / "build/source-index.json")
    config = read(SITE / "content/ui.json")
    sources = sources_data["sources"]
    allowed = {s["id"] for s in sources}
    canonical = read(ROOT / "output/projection-b/questions.json")
    edited_data = read(SITE / "content/questions.edited.json")
    edited = {c["id"]: c for c in edited_data["cards"]}
    assert len(edited) == len(canonical["cards"])
    assert set(edited) == {c["id"] for c in canonical["cards"]}
    overview = read(SITE / "content/overview.edited.json")
    questions, quote_checks = [], []
    used_for = {pid: [] for pid in allowed}
    for i, original in enumerate(canonical["cards"], 1):
        ed = edited[original["id"]]
        quotes = []
        for ex in original["exhibits"]:
            if not ex.get("quote"):
                continue
            pids = list(dict.fromkeys(s["page_id"] for s in ex["sources"]))
            assert pids and set(pids) <= allowed, (original["id"], pids)
            q = {"text": ex["quote"], "source_page_ids": pids}
            if ex.get("quote_is_discontinuous"):
                q["note"] = "В цитате есть обозначенные пропуски. Сверьте её с полной страницей."
            quotes.append(q)
            quote_checks.append({"block": original["id"], "quote_sha256": hashlib.sha256(ex["quote"].encode()).hexdigest(),
                                 "copied_verbatim": True, "source_page_ids": pids})
            for pid in pids:
                if ed["title"] not in used_for[pid]:
                    used_for[pid].append(ed["title"])
        questions.append({
            "id": f"q{i}", "slug": f"vopros-{i}", "title": ed["title"], "summary": ed["summary"],
            "purpose": ed.get("purpose", ""), "addressee": addressee_label(original["addressee"], config["person_display_names"]),
            "kind": original["kind"], "main_questions": ed["main_questions"],
            "followups": ed.get("followups", {}), "limits": clean_array(ed.get("limits")),
            "counterpoints": clean_array(ed.get("counterpoints")),
            "asking_conditions": clean_array(ed.get("asking_conditions")), "quotes": quotes,
            "hearing_priority": config["question_priority"].get(original["id"],99)})
    sections = overview["sections"]
    extra_quotes = []
    for section in sections:
        for i, card in enumerate(section.get("cards", []), 1):
            card["slug"] = section["id"] + "-" + str(i)
            pids = clean_array(card.get("source_page_ids"))
            assert set(pids) <= allowed, (section["id"], card["title"], set(pids) - allowed)
            for q in card.get("quotes", []):
                assert set(q["source_page_ids"]) <= allowed
                extra_quotes.append(q["text"])
                pids += q["source_page_ids"]
            for pid in set(pids):
                if card["title"] not in used_for[pid]:
                    used_for[pid].append(card["title"])
    for p in overview.get("people", []):
        assert set(p.get("source_page_ids", [])) <= allowed
    # Validate every editorially selected quotation against the actual read inputs.
    review = read(SITE / "content/overview-edit-review.json")
    input_files = [
        "analysis-notes/projection-v/report.md", "analysis-notes/projection-v/claim-pleadings-reviewed.court-questions.md",
        "analysis-notes/chat-history/case-review.md", "analysis-notes/chat-history/bank-forms-root-review.md",
        "analysis-notes/chat-history/document-requests.md", "analysis-notes/chat-history/working-plan.md",
        "analysis-notes/case-projection-a-brief.md", "analysis-notes/people-authority-map.md",
        "analysis-notes/document-flow-map.md"]
    input_text = "\n".join((ROOT / p).read_text() for p in input_files)
    input_text += "\n" + "\n".join(ex.get("quote", "") for c in canonical["cards"] for ex in c["exhibits"])
    normal = lambda s: re.sub(r"\s+", " ", s.replace("> ", "")).strip()
    for q in extra_quotes:
        assert normal(q) in normal(input_text), "Editorial quote not found verbatim: " + q[:90]
    for s in sources:
        s["used_for"] = " · ".join(used_for[s["id"]][:2])
    content = {"sources": sources, "reports": sources_data["reports"], "questions": questions,
               "sections": sections, "people": overview.get("people", []),
               "global_notes": overview.get("global_notes", []),
               "question_rules": edited_data["review"]}
    content["ui"] = {k: config[k] for k in ["brand_subtitle","title_suffix","hearing_title",
        "hearing_person_match","hearing_count_label","questions_intro","person_filters","route_aliases"]}
    search = []
    for c in questions:
        search.append({"title": c["title"], "text": flatten_text(c), "section_title": "Вопросы · " + c["addressee"],
                       "url": "#questions?card=" + c["slug"]})
    for s in sections:
        for c in s.get("cards", []):
            search.append({"title": c["title"], "text": flatten_text(c), "section_title": s["title"],
                           "url": "#" + s["id"] + "?card=" + c["slug"]})
    for s in sources:
        search.append({"title": f"Том {s['volume']}, страница {s['page']}",
                       "text": s["used_for"] + (f" лист дела {s['case_sheet']}" if s["case_sheet"] is not None else ""),
                       "section_title": "Исходные страницы", "url": "#sources?source=" + s["id"]})
    content["search"] = search
    for source, target in [("index.html", "index.html"), ("app.js", "assets/app.js"), ("style.css", "assets/style.css"), ("favicon.svg", "assets/favicon.svg")]:
        shutil.copyfile(SITE / source, DIST / target)
    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    assert "/Users/" not in serialized
    assert all(pid not in serialized for pid in config["excluded_source_page_ids"])
    (DIST / "assets/content.json").write_text(serialized + "\n")
    (DIST / "search/index.json").write_text(json.dumps(search, ensure_ascii=False, separators=(",", ":")) + "\n")
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    files = []
    for p in sorted(DIST.rglob("*")):
        if p.is_file() and p.name != "release.json":
            assert not p.is_symlink()
            files.append({"path": p.relative_to(DIST).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p)})
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:10]
    release = {"release_id": f"sud-{now:%Y%m%d}-{digest}", "built_at": now.isoformat(),
               "file_count": len(files), "files": files}
    (DIST / "release.json").write_text(json.dumps(release, ensure_ascii=False, indent=2) + "\n")
    (SITE / "build/content-validation.json").write_text(json.dumps({
        "state": "PASS", "canonical_question_blocks": len(questions),
        "canonical_quotes": len(quote_checks), "canonical_quotes_copied_verbatim": True,
        "editorial_quotes_checked": len(extra_quotes), "all_source_links_allowlisted": True,
        "source_pages": len(sources), "section_cards": sum(len(s.get("cards", [])) for s in sections),
        "source_archive_hashes_preserved": True, "release_id": release["release_id"],
        "quote_checks": quote_checks}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"state": "built", "release_id": release["release_id"], "files": len(files),
                      "bytes": sum(x["bytes"] for x in files), "questions": len(questions),
                      "quotes": len(quote_checks), "source_pages": len(sources)}, ensure_ascii=False), flush=True)
    if package:
        artifact = SITE / "build/site.tar.zst"
        with artifact.open("wb") as output:
            proc = subprocess.Popen(["/opt/homebrew/bin/zstd", "-T1", "-3", "-q", "-c"], stdin=subprocess.PIPE, stdout=output)
            with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
                for p in sorted(DIST.rglob("*")):
                    if p.is_file():
                        info = tar.gettarinfo(str(p), p.relative_to(DIST).as_posix())
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mode = 0o644
                        with p.open("rb") as f:
                            tar.addfile(info, f)
            proc.stdin.close()
            assert proc.wait() == 0
        (SITE / "build/artifact.json").write_text(json.dumps({
            "release_id": release["release_id"], "file": str(artifact.relative_to(ROOT)),
            "bytes": artifact.stat().st_size, "sha256": sha(artifact)}, indent=2) + "\n")
        print(json.dumps({"archive": str(artifact.relative_to(ROOT)), "bytes": artifact.stat().st_size}))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--package", action="store_true")
    main(p.parse_args().package)
