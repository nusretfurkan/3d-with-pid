#!/usr/bin/env python3
"""Export the curated publication candidate from an explicit manifest (standard library only).

Usage, from the workspace root::

    python scripts/export_release.py --check
    python scripts/export_release.py --output release/github [--git-init]

Contents come only from the manifest (default ``scripts/release_manifest.json``):
``templates`` (portable files that replace the private workspace equivalents;
a template destination also replaces the same path inside a tree), ``trees``
(copied recursively minus ``exclude_names``/``exclude_suffixes``), ``files``
(each must exist) and ``optional_files`` (copied when present, reported as
absent otherwise). Nothing else is copied. A missing template is refused; the
private file it replaces is never used instead.

The export fails closed, writing nothing, when a listed file or template is
missing; a path is absolute, contains ``..`` or unsafe characters, or lies
under a ``forbidden_roots`` entry (``artifacts/``, ``release/``, ``.agents/``,
``.claude/``, virtual environments, ...); a file or tree is reached through a
symlinked directory or resolves outside the source workspace; two entries map
to one destination; a symlink, non-regular file, file over ``max_file_bytes``
or non-UTF-8 file is met; or a file contains this machine's home directory or
user name in a path, or a recognised credential pattern (only the path, line
and pattern name are reported, never the match).

Every file is read once into memory (without following a final symlink, and
checked against the planned file identity). Size, UTF-8 and content checks
run on exactly those bytes, and exactly those bytes are written. Before the
candidate is published, each staged copy is re-read and compared with the
scanned bytes, and each source file is re-read: a source that changed during
the export refuses the export. The output directory must not exist or must be
empty: nothing is overwritten. The candidate is assembled in a sibling staging
directory and renamed into place, so a refused export leaves no partial tree.

The copy is deterministic: sorted traversal, file bytes only, mode 0644 or
0755 (when the source is executable). ``<output>.export.json`` (beside the
candidate, not inside it) records the file inventory with SHA-256 per file,
an inventory digest, the manifest hash, skipped counts, template overrides,
the Git result and the export time; only the time varies between exports of
the same sources.

``--check`` plans, reads and scans everything but writes nothing.
``--git-init`` runs ``git init`` in the published candidate, stages its files
and checks that the staged paths equal the inventory. It never commits, adds a
remote or pushes.

Exit status: 0 exported (or check passed); 1 refused, nothing exported;
2 usage error; 3 partial: the candidate was exported but a later step
(requested Git setup, the report) failed; the message says what exists.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "scripts", "release_manifest.json")
REPORT_FORMAT = "skid-release-export/1"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*(/[A-Za-z0-9_.][A-Za-z0-9_.-]*)*$")
SECRET_PATTERNS = {
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "github-token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
}


class Refused(Exception):
    """Nothing was exported."""

    def __init__(self, problems):
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems


class Partial(Exception):
    """The candidate directory was published, but a later step failed."""

    def __init__(self, output, reason, report=None, report_path=None):
        super().__init__(reason)
        self.output, self.reason, self.report, self.report_path = output, reason, report, report_path


def _checkpoint(stage):
    """No-op between export stages; tests replace it to inject faults (source or staging changes)."""


def _personal_patterns():
    """This machine's home directory and user name as path components (computed, never stored)."""
    patterns = {}
    home = os.path.realpath(os.path.expanduser("~"))
    if home not in ("/", "") and len(home) > 3:
        patterns["home-directory"] = re.compile(re.escape(home) + r"(?=/|\b)")
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name available: only the home directory is checked
        user = None
    if user and len(user) >= 3 and user not in ("root", "runner", "user"):
        patterns["user-path"] = re.compile(r"/(?:Users|home)/" + re.escape(user) + r"(?=/|\b)")
    return patterns


