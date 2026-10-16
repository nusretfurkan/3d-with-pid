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
to one destination; a directory inside a tree cannot be listed (it is never
silently omitted); a symlink, non-regular file, file over ``max_file_bytes``
or non-UTF-8 file is met; or a file contains this machine's home directory or
user name in a path, or a recognised credential pattern (only the path, line
and pattern name are reported, never the match).

Individually reviewed PNG images are the only binary exception. The optional
manifest key ``reviewed_images`` maps a destination to
``{"sha256": <64 lowercase hex>, "provenance": <relative path>}`` (no other
keys). Each approved destination must lie under ``docs/media/``, end in
``.png`` and be an exported (listed, present) file whose bytes as read have
exactly that SHA-256; its provenance must be another exported file that is
UTF-8 text. The PNG must have the PNG signature, well-framed chunks with valid
CRCs, only ``IHDR`` (first), ``IDAT`` (at least one, consecutive) and ``IEND``
(last, no bytes after it), sensible IHDR values (1..16384 px per side; only
8-bit greyscale or 8-bit RGB, colour types 0 and 2: no alpha channel, no
16-bit samples and no palette, so no sample value is invisible to a visual
review) and a complete zlib stream of exactly the expected decompressed size.
Malformed, duplicate (including a repeated top-level ``reviewed_images`` key),
unsafe or unused approvals refuse the export. Any other non-UTF-8 file,
including an unapproved ``.png``, is still refused.

Limits of the image checks: they cover integrity and metadata only. The tool
does not decode or inspect pixels, rendered text or glyphs, and performs no
OCR or steganography analysis. The personal and credential patterns are
searched in the raw file bytes only; image data is normally zlib-compressed,
so text inside it is not visible to that search, and a secret shown on screen
is never found by it. Per-scanline filter bytes are not validated (only the
total decompressed size is). This tool certifies nothing about secrets in an
image: the digest binds the exact bytes that were decoded by an external
decoder and visually reviewed outside this tool, and that review is the
control.

Every file is read once into memory (without following a final symlink, and
checked against the planned file identity). Size, UTF-8 and content checks
run on exactly those bytes, and exactly those bytes are written. Before the
candidate is published, each staged copy is re-read and compared with the
scanned bytes, and each source file is re-read: a source that changed during
the export refuses the export. The output directory must not exist or must be
empty: nothing is overwritten. The candidate is assembled in a sibling staging
directory and renamed into place, so a refused export leaves no partial tree.

Choose an output outside every manifest tree. The default location
``release/`` is a forbidden root and is never exported. Inside a tree, only
the *current* output directory is skipped: an earlier candidate (and its
``.export.json``) left inside a tree, for example after ``--output skid/out``,
would be exported again as ordinary source files. This is not detected.

The copy is deterministic: sorted traversal, file bytes only, mode 0644 or
0755 (when the source is executable). ``<output>.export.json`` (beside the
candidate, not inside it) records the file inventory with SHA-256 per file,
an inventory digest, the manifest hash, skipped counts, template overrides,
the Git result and the export time; only the time varies between exports of
the same sources. A reviewed image's inventory entry also records
``"type": "image/png"``, its width, height and provenance.

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
import struct
import subprocess
import sys
import zlib

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
IMAGE_ROOT = "docs/media/"
APPROVAL_KEYS = ("provenance", "sha256")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CHUNKS = (b"IHDR", b"IDAT", b"IEND")
PNG_MAX_SIDE = 16384
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Colour type -> permitted bit depths: 8-bit greyscale and 8-bit RGB only. Alpha (types 4 and 6) could hide
# sample values in fully transparent pixels and 16-bit samples carry low bytes a visual review cannot see;
# palette (type 3) needs PLTE, which is not permitted.
_PNG_DEPTHS = {0: (8,), 2: (8,)}
_PNG_CHANNELS = {0: 1, 2: 3}
REVIEWED_IMAGE_LIMITS = ("digest, structure and metadata checked; pixels are not inspected by this tool: no "
                         "decoding, OCR or steganography check, and the raw-byte pattern scan does not see "
                         "compressed image data; the digest binds externally decoded and visually reviewed bytes")
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


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


