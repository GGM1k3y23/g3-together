#!/usr/bin/env python3
"""Collect and redact G3 Together logs for a support report.

Deutsche Kurz-Anleitung
=======================
1. Oeffne PowerShell in dem Ordner, in dem die ZIP-Datei entstehen soll.
2. Starte das Skript mit seinem Pfad und dem Gothic-3-Ordner:
       python "<Pfad>\\collect_logs.py" "C:\\Games\\Gothic 3"
   Liegen die Windows-Dokumente an einem anderen Ort, gib den Dokumente-Ordner
   als zweites Skriptargument an:
       python "<Pfad>\\collect_logs.py" "C:\\Games\\Gothic 3" "D:\\Profile\\Documents"
3. Haenge die erzeugte ZIP-Datei an deinen Fehlerbericht an. Das Skript greift
   nicht auf das Netzwerk zu und loescht keine Dateien.

Die ZIP-Datei wird im aktuellen Ordner angelegt. Pfade unter C:\\Users\\...,
IPv4-/IPv6-Adressen und die Konfigurationszeile "address" werden bereinigt.
127.0.0.1 bleibt erhalten.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path


_WINDOWS_USER_PATH = re.compile(
    r"(?i)(?<![\w%])([A-Z]:[\\/]Users[\\/]+)[^\\/\s\"'<>]+"
)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
_ADDRESS_SETTING = re.compile(r"^\s*address\s*=", re.IGNORECASE)


def _replace_user_paths(text: str) -> str:
    return _WINDOWS_USER_PATH.sub("%USERPROFILE%", text)


def _replace_ipv4(match: re.Match[str]) -> str:
    value = match.group(0)
    try:
        parsed = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return value
    return value if str(parsed) == "127.0.0.1" else "<ip>"


def _replace_ipv6(match: re.Match[str]) -> str:
    value = match.group(0)
    try:
        ipaddress.IPv6Address(value.split("%", 1)[0])
    except ipaddress.AddressValueError:
        return value
    return "<ip>"


def redact(text: str) -> str:
    """Replace local user path prefixes and IP addresses in text."""
    text = _replace_user_paths(text)
    text = _IPV4.sub(_replace_ipv4, text)
    return _IPV6.sub(_replace_ipv6, text)


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _ini_without_address(path: Path) -> str:
    lines = _read_text(path).splitlines(keepends=True)
    return "".join(line for line in lines if not _ADDRESS_SETTING.match(line))


def _files_to_collect(game_dir: Path, documents_dir: Path) -> list[tuple[Path, str, bool]]:
    files: list[tuple[Path, str, bool]] = []
    for name in ("g3together.log", "g3together.ini", "g3trace.log"):
        source = game_dir / name
        if source.is_file():
            files.append((source, name, name.lower() == "g3together.ini"))

    engine_dir = documents_dir / "gothic3"
    paru_log = engine_dir / "paru" / "g3debug.log"
    if paru_log.is_file():
        files.append((paru_log, "engine/paru/g3debug.log", False))
    for source in sorted(engine_dir.glob("ge3log*.log")):
        if source.is_file():
            files.append((source, f"engine/{source.name}", False))
    last_log = engine_dir / "Lastlog_GE3.log"
    if last_log.is_file():
        files.append((last_log, f"engine/{last_log.name}", False))
    return files


def _unique_archive_path(folder: Path, timestamp: str) -> Path:
    base = f"g3together-logs-{timestamp}"
    candidate = folder / f"{base}.zip"
    index = 2
    while candidate.exists():
        candidate = folder / f"{base}-{index}.zip"
        index += 1
    return candidate


def collect(game_dir: Path, documents_dir: Path, output_dir: Path) -> tuple[Path, int]:
    sources = _files_to_collect(game_dir, documents_dir)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    archive = _unique_archive_path(output_dir, timestamp)

    with zipfile.ZipFile(archive, mode="x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for source, archive_name, is_ini in sources:
            content = _ini_without_address(source) if is_ini else _read_text(source)
            bundle.writestr(archive_name, redact(content))
    return archive, len(sources)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="G3-Together- und Gothic-3-Logs bereinigen und als ZIP sammeln."
    )
    parser.add_argument("gothic3_dir", type=Path, help="Gothic-3-Installationsordner")
    parser.add_argument(
        "documents_dir",
        nargs="?",
        type=Path,
        default=Path.home() / "Documents",
        help="optionaler Dokumente-Ordner (Standard: %%USERPROFILE%%\\Documents)",
    )
    args = parser.parse_args(argv)

    if not args.gothic3_dir.is_dir():
        parser.error(f"Gothic-3-Ordner nicht gefunden: {args.gothic3_dir}")
    if not args.documents_dir.is_dir():
        parser.error(f"Dokumente-Ordner nicht gefunden: {args.documents_dir}")

    archive, count = collect(args.gothic3_dir, args.documents_dir, Path.cwd())
    print(f"ZIP erstellt: {archive.name} ({count} Dateien)")
    if count == 0:
        print("Keine passenden Logdateien gefunden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
