import io
import subprocess
import zipfile

import pytest
from conftest import git

from public_mirror.scan import has_gps
from public_mirror.transform import transform


def test_publish_copies_only_public_files(project, capsys):
    project.config('[paths]\npublic = ["/README.md", "/docs/"]\nprivate = ["/docs/secret.md"]\n')
    project.write("README.md", "# Demo\n")
    project.write("docs/guide.md", "Guide\n")
    project.write("docs/secret.md", "Secret plans\n")
    project.write("notes/todo.md", "Not listed anywhere\n")
    project.commit()

    assert project.run("publish") == 0
    assert set(project.public_files()) == {"README.md", "docs/guide.md"}
    assert "notes/todo.md" in capsys.readouterr().out  # unmatched files are listed, not published
    # No private object reached the public repo, not even an unreferenced one.
    secret = git(project.private, "rev-parse", "HEAD:docs/secret.md")
    assert subprocess.run(["git", "cat-file", "-e", secret], cwd=project.public).returncode != 0


def test_settings_and_in_repo_deny_lists_are_never_published(project):
    project.config('[paths]\npublic = ["*"]\n\n[scan]\ndeny_files = ["deny.txt"]\n')
    project.write("deny.txt", "# terms that must stay private\nFalcon\n")
    project.write("a.md", "hello\n")
    project.commit()

    assert project.run("publish") == 0
    assert set(project.public_files()) == {"a.md"}


def test_private_blocks_are_removed_in_any_language(project):
    project.config('[paths]\npublic = ["*"]\n')
    project.write("README.md", "Intro\n<!-- private:start -->\nPhone list\n<!-- private:end -->\nOutro\n")
    project.write("tool.py", "x = 1\r\n# private:start\r\nNOTE = 'internal'\r\n# private:end\r\ny = 2\r\n")
    project.commit()

    assert project.run("publish") == 0
    files = project.public_files()
    assert files["README.md"] == b"Intro\nOutro\n"
    assert files["tool.py"] == b"x = 1\r\ny = 2\r\n"  # line endings survive


def test_prose_that_mentions_the_markers_is_left_alone():
    text = "Wrap lines in `<!-- private:start -->` and `<!-- private:end -->`.\n# private:start is the Python form\n"
    result = transform(text, "guide.md", markers=True, is_unpublished=None)
    assert result.text == text
    assert not result.errors


def test_broken_markers_block_publishing(project, capsys):
    project.config('[paths]\npublic = ["*"]\n')
    project.write("notes.md", "a\n<!-- private:start -->\nsecret\n")
    project.write("other.md", "b\n<!-- private:end -->\n")
    project.commit()

    assert project.run("publish") == 1
    out = capsys.readouterr().out
    assert "notes.md:2  [marker] private:start is never closed" in out
    assert "other.md:2  [marker] private:end without a private:start" in out
    assert project.public_head() is None


def test_markdown_lines_that_link_to_unpublished_files_are_dropped(project):
    project.config('[paths]\npublic = ["/README.md", "/docs/"]\nprivate = ["/docs/private/"]\n')
    project.write("README.md", "\n".join([
        "# Index",
        "- [Guide](docs/guide.md)",
        "- [Plans](docs/private/plans.md)",
        "- [All private notes](docs/private/)",
        "- [Unmatched file](notes/todo.md#top)",
        "- [Website](https://example.com/notes/todo.md)",
        "[plans]: docs/private/plans.md",
        '<img src="docs/private/photo.png">',
        "- [Missing file](docs/nope.md)",
        "",
    ]))
    project.write("docs/guide.md", "Guide\nSee [the plans](private/plans.md) and [home](/README.md).\n")
    project.write("docs/private/plans.md", "x\n")
    project.write("docs/private/photo.png", b"\x89PNG\r\n\x1a\n\0")
    project.write("notes/todo.md", "x\n")
    project.commit()

    assert project.run("publish") == 0
    files = project.public_files()
    assert files["README.md"].decode().splitlines() == [
        "# Index",
        "- [Guide](docs/guide.md)",
        "- [Website](https://example.com/notes/todo.md)",
        "- [Missing file](docs/nope.md)",
    ]
    assert files["docs/guide.md"] == b"Guide\n"