class _Object(dict):
    """A JSON object; ``duplicates`` names keys given more than once (the last value is kept, as by ``json``)."""

    duplicates = ()


def _object(pairs):
    found = _Object(pairs)
    if len(found) != len(pairs):
        names = [key for key, _ in pairs]
        found.duplicates = sorted({key for key in names if names.count(key) > 1})
    return found


def load_manifest(path):
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle, object_pairs_hook=_object)
    required = ("templates", "trees", "files", "optional_files", "exclude_names", "exclude_suffixes",
                "forbidden_roots", "max_file_bytes")
    missing = [key for key in required if key not in manifest]
    if missing or manifest.get("format") != "skid-release-manifest/1":
        raise Refused([f"manifest {path}: unsupported format or missing {missing}"])
    problems = _approval_problems(manifest)
    if problems:
        raise Refused([f"manifest {path}: {problem}" for problem in problems])
    return manifest


def _approval_problems(manifest):
    """Problems in the optional ``reviewed_images`` mapping (its form only; use is checked by ``plan``)."""
    if "reviewed_images" not in manifest:
        return []
    if "reviewed_images" in getattr(manifest, "duplicates", ()):  # json keeps the last: earlier entries would vanish
        return ["reviewed_images is given more than once; give one mapping of every approval"]
    approvals = manifest["reviewed_images"]
    if not isinstance(approvals, dict):
        return ["reviewed_images must be an object mapping a destination to {sha256, provenance}"]
    problems = [f"reviewed image {dest!r} is approved more than once"
                for dest in getattr(approvals, "duplicates", ())]
    forbidden, folded = list(manifest["forbidden_roots"]), {}
    for dest, approval in sorted(approvals.items()):
        problem = _check_path(dest, forbidden, "reviewed image")
        if problem:
            problems.append(problem)
            continue
        name = dest.rsplit("/", 1)[-1]
        if not dest.startswith(IMAGE_ROOT) or not name.endswith(".png") or name == ".png":
            problems.append(f"reviewed image {dest!r} must be a .png file under {IMAGE_ROOT!r}")
        if dest.lower() in folded:
            problems.append(f"reviewed images {folded[dest.lower()]!r} and {dest!r} differ only in case")
        folded[dest.lower()] = dest
        if not isinstance(approval, dict):
            problems.append(f"reviewed image {dest!r}: the approval must be an object {{sha256, provenance}}")
            continue
        problems.extend(f"reviewed image {dest!r}: approval key {key!r} is given more than once"
                        for key in getattr(approval, "duplicates", ()))
        if sorted(approval) != sorted(APPROVAL_KEYS):
            problems.append(f"reviewed image {dest!r}: the approval must have exactly the keys "
                            f"{list(APPROVAL_KEYS)}, not {sorted(approval)}")
            continue
        if not isinstance(approval["sha256"], str) or not _SHA256.match(approval["sha256"]):
            problems.append(f"reviewed image {dest!r}: sha256 must be 64 lowercase hexadecimal digits")
        provenance = approval["provenance"]
        problem = _check_path(provenance, forbidden, "provenance")
        if problem:
            problems.append(f"reviewed image {dest!r}: {problem}")
        elif provenance in approvals:
            problems.append(f"reviewed image {dest!r}: provenance {provenance!r} must be a text document, "
                            "not a reviewed image")
    return problems


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

        def unlistable(exc):  # os.walk would otherwise skip the directory silently: refuse instead of omitting it
            where = os.path.relpath(exc.filename or top, source).replace(os.sep, "/")
            problems.append(f"tree directory {where!r} cannot be listed ({exc.strerror or exc}); "
                            "its files would be silently omitted")

        for directory, subdirs, files in os.walk(top, onerror=unlistable, followlinks=False):
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
    for dest, approval in sorted(manifest.get("reviewed_images", {}).items()):
        if dest not in entries:
            problems.append(f"reviewed image {dest!r} is not an exported file; the approval is unused")
        if approval["provenance"] not in entries:
            problems.append(f"reviewed image {dest!r}: provenance {approval['provenance']!r} is not an exported file")
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