def _check_path(rel, forbidden, what):
    if not isinstance(rel, str) or not _SAFE_PATH.match(rel) or any(p in ("", ".", "..") for p in rel.split("/")):
        return f"{what} {rel!r} is not a safe relative path"
    for root in forbidden:
        if rel == root or rel.startswith(root.rstrip("/") + "/"):
            return f"{what} {rel!r} lies under the excluded {root!r}"
    return None


def _containment(source, rel):
    """A problem when any component of ``source/rel`` is a symlink or the path resolves outside ``source``.

    ``source`` is already a real path. Every directory component is checked with ``lstat`` (the final
    component is checked by the caller, which reports symlinked files by destination).
    """
    parts = rel.split("/")
    current = source
    for part in parts[:-1]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return f"{rel!r}: directory {os.path.relpath(current, source)!r} is missing"
        if stat.S_ISLNK(info.st_mode):
            return f"{rel!r} is reached through the symlink {os.path.relpath(current, source)!r}; not exported"
        if not stat.S_ISDIR(info.st_mode):
            return f"{rel!r}: {os.path.relpath(current, source)!r} is not a directory"
    real = os.path.realpath(os.path.join(source, *parts))
    if os.path.commonpath([real, source]) != source:
        return f"{rel!r} resolves outside the source workspace; not exported"
    return None


def load_manifest(path):
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    required = ("templates", "trees", "files", "optional_files", "exclude_names", "exclude_suffixes",
                "forbidden_roots", "max_file_bytes")
    missing = [key for key in required if key not in manifest]
    if missing or manifest.get("format") != "skid-release-manifest/1":
        raise Refused([f"manifest {path}: unsupported format or missing {missing}"])
    return manifest


def plan(source, manifest, output):
    """``(entries, identities, skipped, absent, overrides)``. Raises ``Refused``.

    ``entries`` maps destination -> source path; ``identities`` maps destination -> ``(st_dev, st_ino)`` of
    the planned regular file; ``overrides`` lists tree files replaced by a template with the same destination.
    """
    source = os.path.realpath(source)
    problems, entries, skipped, absent, overrides = [], {}, {}, [], []
    forbidden = list(manifest["forbidden_roots"])
    names, suffixes = set(manifest["exclude_names"]), tuple(manifest["exclude_suffixes"])
    output_real = os.path.realpath(output)
    template_dests = set()

    def add(dest, rel):
        problem = _check_path(dest, forbidden, "destination") or _containment(source, rel)
        if problem:
            problems.append(problem)
        elif dest in entries:
            problems.append(f"two manifest entries map to {dest!r}")
        else:
            entries[dest] = os.path.join(source, *rel.split("/"))

    for dest, template in sorted(manifest["templates"].items()):
        if not isinstance(template, str) or not _SAFE_PATH.match(template) or ".." in template.split("/"):
            problems.append(f"template {template!r} is not a safe relative path")
            continue
        if not os.path.lexists(os.path.join(source, template)):
            problems.append(f"template {template!r} for {dest!r} is missing")
        else:
            add(dest, template)
        template_dests.add(dest)
    for rel in manifest["files"]:
        problem = _check_path(rel, forbidden, "file")
        if problem:
            problems.append(problem)
        elif not os.path.lexists(os.path.join(source, rel)):
            problems.append(f"listed file {rel!r} is missing")
        else:
            add(rel, rel)
    for rel in manifest["optional_files"]:
        problem = _check_path(rel, forbidden, "optional file")
        if problem:
            problems.append(problem)
        elif os.path.lexists(os.path.join(source, rel)):
            add(rel, rel)
        else:
            absent.append(rel)
    for tree in manifest["trees"]:
        problem = _check_path(tree, forbidden, "tree")
        if problem:
            problems.append(problem)
            continue
        top = os.path.join(source, *tree.split("/"))
        problem = _containment(source, tree)
        if problem:
            problems.append(problem)
            continue
        if os.path.islink(top) or not os.path.isdir(top):
            problems.append(f"tree {tree!r} is missing or not a real directory")
            continue
        for directory, subdirs, files in os.walk(top, followlinks=False):
            if os.path.realpath(directory) == output_real:
                subdirs[:] = []
                continue
            keep = []
            for name in sorted(subdirs):
                full = os.path.join(directory, name)
                if name in names:
                    skipped[name] = skipped.get(name, 0) + 1
                elif os.path.islink(full):
                    problems.append(f"symlink {os.path.relpath(full, source)!r} is not exported")
                elif os.path.realpath(full) == output_real:
                    continue
                else:
                    keep.append(name)
            subdirs[:] = keep
            for name in sorted(files):
                full = os.path.join(directory, name)
                rel = os.path.relpath(full, source).replace(os.sep, "/")
                if name in names or name.endswith(suffixes):
                    key = name if name in names else os.path.splitext(name)[1]
                    skipped[key] = skipped.get(key, 0) + 1
                    continue
                if rel in template_dests:
                    overrides.append(rel)
                    continue
                add(rel, rel)
    identities = {}
    for dest, src in sorted(entries.items()):
        info = os.lstat(src)
        if stat.S_ISLNK(info.st_mode):
            problems.append(f"{dest!r} is a symlink")
        elif not stat.S_ISREG(info.st_mode):
            problems.append(f"{dest!r} is not a regular file")
        elif info.st_size > manifest["max_file_bytes"]:
            problems.append(f"{dest!r} has {info.st_size} bytes (limit {manifest['max_file_bytes']})")
        else:
            identities[dest] = (info.st_dev, info.st_ino)
    if problems:
        raise Refused(problems)
    return entries, identities, skipped, absent, sorted(overrides)