def test_scan_blocks_deny_terms_contacts_and_keys(project, capsys):
    project.config('[paths]\npublic = ["*"]\n\n[scan]\ndeny = ["Project Falcon"]\nallow = [\'me@public\\.dev\']\n')
    project.write("a.md", "Write to me@public.dev about project\n  FALCON.\n")
    project.write("b.md", "Intro\nCall +82 10 1234 5678 or mail recruiter@corp.co.kr\n")
    project.write("c.py", 'KEY = "sk-ant-' + "a" * 30 + '"\n')
    project.write("d.md", "Version 1.2.3 shipped on 2026-10-09, +30% faster.\n")
    project.commit()

    assert project.run("check") == 1
    out = capsys.readouterr().out
    assert "a.md:1  [deny]" in out  # across a line break, in another case
    assert "b.md:2  [phone]" in out
    assert "b.md:2  [email]" in out
    assert "c.py:1  [token]" in out
    assert "d.md" not in out
    assert "me@public" not in out  # allowed
    assert "1234 5678" not in out  # matches are masked in the report


def test_scan_finds_public_ip_addresses(project, capsys):
    project.config('[paths]\npublic = ["*"]\n')
    project.write("server.md", "\n".join([
        "Connect with ssh deploy@8.8.4.4 to reach the gateway.",
        "It also answers at https://api.1-1-1-1.sslip.io/v1/generate.",
        "Over IPv6: [2001:4860:4860::8888]:443",
        "Mapped, as in a log: ::ffff:9.9.9.9",
        "",
    ]))
    project.write("fine.md", "\n".join([
        "Local: 127.0.0.1, 192.168.0.10, 10.0.0.5, 172.16.4.2, 100.64.0.1, 224.0.0.1, fe80::1, ::1, fd00::7",
        "Examples: 203.0.113.7, 198.51.100.20, 192.0.2.1, 2001:db8::5, 203-0-113-7.sslip.io",
        "Versions: v1.2.3.4, 1.2.3.4.5, Python 3.11.4, jdk-17.0.19.10-hotspot, Chrome/120.0.0.0, 999.1.1.1",
        "Code and times: std::vector, a[1::2], 12:30:45, 00:1a:2b:3c:4d:5e, 2026-10-11",
        "File names: backup-2026-11-18-12-30.log, 11-18-12-30.md",
        "",
    ]))
    project.commit()

    assert project.run("check") == 1
    out = capsys.readouterr().out
    for line in (1, 2, 3, 4):
        assert f"server.md:{line}  [ip-address]" in out
    assert "fine.md" not in out
    assert "8.8.4.4" not in out  # masked in the report

    # An address that is meant to be public can be allowed, and the detector switched off.
    project.config('[paths]\npublic = ["*"]\n\n[scan]\nallow = [\'^8\\.8\\.4\\.4$\']\n')
    assert project.run("check") == 1
    out = capsys.readouterr().out
    assert "server.md:1  [ip-address]" not in out
    assert "server.md:2  [ip-address]" in out
    project.config('[paths]\npublic = ["*"]\n\n[scan]\nskip_detectors = ["ip-address"]\n')
    assert project.run("check") == 0


def test_replace_rewrites_terms_and_keeps_them_out(project, capsys):
    project.config("\n".join([
        '[paths]\npublic = ["*"]\n',
        "[transform.replace]",
        "'**Falcon** (our biggest client)' = 'a large client'",
        '"Falcon\'s" = "the client\'s"',
        '"Falcon" = "a client"',
        '" (Kim Minsu)" = ""',
        "",
    ]))
    project.write("a.md", "We served **Falcon** (our biggest client). Falcon's team\nliked it. Ask Falcon.\n"
                          "Mentored a teammate\n  (Kim Minsu) and a falconer.\n")
    project.write("b.docx", make_docx("Falcon"))  # documents are scanned but never rewritten
    project.commit()

    assert project.run("check", "-v") == 1
    out = capsys.readouterr().out
    assert "a.md: 4 terms replaced" in out
    assert "b.docx  [deny]" in out
    assert "a.md:" not in out.replace("a.md: 4", "")  # nothing left to report in the rewritten file

    project.write("b.docx", make_docx("A client"))
    project.commit()
    assert project.run("publish") == 0
    assert project.public_files()["a.md"] == (b"We served a large client. the client's team\nliked it. Ask a client.\n"
                                              b"Mentored a teammate and a falconer.\n")