def _png_raw_size(width, height, bits_per_pixel, interlace):
    """Decompressed size of the image data: one filter byte plus the packed pixels per scanline (per Adam7 pass)."""
    total = 0
    for x0, y0, dx, dy in _ADAM7 if interlace else ((0, 0, 1, 1),):
        columns = (width - x0 + dx - 1) // dx if width > x0 else 0
        rows = (height - y0 + dy - 1) // dy if height > y0 else 0
        if columns and rows:
            total += rows * (1 + (columns * bits_per_pixel + 7) // 8)
    return total


def png_structure(data):
    """``(width, height)`` of a PNG made only of IHDR, IDAT and IEND; raises ``ValueError`` naming the defect.

    Checks the signature, chunk framing and CRCs, chunk types and order, IHDR values (8-bit greyscale or RGB
    only), and that the IDAT data is one complete zlib stream of exactly the size IHDR implies. It does not
    validate per-scanline filter bytes, decode or judge the pixels, or look for text in the decompressed data.
    """
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG (no PNG signature)")
    pos, kinds, idat = len(PNG_SIGNATURE), [], []
    while pos < len(data):
        if len(data) - pos < 12:
            raise ValueError(f"truncated chunk at byte {pos}")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if length > 0x7FFFFFFF or end > len(data):
            raise ValueError(f"truncated chunk at byte {pos}")
        body = data[pos + 8:end - 4]
        if zlib.crc32(kind + body) & 0xFFFFFFFF != struct.unpack(">I", data[end - 4:end])[0]:
            raise ValueError(f"chunk at byte {pos} has a bad CRC")
        if kind not in PNG_CHUNKS:
            shown = kind.decode("ascii") if kind.isalpha() else repr(kind)
            raise ValueError(f"chunk {shown} is not permitted (only IHDR, IDAT and IEND: no metadata or "
                             "ancillary chunks)")
        if (kind == b"IHDR") != (not kinds):
            raise ValueError("IHDR must be the first chunk and appear once")
        if kind == b"IDAT":
            if idat and kinds[-1] != b"IDAT":
                raise ValueError("IDAT chunks are not consecutive")
            idat.append(body)
        if kind == b"IEND":
            if length:
                raise ValueError("IEND has data")
            if end != len(data):
                raise ValueError(f"{len(data) - end} byte(s) after IEND")
        kinds.append(kind)
        pos = end
    if not kinds or kinds[-1] != b"IEND":
        raise ValueError("no IEND chunk (truncated)")
    if not idat:
        raise ValueError("no IDAT chunk")
    ihdr = data[16:16 + 13] if struct.unpack(">I", data[8:12])[0] == 13 else None
    if ihdr is None:
        raise ValueError("IHDR does not have 13 bytes")
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", ihdr)
    if not (1 <= width <= PNG_MAX_SIDE and 1 <= height <= PNG_MAX_SIDE):
        raise ValueError(f"dimensions {width}x{height} are outside 1..{PNG_MAX_SIDE} pixels")
    if colour not in _PNG_DEPTHS:
        raise ValueError(f"colour type {colour} is not permitted (only 0 greyscale and 2 RGB: no alpha channel, "
                         "no palette)")
    if depth not in _PNG_DEPTHS[colour]:
        raise ValueError(f"bit depth {depth} is not permitted (only 8-bit samples)")
    if compression or filtering or interlace not in (0, 1):
        raise ValueError(f"invalid IHDR (bit depth {depth}, colour type {colour}, compression {compression}, "
                         f"filter {filtering}, interlace {interlace})")
    expected = _png_raw_size(width, height, depth * _PNG_CHANNELS[colour], interlace)
    stream, produced, pending = zlib.decompressobj(), 0, b"".join(idat)
    try:
        while True:  # bounded output per step: a decompression bomb is refused, never held in memory
            out = stream.decompress(pending, 1 << 20)
            produced += len(out)
            if produced > expected:
                raise ValueError(f"image data decompresses to more than the {expected} bytes IHDR implies")
            pending = stream.unconsumed_tail
            if stream.eof or not (pending or out):
                break
    except zlib.error as exc:
        raise ValueError(f"image data is not a valid zlib stream ({exc})") from None
    if not stream.eof:
        raise ValueError("image data stream is incomplete")
    if stream.unused_data:
        raise ValueError("bytes follow the image data stream")
    if produced != expected:
        raise ValueError(f"image data decompresses to {produced} bytes; IHDR implies {expected}")
    return width, height


def _reviewed_image_problems(dest, data, approval, files):
    """Problems with one approved image: digest of the bytes read, PNG structure, provenance document."""
    problems = []
    if _sha(data) != approval["sha256"]:
        problems.append(f"{dest}: SHA-256 of the bytes read differs from the reviewed digest; "
                        "this image was not approved in this form")
    try:
        png_structure(data)
    except ValueError as exc:
        problems.append(f"{dest}: rejected reviewed PNG: {exc}")
    provenance = approval["provenance"]
    if provenance not in files:
        problems.append(f"{dest}: provenance {provenance!r} is not an exported file")
    else:
        try:
            files[provenance][0].decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{dest}: provenance {provenance!r} is not UTF-8 text")
    return problems


def scan(files, patterns, reviewed=None):
    """Content problems in the snapshot bytes: non-UTF-8 files and personal/credential matches.

    Only the path, line and pattern name are reported, never the match. A destination in ``reviewed`` (the
    manifest's ``reviewed_images``) is checked as an approved PNG instead of as UTF-8 text; its raw file bytes
    are still searched for every pattern. That search does not decompress the image data, so it only finds
    text stored literally in the file; it never finds text drawn as pixels (see the module docstring).
    """
    problems, reviewed = [], reviewed or {}
    for dest, (data, _) in sorted(files.items()):
        if dest in reviewed:
            problems.extend(_reviewed_image_problems(dest, data, reviewed[dest], files))
            raw = data.decode("latin-1")
            problems.extend(f"{dest}: matches {name}" for name, pattern in patterns.items() if pattern.search(raw))
            continue
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
    problems = scan(files, {**_personal_patterns(), **SECRET_PATTERNS}, manifest.get("reviewed_images"))
    if problems:
        raise Refused(problems)
    summary = {"files": len(files), "bytes": sum(len(d) for d, _ in files.values()), "skipped": skipped,
               "optional_absent": absent, "template_overrides": overrides}
    if "reviewed_images" in manifest:
        summary["reviewed_images"] = sorted(manifest["reviewed_images"])
    return summary


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
    limit, reviewed = manifest["max_file_bytes"], manifest.get("reviewed_images", {})
    entries, identities, skipped, absent, overrides = plan(source, manifest, output)
    _checkpoint("planned")
    files = snapshot(source, entries, identities, limit)
    problems = scan(files, {**_personal_patterns(), **SECRET_PATTERNS}, manifest.get("reviewed_images"))
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
            entry = {"path": dest, "bytes": len(data), "sha256": _sha(data), "mode": f"{mode:o}"}
            if dest in reviewed:
                width, height = png_structure(data)
                entry.update(type="image/png", width=width, height=height,
                             provenance=reviewed[dest]["provenance"])
            inventory.append(entry)
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
    mode.add_argument("--output", metavar="DIR", help="new or empty candidate directory; keep it outside every "
                      "manifest tree (e.g. release/github): an earlier candidate left inside a tree is re-exported")
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
        if summary.get("reviewed_images"):
            print(f"reviewed PNG images: {len(summary['reviewed_images'])} ({REVIEWED_IMAGE_LIMITS})")
        return 0
    print(f"exported {report['counts']['files']} files ({report['counts']['bytes']} bytes) to {args.output}; "
          f"inventory {report['inventory_sha256']}; report {path}")
    if report["optional_absent"]:
        print(f"optional files absent: {', '.join(report['optional_absent'])}")
    images = [f for f in report["files"] if f.get("type") == "image/png"]
    if images:
        print(f"reviewed PNG images: {len(images)} ({REVIEWED_IMAGE_LIMITS})")
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