def _read_once(dest, src, identity, limit):
    """``(bytes, executable)`` read through one descriptor that does not follow a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(src, flags)
    except OSError as exc:
        raise Refused([f"{dest!r} cannot be opened without following links ({exc.strerror})"]) from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
            raise Refused([f"{dest!r} was replaced after planning"])
        data = handle.read(limit + 1)
    return data, bool(info.st_mode & 0o111)


def snapshot(source, entries, identities, limit):
    """Read every planned file exactly once: ``{dest: (bytes, executable)}``; size enforced on the bytes read."""
    source = os.path.realpath(source)
    problems, files = [], {}
    for dest, src in sorted(entries.items()):
        try:
            data, executable = _read_once(dest, src, identities[dest], limit)
        except Refused as exc:
            problems.extend(exc.problems)
            continue
        if len(data) > limit:
            problems.append(f"{dest!r} has more than {limit} bytes when read (limit {limit})")
            continue
        problem = _containment(source, os.path.relpath(src, source).replace(os.sep, "/"))
        if problem:
            problems.append(problem)
            continue
        files[dest] = (data, executable)
    if problems:
        raise Refused(problems)
    return files


def scan(files, patterns):
    """Content problems in the snapshot bytes: non-UTF-8 files and personal/credential matches.

    Only the path, line and pattern name are reported, never the match.
    """
    problems = []
    for dest, (data, _) in sorted(files.items()):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{dest}: not UTF-8 text (binaries are not exported)")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for name, pattern in patterns.items():
                if pattern.search(line):
                    problems.append(f"{dest}:{number}: matches {name}")
    return problems


def _prepare(output):
    if os.path.lexists(output):
        if os.path.islink(output) or not os.path.isdir(output) or os.listdir(output):
            raise Refused([f"{output} exists and is not an empty directory; nothing is overwritten"])
    report = output.rstrip(os.sep) + ".export.json"
    if os.path.lexists(report):
        raise Refused([f"{report} exists; nothing is overwritten"])
    return report


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _verify_before_publication(staging, entries, identities, files, limit):
    """Problems when a staged copy differs from the scanned bytes or a source changed since it was read."""
    problems = []
    for dest, (data, _) in sorted(files.items()):
        with open(os.path.join(staging, *dest.split("/")), "rb") as handle:
            if _sha(handle.read()) != _sha(data):
                problems.append(f"{dest!r}: the staged copy differs from the scanned bytes")
        try:
            again, _ = _read_once(dest, entries[dest], identities[dest], limit)
        except (Refused, OSError):
            problems.append(f"{dest!r}: the source changed during the export")
            continue
        if _sha(again) != _sha(data):
            problems.append(f"{dest!r}: the source changed during the export")
    return problems


def check(source, manifest_path, output=None):
    """Plan, read and scan without writing anything. Returns a summary dict; raises ``Refused``."""
    source = os.path.realpath(source)
    manifest = load_manifest(manifest_path)
    entries, identities, skipped, absent, overrides = plan(
        source, manifest, os.path.abspath(output) if output else os.path.join(source, "release"))
    files = snapshot(source, entries, identities, manifest["max_file_bytes"])
    problems = scan(files, {**_personal_patterns(), **SECRET_PATTERNS})
    if problems:
        raise Refused(problems)
    return {"files": len(files), "bytes": sum(len(d) for d, _ in files.values()), "skipped": skipped,
            "optional_absent": absent, "template_overrides": overrides}


def export(source, manifest_path, output, git_init=False):
    """Export and return ``(report, report_path)``.

    Raises ``Refused`` (nothing exported) or ``Partial`` (the candidate exists; a later step failed). A
    requested Git setup that fails is not an exception: ``report["git"]["initialized"]`` is false and the
    command line exits 3.
    """
    source = os.path.realpath(source)
    output = os.path.abspath(output)
    manifest = load_manifest(manifest_path)
    if os.path.commonpath([os.path.realpath(output), source]) == os.path.realpath(output):
        raise Refused(["the output may not contain the source workspace"])
    report_path = _prepare(output)
    limit = manifest["max_file_bytes"]
    entries, identities, skipped, absent, overrides = plan(source, manifest, output)
    _checkpoint("planned")
    files = snapshot(source, entries, identities, limit)
    problems = scan(files, {**_personal_patterns(), **SECRET_PATTERNS})
    if problems:
        raise Refused(problems)
    _checkpoint("scanned")
    parent = os.path.dirname(output)
    os.makedirs(parent, exist_ok=True)
    staging = os.path.join(parent, f".{os.path.basename(output)}.staging-{os.getpid()}")
    if os.path.lexists(staging):
        raise Refused([f"staging directory {staging} already exists"])
    inventory = []
    try:
        os.makedirs(staging)
        for dest, (data, executable) in sorted(files.items()):
            target = os.path.join(staging, *dest.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(data)
            mode = 0o755 if executable else 0o644
            os.chmod(target, mode)
            inventory.append({"path": dest, "bytes": len(data), "sha256": _sha(data), "mode": f"{mode:o}"})
        _checkpoint("staged")
        problems = _verify_before_publication(staging, entries, identities, files, limit)
        if problems:
            raise Refused(problems)
        if os.path.isdir(output):
            os.rmdir(output)  # verified empty above
        os.rename(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    report = None
    try:
        _checkpoint("published")
        listing = "".join(f"{f['sha256']}  {f['path']}\n" for f in inventory)
        with open(manifest_path, "rb") as handle:
            manifest_sha = _sha(handle.read())
        report = {"format": REPORT_FORMAT, "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                      timespec="seconds"),
                  "output": os.path.basename(output), "manifest_sha256": manifest_sha,
                  "inventory_sha256": _sha(listing.encode("utf-8")),
                  "counts": {"files": len(inventory), "bytes": sum(f["bytes"] for f in inventory)},
                  "skipped": dict(sorted(skipped.items())), "optional_absent": absent,
                  "template_overrides": overrides, "git": None, "files": inventory}
        if git_init:
            report["git"] = _git_init(output, [f["path"] for f in inventory])
        with open(report_path, "x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=1, sort_keys=True)
            handle.write("\n")
    except Exception as exc:  # noqa: BLE001 - the candidate exists: report a partial export, never "nothing"
        raise Partial(output, f"{type(exc).__name__}: {exc}", report,
                      report_path if os.path.exists(report_path) else None) from exc
    return report, report_path


def _git_init(output, expected):
    """Initialise and stage; ``initialized`` is true only when every step passed and staged == ``expected``."""
    git = shutil.which("git")
    if git is None:
        return {"initialized": False, "staged": False, "reason": "git is not on PATH", "steps": []}
    steps = []
    for shown in (["init", "-q", "-b", "main", "<output>"], ["-C", "<output>", "add", "-A"],
                  ["-C", "<output>", "ls-files", "-z"]):
        argv = [git] + [output if part == "<output>" else part for part in shown]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            steps.append({"argv": ["git"] + shown, "exit": None})
            return {"initialized": False, "staged": False, "steps": steps,
                    "reason": f"{type(exc).__name__}: {exc}"[-400:]}
        steps.append({"argv": ["git"] + shown, "exit": done.returncode})
        if done.returncode != 0:
            return {"initialized": False, "staged": False, "steps": steps,
                    "reason": f"git {shown[0] if shown[0] != '-C' else shown[2]} failed",
                    "stderr": done.stderr[-400:]}
    staged = sorted(p for p in done.stdout.split("\0") if p)
    if staged != sorted(expected):
        unstaged, extra = sorted(set(expected) - set(staged)), sorted(set(staged) - set(expected))
        return {"initialized": False, "staged": False, "steps": steps,
                "reason": "the staged paths differ from the inventory (ignore rules?)",
                "unstaged": unstaged[:50], "unexpected": extra[:50]}
    return {"initialized": True, "staged": True, "staged_files": len(staged), "committed": False,
            "remote": None, "steps": steps}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python scripts/export_release.py", description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", metavar="DIR", help="new or empty candidate directory")
    mode.add_argument("--check", action="store_true", help="plan, read and scan only; write nothing")
    parser.add_argument("--manifest", default=MANIFEST, metavar="JSON", help="release manifest")
    parser.add_argument("--source", default=ROOT, metavar="DIR", help=argparse.SUPPRESS)
    parser.add_argument("--git-init", action="store_true", help="git init and stage the candidate (no commit)")
    args = parser.parse_args(argv)
    if args.check and args.git_init:
        parser.error("--git-init needs --output")
    try:
        if args.check:
            summary = check(args.source, args.manifest)
        else:
            report, path = export(args.source, args.manifest, args.output, args.git_init)
    except Refused as exc:
        print(f"REFUSED: nothing was exported ({len(exc.problems)} problem(s)):", file=sys.stderr)
        for problem in exc.problems[:200]:
            print(f"  {problem}", file=sys.stderr)
        if len(exc.problems) > 200:
            print(f"  ... {len(exc.problems) - 200} more", file=sys.stderr)
        return 1
    except Partial as exc:
        print(f"PARTIAL: the candidate WAS exported to {args.output}, but a later step failed: {exc.reason}",
              file=sys.stderr)
        print(f"  report: {exc.report_path or 'not written'}; inspect or remove the candidate before retrying",
              file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print(f"REFUSED: nothing was exported: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.check:
        print(f"check passed: {summary['files']} files ({summary['bytes']} bytes) would be exported; nothing written")
        if summary["optional_absent"]:
            print(f"optional files absent: {', '.join(summary['optional_absent'])}")
        return 0
    print(f"exported {report['counts']['files']} files ({report['counts']['bytes']} bytes) to {args.output}; "
          f"inventory {report['inventory_sha256']}; report {path}")
    if report["optional_absent"]:
        print(f"optional files absent: {', '.join(report['optional_absent'])}")
    git = report["git"]
    if args.git_init and not git["initialized"]:
        print(f"PARTIAL: the candidate WAS exported, but the requested Git setup failed: {git.get('reason')}; "
              f"see {path}", file=sys.stderr)
        return 3
    if git:
        print(f"git: initialised and staged {git['staged_files']} files; no commit, no remote")
    return 0


if __name__ == "__main__":
    sys.exit(main())