def test_line_numbers_survive_replacements_that_join_lines(project, capsys):
    project.config('[paths]\npublic = ["*"]\n\n[transform.replace]\n"Big Client" = "a client"\n\n'
                   '[scan]\ndeny = ["secret"]\n')
    project.write("a.md", "Big\nClient\nline three\nsecret on line four\n")
    project.commit()

    assert project.run("check") == 1
    assert "a.md:4  [deny]" in capsys.readouterr().out


def test_reported_lines_point_at_the_source_file(project, capsys):
    project.config('[paths]\npublic = ["*"]\n\n[scan]\ndeny = ["Falcon"]\n')
    project.write("a.md", "one\n<!-- private:start -->\nhidden\n<!-- private:end -->\nFalcon\n")
    project.commit()

    assert project.run("check") == 1
    assert "a.md:5  [deny]" in capsys.readouterr().out


def make_docx(*runs: str) -> bytes:
    body = "".join(f"<w:r><w:t>{run}</w:t></w:r>" for run in runs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml",
                         f'<?xml version="1.0"?><w:document xmlns:w="w"><w:body><w:p>{body}</w:p></w:body></w:document>')
    return buffer.getvalue()


def test_word_files_are_scanned_through_their_text(project, capsys):
    project.config('[paths]\npublic = ["*"]\n')
    project.write("cv.docx", make_docx("Call +82 10", " 1234 5678"))  # Word splits text into runs
    project.commit()

    assert project.run("check") == 1
    assert "cv.docx  [phone]" in capsys.readouterr().out


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_pdf_text_is_scanned(project, capsys):
    pytest.importorskip("pypdf")
    project.config('[paths]\npublic = ["*"]\n\n[scan]\ndeny = ["Falcon"]\n')
    project.write("doc.pdf", make_pdf("Project Falcon"))
    project.commit()

    assert project.run("check") == 1
    assert "doc.pdf  [deny]" in capsys.readouterr().out


def jpeg(gps: bool) -> bytes:
    def u16(n): return n.to_bytes(2, "little")
    def u32(n): return n.to_bytes(4, "little")
    # IFD0 holds one entry: either the GPS pointer (to offset 26) or an unrelated tag (Orientation).
    ifd0 = u16(1) + (u16(0x8825) + u16(4) + u32(1) + u32(26) if gps else u16(0x0112) + u16(3) + u32(1) + u32(1)) + u32(0)
    gps_ifd = u16(1) + u16(2) + u16(5) + u32(3) + u32(0) + u32(0)  # GPSLatitude
    exif = b"Exif\0\0" + b"II*\0" + u32(8) + ifd0 + gps_ifd
    return b"\xff\xd8\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif + b"\xff\xd9"


def test_photo_locations_are_found(project, capsys):
    assert has_gps(".jpg", jpeg(gps=True))
    assert not has_gps(".jpg", jpeg(gps=False))

    project.config('[paths]\npublic = ["*"]\n')
    project.write("trip.jpg", jpeg(gps=True))
    project.write("plain.jpg", jpeg(gps=False))
    project.commit()
    assert project.run("check", "-v") == 1
    out = capsys.readouterr().out
    assert "trip.jpg  [gps]" in out
    assert "plain.jpg  [gps]" not in out
    assert "  plain.jpg" in out  # listed as not scanned


def test_publish_again_only_when_the_public_copy_changes(project, capsys):
    project.config('[paths]\npublic = ["/pub/"]\n')
    project.write("pub/a.md", "one\n")
    project.write("private.md", "x\n")
    project.commit()
    assert project.run("publish") == 0
    first = project.public_head()

    project.write("private.md", "y\n")
    project.commit()
    assert project.run("publish") == 0
    assert project.public_head() == first
    assert "already matches" in capsys.readouterr().out

    project.write("pub/a.md", "two\n")
    project.commit()
    assert project.run("publish", "-m", "Refresh the docs") == 0
    second = project.public_head()
    assert git(project.public, "rev-parse", f"{second}^") == first
    message = git(project.public, "log", "-1", "--format=%B", second)
    assert message.startswith("Refresh the docs")
    assert "Public-Mirror-Source: " in message


def test_dry_run_pushes_nothing(project, capsys):
    project.config('[paths]\npublic = ["*"]\n')
    project.write("a.md", "x\n")
    project.commit()

    assert project.run("publish", "--dry-run") == 0
    assert "First publish: 1 file." in capsys.readouterr().out
    assert project.public_head() is None


def test_nothing_public_means_nothing_published(project, capsys):
    project.config("[paths]\npublic = []\n")
    project.write("a.md", "x\n")
    project.commit()

    assert project.run("publish") == 1
    assert "nothing to publish" in capsys.readouterr().out
    assert project.public_head() is None


def push_public_change(project, tmp_path, name: str, content: bytes, message: str) -> None:
    clone = tmp_path / "contributor"
    if clone.exists():
        git(clone, "pull", "-q")
    else:
        git(tmp_path, "clone", "-q", str(project.public), str(clone))
    (clone / name).write_bytes(content)
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", message)
    git(clone, "push", "-q", "origin", "main")


def test_public_commits_are_pulled_before_publishing(project, tmp_path, capsys):
    project.config('[paths]\npublic = ["/README.md"]\n')
    project.write("README.md", "Title\n<!-- private:start -->\nsecret line\n<!-- private:end -->\nBody\n")
    project.commit()
    assert project.run("publish") == 0
    push_public_change(project, tmp_path, "README.md", b"Title\nBody\nContributed line\n", "Add a line")
    capsys.readouterr()

    assert project.run("publish") == 1
    assert "1 commit(s) that public-mirror did not make" in capsys.readouterr().out

    assert project.run("pull") == 0
    assert (project.private / "README.md").read_bytes() == (
        b"Title\n<!-- private:start -->\nsecret line\n<!-- private:end -->\nBody\nContributed line\n")
    project.commit("Pull the public change")

    assert project.run("publish") == 0
    assert project.public_files()["README.md"] == b"Title\nBody\nContributed line\n"
    tip = project.public_head()
    assert "Public-Mirror-Source: " in git(project.public, "log", "-1", "--format=%B", tip)
    assert project.run("publish") == 0
    assert project.public_head() == tip


def test_overwriting_a_public_repo_that_was_started_elsewhere(project, tmp_path, capsys):
    push_public_change(project, tmp_path, "LICENSE", b"MIT\n", "Initial commit")  # e.g. created on GitHub
    project.config('[paths]\npublic = ["/README.md"]\n')
    project.write("README.md", "Hello\n")
    project.commit()

    assert project.run("publish") == 1
    assert project.run("pull") == 1
    assert "no public-mirror commit yet" in capsys.readouterr().out
    assert project.run("publish", "--overwrite-public-changes") == 0
    assert set(project.public_files()) == {"README.md"}


VERSIONS = '[paths]\npublic = ["/README.md"]\nprivate = ["/data/"]\npublic_versions = "public-versions"\n'


def test_public_versions_stand_in_for_private_files(project, capsys):
    project.config(VERSIONS)
    project.write("README.md", "See [the tracker](data/tracker.csv).\n")
    project.write("data/tracker.csv", "id,company\n1,Real Company\n")
    project.write("public-versions/data/tracker.csv", "id,company\n1,Example Corp\n")
    project.write("public-versions/LICENSE", "MIT\n")
    project.commit()

    assert project.run("publish") == 0
    assert project.public_files() == {
        "README.md": b"See [the tracker](data/tracker.csv).\n",  # the link now has a public target
        "data/tracker.csv": b"id,company\n1,Example Corp\n",
        "LICENSE": b"MIT\n",
    }
    assert project.run("files", "data", "public-versions") == 0
    rows = [line.split()[:2] for line in capsys.readouterr().out.splitlines()]
    assert ["public", "data/tracker.csv"] in rows
    assert ["private", "public-versions/data/tracker.csv"] in rows


def test_csv_versions_must_keep_the_real_columns(project, capsys):
    project.config(VERSIONS)
    project.write("README.md", "x\n")
    project.write("data/tracker.csv", "id,company,stage\n1,Real Company,offer\n")
    project.write("public-versions/data/tracker.csv", "id,company\n1,Example Corp\n")
    project.commit()

    assert project.run("check") == 0  # a warning, not a blocker
    assert "public-versions/data/tracker.csv:1  [columns]" in capsys.readouterr().out


def test_include_publishes_committed_files_from_another_repo(project, tmp_path, capsys):
    other = tmp_path / "configs"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "rules.md").write_bytes(b"Rule one\n<!-- private:start -->\nsecret rule\n<!-- private:end -->\n"
                                     b"Call 010-1234-5678\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "Rules")
    project.config('[paths]\npublic = ["/README.md"]\n\n'
                   '[include]\n"docs/rules.md" = { repo = "../configs", path = "rules.md" }\n')
    project.write("README.md", "x\n")
    project.commit()

    assert project.run("check") == 1
    assert "docs/rules.md:5  [phone]" in capsys.readouterr().out  # scanned, with the source's line number

    (other / "rules.md").write_bytes(b"Rule one\n<!-- private:start -->\nsecret rule\n<!-- private:end -->\n")
    git(other, "commit", "-q", "-am", "Drop the phone number")
    (other / "rules.md").write_bytes(b"An uncommitted edit\n")  # only committed content is published
    assert project.run("publish") == 0
    assert project.public_files()["docs/rules.md"] == b"Rule one\n"


def test_broken_includes_are_errors(project, capsys):
    project.config('[paths]\npublic = ["/README.md"]\n\n'
                   '[include]\n"README.md" = { repo = "../nowhere", path = "x.md" }\n')
    project.write("README.md", "x\n")
    project.commit()
    assert project.run("check") == 2
    assert "already exists in this repo" in capsys.readouterr().err

    project.config('[paths]\npublic = ["/README.md"]\n\n'
                   '[include]\n"docs/x.md" = { repo = "../nowhere", path = "x.md" }\n')
    assert project.run("check") == 2
    assert "cannot read 'x.md'" in capsys.readouterr().err


def test_pull_leaves_files_with_public_versions_alone(project, tmp_path, capsys):
    project.config(VERSIONS)
    project.write("README.md", "Title\n")
    project.write("data/tracker.csv", "id,company\n1,Real Company\n")
    project.write("public-versions/data/tracker.csv", "id,company\n1,Example Corp\n")
    project.commit()
    assert project.run("publish") == 0
    push_public_change(project, tmp_path, "data/tracker.csv", b"id,company\n1,Example Corp\n2,Another\n", "Add a row")
    push_public_change(project, tmp_path, "README.md", b"Title\nMore\n", "Extend the README")
    capsys.readouterr()

    assert project.run("pull") == 0
    out = capsys.readouterr().out
    assert "data/tracker.csv (from public-versions/data/tracker.csv)" in out
    assert (project.private / "README.md").read_bytes() == b"Title\nMore\n"
    assert (project.private / "data" / "tracker.csv").read_bytes() == b"id,company\n1,Real Company\n"


def test_export_writes_the_public_copy(project, tmp_path):
    project.config('[paths]\npublic = ["/docs/"]\n')
    project.write("docs/a.md", "a\n<!-- private:start -->\nb\n<!-- private:end -->\n")
    project.write("secret.md", "s\n")
    project.commit()
    target = tmp_path / "preview"

    assert project.run("export", str(target)) == 0
    assert [p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()] == ["docs/a.md"]
    assert (target / "docs" / "a.md").read_bytes() == b"a\n"
    assert project.run("export", str(project.private / "preview")) == 2  # never inside the repo


def test_files_explains_each_decision(project, capsys):
    project.config('[paths]\npublic = ["/docs/"]\nprivate = ["*.secret.md"]\n')
    project.write("docs/a.md", "a\n")
    project.write("docs/b.secret.md", "b\n")
    project.write("c.md", "c\n")
    project.commit()

    assert project.run("files") == 0
    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert ["public", "docs/a.md", "public:", "/docs/"] in rows
    assert ["private", "docs/b.secret.md", "private:", "*.secret.md"] in rows
    assert rows[[row[1] for row in rows].index("c.md")][0] == "unmatched"


def test_negation_works_like_gitignore(project, capsys):
    project.config('[paths]\npublic = ["/docs/*", "!/docs/draft.md"]\n')
    project.write("docs/a.md", "a\n")
    project.write("docs/draft.md", "d\n")
    project.commit()

    assert project.run("files", "--status", "public") == 0
    assert [line.split()[1] for line in capsys.readouterr().out.splitlines()] == ["docs/a.md"]


def test_typos_in_settings_are_reported(project, capsys):
    project.config('[paths]\npublic = ["/README.md", "/nope/"]\n')
    project.write("README.md", "x\n")
    project.commit()
    assert project.run("check") == 0
    assert "public pattern '/nope/' matches no file" in capsys.readouterr().out

    project.config('[paths]\npublik = ["*"]\n')
    assert project.run("check") == 2
    assert "unknown key [paths] publik" in capsys.readouterr().err
