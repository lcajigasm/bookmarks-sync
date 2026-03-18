#!/usr/bin/env python3
"""
browser-path — Sincroniza marcadores entre navegadores.

Soporta como origen y destino:
  Brave · Google Chrome (y Beta/Dev) · Vivaldi · Microsoft Edge ·
  Firefox · Firefox Developer Edition

Formato interno: Chromium JSON (bookmark_bar / other / synced).
  - Chromium → Chromium : copia JSON, IDs/GUIDs de raíz preservados,
                           GUIDs de contenido nuevos (Sync detecta cambios).
  - Chromium → Firefox  : JSON → SQLite con tombstones (Sync propaga borrados).
  - Firefox  → Chromium : SQLite → JSON con GUIDs nuevos.
  - Firefox  → Firefox  : SQLite → JSON → SQLite con tombstones.

Uso:
  browser-path             Menú interactivo (origen → destino)
  browser-path --dry-run   Simula sin modificar nada
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import random
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# ── Internationalisation (i18n) ───────────────────────────────────────────────

def _detect_lang() -> str:
    for var in ("LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        if os.environ.get(var, "").lower().startswith("es"):
            return "es"
    return "en"

_LANG = _detect_lang()

_STRINGS: dict[str, dict[str, str]] = {
    "title":              {"en": "Bookmark Synchronizer",
                           "es": "Sincronizador de Marcadores"},
    "dry_run_note":       {"en": "  [dry-run — no files will be modified]",
                           "es": "  [dry-run — no se realizará ningún cambio]"},
    "menu_src_browser":   {"en": "Select SOURCE browser:",
                           "es": "Selecciona el navegador ORIGEN:"},
    "menu_dst_browser":   {"en": "Select DESTINATION browser:",
                           "es": "Selecciona el navegador DESTINO:"},
    "lbl_src_profile":    {"en": "Source profile — {browser}:",
                           "es": "Perfil de origen — {browser}:"},
    "lbl_dst_profile":    {"en": "Destination profile — {browser}:",
                           "es": "Perfil de destino — {browser}:"},
    "msg_single_profile": {"en": "\n  ({browser}: single profile → {profile})",
                           "es": "\n  ({browser}: perfil único → {profile})"},
    "prompt_select":      {"en": "  Selection (q=quit): ",
                           "es": "  Selección (q=salir): "},
    "err_invalid_sel":    {"en": "  Number between 1 and {n}, or 'q' to quit.",
                           "es": "  Número entre 1 y {n}, o 'q' para salir."},
    "msg_cancelled":      {"en": "\nCancelled.",
                           "es": "\nCancelado."},
    "err_no_browsers":    {"en": "\n✗  No compatible browser found.",
                           "es": "\n✗  No se encontró ningún navegador compatible."},
    "err_no_profiles":    {"en": "\n✗  No profiles found for {browser}.",
                           "es": "\n✗  No se encontraron perfiles para {browser}."},
    "err_same_profile":   {"en": "\n✗  Source and destination are the same profile.",
                           "es": "\n✗  Origen y destino son el mismo perfil."},
    "lbl_src":            {"en": "  Source      : {browser} / {profile}",
                           "es": "  Origen      : {browser} / {profile}"},
    "lbl_dst":            {"en": "  Destination : {browser} / {profile}",
                           "es": "  Destino     : {browser} / {profile}"},
    "warn_overwrite":     {"en": "\n  ⚠  ALL destination bookmarks will be overwritten.",
                           "es": "\n  ⚠  TODOS los marcadores del destino serán sobreescritos."},
    "prompt_confirm":     {"en": "\n  Confirm (y/N): ",
                           "es": "\n  Confirmar (s/N): "},
    "err_dst_running":    {"en": "\n✗  {browser} is running.\n   Close it before continuing.",
                           "es": "\n✗  {browser} está en ejecución.\n   Ciérralo antes de continuar."},
    "lbl_backup":         {"en": "\n  Backup      : {path}",
                           "es": "\n  Backup      : {path}"},
    "msg_reading":        {"en": "\nReading source bookmarks...",
                           "es": "\nLeyendo marcadores de origen..."},
    "msg_read_total":     {"en": "  {n} item(s) in source root folders.",
                           "es": "  {n} elemento(s) en carpetas raíz del origen."},
    "msg_writing":        {"en": "\nWriting to destination...",
                           "es": "\nEscribiendo en destino..."},
    "err_read":           {"en": "\n✗  Error reading source: {e}",
                           "es": "\n✗  Error al leer origen: {e}"},
    "err_write":          {"en": "\n✗  Error: {e}",
                           "es": "\n✗  Error: {e}"},
    "msg_restored":       {"en": "  Destination restored from backup.",
                           "es": "  Destino restaurado desde backup."},
    "msg_done":           {"en": "{prefix}Synchronisation complete.",
                           "es": "{prefix}Sincronización completada."},
    "lbl_items":          {"en": "item(s)",
                           "es": "elemento(s)"},
    "msg_total":          {"en": "\n  Total — Bookmarks: {bm}  |  Folders: {fld}",
                           "es": "\n  Total — Marcadores: {bm}  |  Carpetas: {fld}"},
    "msg_restart":        {"en": "\nRestart {browser} to see the changes.",
                           "es": "\nReinicia {browser} para ver los cambios."},
    "msg_sync_ff":        {"en": "  → Firefox Sync will push the changes to the server on next start.",
                           "es": "  → Firefox Sync propagará los cambios al servidor en el próximo arranque."},
    "msg_sync_cr":        {"en": "  → Chrome/Brave Sync will detect the changes and update the server.",
                           "es": "  → Chrome/Brave Sync detectará los cambios y actualizará el servidor."},
    "prefix_dry":         {"en": "[dry-run] ", "es": "[dry-run] "},
    "prefix_ok":          {"en": "✓  ",        "es": "✓  "},
    "root_bar":           {"en": "Bookmarks bar",      "es": "Barra de marcadores"},
    "root_other":         {"en": "Menu / Other",       "es": "Menú / Otros"},
    "root_synced":        {"en": "No folder / Mobile", "es": "Sin carpeta / Móvil"},
}

def t(key: str, **kw) -> str:
    """Return translated string for key in the active language."""
    tmpl = _STRINGS.get(key, {}).get(_LANG) or _STRINGS.get(key, {}).get("en") or key
    return tmpl.format(**kw) if kw else tmpl


HOME        = Path.home()
APP_SUPPORT = HOME / "Library/Application Support"
_NOW_US     = int(time.time() * 1_000_000)
_EPOCH_DELTA = 11_644_473_600 * 1_000_000   # µs entre 1601-01-01 y 1970-01-01

# ── Definición de navegadores ─────────────────────────────────────────────────

@dataclass
class BrowserDef:
    name:   str
    btype:  str    # "chromium" | "firefox"
    app:    Path   # ruta a .app
    base:   Path   # directorio de datos del usuario
    proc:   str    # patrón para pgrep
    ff_dev: bool = False

BROWSERS: list[BrowserDef] = [
    BrowserDef("Brave",
               "chromium", Path("/Applications/Brave Browser.app"),
               APP_SUPPORT / "BraveSoftware/Brave-Browser", "Brave Browser"),
    BrowserDef("Google Chrome",
               "chromium", Path("/Applications/Google Chrome.app"),
               APP_SUPPORT / "Google/Chrome", "Google Chrome"),
    BrowserDef("Google Chrome Beta",
               "chromium", Path("/Applications/Google Chrome Beta.app"),
               APP_SUPPORT / "Google/Chrome Beta", "Google Chrome Beta"),
    BrowserDef("Google Chrome Dev",
               "chromium", Path("/Applications/Google Chrome Dev.app"),
               APP_SUPPORT / "Google/Chrome Dev", "Google Chrome Dev"),
    BrowserDef("Vivaldi",
               "chromium", Path("/Applications/Vivaldi.app"),
               APP_SUPPORT / "Vivaldi", "Vivaldi"),
    BrowserDef("Microsoft Edge",
               "chromium", Path("/Applications/Microsoft Edge.app"),
               APP_SUPPORT / "Microsoft Edge", "Microsoft Edge"),
    BrowserDef("Firefox",
               "firefox", Path("/Applications/Firefox.app"),
               APP_SUPPORT / "Firefox", "firefox", ff_dev=False),
    BrowserDef("Firefox Developer Edition",
               "firefox", Path("/Applications/Firefox Developer Edition.app"),
               APP_SUPPORT / "Firefox", "firefox", ff_dev=True),
]

def installed_browsers() -> list[BrowserDef]:
    return [b for b in BROWSERS if b.app.exists()]

# ── Perfiles ──────────────────────────────────────────────────────────────────

@dataclass
class Profile:
    browser: BrowserDef
    display: str
    path:    Path   # directorio del perfil

def _chromium_profiles(b: BrowserDef) -> list[Profile]:
    names: dict[str, str] = {}
    ls_path = b.base / "Local State"
    if ls_path.exists():
        try:
            ls = json.loads(ls_path.read_text(encoding="utf-8"))
            for k, v in ls.get("profile", {}).get("info_cache", {}).items():
                names[k] = v.get("name", k)
        except Exception:
            pass
    result: list[Profile] = []
    for dirname in ["Default"] + [f"Profile {i}" for i in range(1, 20)]:
        p = b.base / dirname
        if (p / "Bookmarks").exists():
            label = names.get(dirname, dirname)
            result.append(Profile(b, f"{label}  [{dirname}]", p))
    return result

def _firefox_profiles(b: BrowserDef) -> list[Profile]:
    ini = b.base / "profiles.ini"
    if not ini.exists():
        return []
    cfg = configparser.ConfigParser()
    cfg.read(ini)
    matches: list[Profile] = []
    fallback: list[Profile] = []
    for sec in cfg.sections():
        if not sec.startswith("Profile"):
            continue
        rel    = cfg.get(sec, "Path", fallback=None)
        if not rel:
            continue
        is_rel = cfg.get(sec, "IsRelative", fallback="0") == "1"
        path   = (b.base / rel) if is_rel else Path(rel)
        name   = cfg.get(sec, "Name", fallback=rel)
        is_def = cfg.get(sec, "Default", fallback="0") == "1"
        is_dev = "dev" in name.lower()
        if path.is_dir() and (path / "places.sqlite").exists():
            label = name + ("  ★" if is_def else "") + f"  [{path.name}]"
            prof  = Profile(b, label, path)
            (matches if is_dev == b.ff_dev else fallback).append(prof)
    return matches or fallback

def list_profiles(b: BrowserDef) -> list[Profile]:
    return _chromium_profiles(b) if b.btype == "chromium" else _firefox_profiles(b)

# ── Menú interactivo ──────────────────────────────────────────────────────────

def menu(title: str, options: list[str]) -> int:
    sep = "─" * max(len(title) + 4, 48)
    print(f"\n{sep}\n  {title}\n{sep}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    print()
    while True:
        try:
            raw = input(t("prompt_select")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n" + t("msg_cancelled")); sys.exit(0)
        if raw.lower() == "q":
            print(t("msg_cancelled")); sys.exit(0)
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print(t("err_invalid_sel", n=len(options)))

def pick_profile(prompt: str, b: BrowserDef) -> Profile:
    plist = list_profiles(b)
    if not plist:
        print(t("err_no_profiles", browser=b.name))
        sys.exit(1)
    if len(plist) == 1:
        print(t("msg_single_profile", browser=b.name, profile=plist[0].display))
        return plist[0]
    return plist[menu(prompt, [p.display for p in plist])]

# ── Utilidades ────────────────────────────────────────────────────────────────

_GUID_CHARS = string.ascii_letters + string.digits + "_-"

def new_guid() -> str:
    return "".join(random.choices(_GUID_CHARS, k=12))

def chrome_ts_to_prtime(ts) -> int:
    """Chrome µs (desde 1601) → Firefox PRTime µs (desde 1970)."""
    try:
        v = int(ts or 0)
    except (ValueError, TypeError):
        v = 0
    r = v - _EPOCH_DELTA
    return r if r > 0 else _NOW_US

def prtime_to_chrome_ts(pt: int) -> int:
    """Firefox PRTime µs (desde 1970) → Chrome µs (desde 1601)."""
    return (pt + _EPOCH_DELTA) if pt and pt > 0 else 0

def now_as_chrome_ts() -> int:
    return _NOW_US + _EPOCH_DELTA

def rev_host(url: str) -> str | None:
    try:
        h = urlparse(url).hostname
        return (h[::-1] + ".") if h else None
    except Exception:
        return None

def url_hash(url: str) -> int:
    h = 0
    for ch in url:
        h = (h * 31 + ord(ch)) & 0x7FFF_FFFF_FFFF_FFFF
    return h

def chromium_checksum(roots: dict) -> str:
    """
    MD5 sobre nodos tipo 'url' (id + title + url), igual que Chrome.
    Si difiere del valor esperado, Chrome lo reescribe en el próximo guardado
    sin rechazar el archivo.
    """
    md5 = hashlib.md5()
    def visit(node: dict) -> None:
        if node.get("type") == "url":
            nid = node.get("id", "")
            if nid:
                md5.update(nid.encode("utf-8"))
            md5.update(node.get("name", "").encode("utf-8"))
            md5.update(node.get("url",  "").encode("utf-8"))
        for child in node.get("children", []):
            visit(child)
    for r in ("bookmark_bar", "other", "synced"):
        if r in roots:
            visit(roots[r])
    return md5.hexdigest()

# URLs que no tienen sentido fuera del navegador de origen
_SKIP_FF  = ("javascript:", "data:")
_SKIP_CR  = ("javascript:", "data:")

# ── Lectura → formato interno (Chromium JSON) ─────────────────────────────────

def read_roots(p: Profile) -> dict:
    return _read_chromium(p) if p.browser.btype == "chromium" else _read_firefox(p)

def _read_chromium(p: Profile) -> dict:
    data = json.loads((p.path / "Bookmarks").read_text(encoding="utf-8"))
    return data.get("roots", {})

def _read_firefox(p: Profile) -> dict:
    """
    Lee places.sqlite (copia temporal para seguridad) y convierte
    al formato de roots Chromium JSON.
    """
    tmp = Path(tempfile.mktemp(suffix=".sqlite"))
    shutil.copy2(p.path / "places.sqlite", tmp)
    try:
        conn    = sqlite3.connect(str(tmp))
        cur     = conn.cursor()
        counter = [3]   # IDs 1–3 reservados para raíces

        def build_node(ff_id: int, name: str) -> dict:
            cur.execute("""
                SELECT b.id, b.type, b.title, b.dateAdded, b.lastModified, p.url
                FROM moz_bookmarks b
                LEFT JOIN moz_places p ON b.fk = p.id
                WHERE b.parent = ? ORDER BY b.position
            """, (ff_id,))
            children = []
            for bid, btype, title, da, dm, url in cur.fetchall():
                counter[0] += 1
                nid = str(counter[0])
                ts  = str(prtime_to_chrome_ts(da or 0))
                if btype == 1 and url:
                    if any(url.startswith(s) for s in ("javascript:", "data:")):
                        continue
                    children.append({
                        "id": nid, "guid": new_guid(), "type": "url",
                        "name": title or "", "url": url,
                        "date_added": ts, "date_modified": "0",
                    })
                elif btype == 2:
                    sub = build_node(bid, title or "")
                    sub["id"] = nid
                    children.append(sub)
            return {
                "id": "0", "guid": new_guid(), "type": "folder",
                "name": name, "children": children,
                "date_added":    str(now_as_chrome_ts()),
                "date_modified": str(now_as_chrome_ts()),
            }

        FF_GUID_MAP = {
            "toolbar_____": "bookmark_bar",
            "menu________": "other",
            "unfiled_____": "synced",
        }
        ROOT_IDS = {"bookmark_bar": "1", "other": "2", "synced": "3"}
        roots: dict = {}
        for ff_guid, cr_name in FF_GUID_MAP.items():
            cur.execute("SELECT id FROM moz_bookmarks WHERE guid = ?", (ff_guid,))
            row = cur.fetchone()
            if row:
                node = build_node(row[0], cr_name)
                node["id"] = ROOT_IDS[cr_name]
                roots[cr_name] = node
        conn.close()
        return roots
    finally:
        tmp.unlink(missing_ok=True)

# ── Escritura de marcadores ───────────────────────────────────────────────────

def write_roots(p: Profile, roots: dict, dry: bool) -> tuple[int, int]:
    return (_write_chromium(p, roots, dry) if p.browser.btype == "chromium"
            else _write_firefox(p, roots, dry))

def _write_chromium(p: Profile, new_roots: dict, dry: bool) -> tuple[int, int]:
    """
    Escribe el archivo Bookmarks de Chrome/Brave/Vivaldi.

    Persistencia con Sync:
    · GUIDs de carpetas RAÍZ preservados → Sync sigue identificándolas.
    · GUIDs de CONTENIDO nuevos → Sync los sube como ítems nuevos.
    · GUIDs del contenido anterior, al desaparecer del archivo local,
      son detectados por el cliente Sync (vía LevelDB) como eliminados
      localmente → se propagan como borrados al servidor.
    """
    bm_path = p.path / "Bookmarks"

    # Preservar metadatos de raíces existentes
    root_meta: dict[str, dict] = {}
    if bm_path.exists():
        try:
            ex = json.loads(bm_path.read_text(encoding="utf-8"))
            for rname, rnode in ex.get("roots", {}).items():
                if isinstance(rnode, dict):
                    root_meta[rname] = {k: rnode.get(k)
                                        for k in ("id", "guid", "date_added")}
        except Exception:
            pass

    max_id = max(
        (int(m["id"]) for m in root_meta.values()
         if str(m.get("id", "")).isdigit()),
        default=3,
    )
    counter = [max_id]
    stats   = [0, 0]   # [bookmarks, folders]

    def assign_ids(node: dict) -> None:
        counter[0] += 1
        node["id"]   = str(counter[0])
        node["guid"] = new_guid()         # GUID nuevo → Sync lo trata como ítem nuevo
        if node.get("type") == "url":
            stats[0] += 1
        elif node.get("type") == "folder":
            stats[1] += 1
            for child in node.get("children", []):
                assign_ids(child)

    ROOT_DISPLAY = {
        "bookmark_bar": "Bookmarks bar",
        "other":        "Other bookmarks",
        "synced":       "Mobile bookmarks",
    }
    ROOT_DEFAULT_ID = {"bookmark_bar": "1", "other": "2", "synced": "3"}
    ts = str(now_as_chrome_ts())

    built: dict = {}
    for rname, display in ROOT_DISPLAY.items():
        meta     = root_meta.get(rname, {})
        children = [c for c in new_roots.get(rname, {}).get("children", [])
                    if c.get("type") != "url"
                    or not any(c.get("url", "").startswith(s) for s in _SKIP_CR)]
        for child in children:
            assign_ids(child)
        built[rname] = {
            "children":       children,
            "date_added":     meta.get("date_added") or ts,
            "date_last_used": "0",
            "date_modified":  ts,
            "guid":           meta.get("guid") or new_guid(),   # raíz preservada
            "id":             meta.get("id") or ROOT_DEFAULT_ID[rname],
            "name":           display,
            "source":         0,
            "type":           "folder",
        }

    if not dry:
        data = {"checksum": chromium_checksum(built), "roots": built, "version": 1}
        bm_path.write_text(json.dumps(data, ensure_ascii=False, indent=3),
                           encoding="utf-8")
    return stats[0], stats[1]

def _write_firefox(p: Profile, roots: dict, dry: bool) -> tuple[int, int]:
    """
    Escribe en places.sqlite.

    Persistencia con Firefox Sync:
    · Tombstones en moz_bookmarks_deleted → Sync propaga los borrados al servidor.
    · syncChangeCounter=1 en nuevos ítems → Sync los sube al servidor.
    · syncChangeCounter en carpetas raíz  → Sync sabe que el contenido cambió.
    """
    db    = p.path / "places.sqlite"
    stats = [0, 0]

    if dry:
        def _count(node: dict) -> None:
            if node.get("type") == "url":
                stats[0] += 1
            elif node.get("type") == "folder":
                stats[1] += 1
                for c in node.get("children", []):
                    _count(c)
        for r in ("bookmark_bar", "other", "synced"):
            for c in roots.get(r, {}).get("children", []):
                _count(c)
        return stats[0], stats[1]

    conn = sqlite3.connect(str(db))
    cur  = conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=OFF")

        FF_GUID_MAP = {
            "bookmark_bar": "toolbar_____",
            "other":        "menu________",
            "synced":       "unfiled_____",
        }
        folder_ids: dict[str, int] = {}
        for cr, ff_guid in FF_GUID_MAP.items():
            cur.execute("SELECT id FROM moz_bookmarks WHERE guid = ?", (ff_guid,))
            row = cur.fetchone()
            if row:
                folder_ids[cr] = row[0]

        now_us = int(time.time() * 1_000_000)

        def clear_folder(fid: int) -> None:
            cur.execute(
                "SELECT id, type, guid FROM moz_bookmarks"
                " WHERE parent = ? ORDER BY position", (fid,),
            )
            for cid, ctype, cguid in cur.fetchall():
                if ctype == 2:
                    clear_folder(cid)
                if cguid:
                    cur.execute(
                        "INSERT OR REPLACE INTO moz_bookmarks_deleted"
                        " (guid, dateRemoved) VALUES (?,?)", (cguid, now_us),
                    )
                cur.execute("DELETE FROM moz_bookmarks WHERE id = ?", (cid,))
            # Marcar carpeta raíz como modificada para que Sync lo suba
            cur.execute(
                "UPDATE moz_bookmarks"
                " SET syncChangeCounter = syncChangeCounter + 1 WHERE id = ?", (fid,),
            )

        def get_or_create_place(url: str, title: str | None) -> int:
            cur.execute("SELECT id FROM moz_places WHERE url = ?", (url,))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                "INSERT INTO moz_places"
                " (url, title, rev_host, url_hash, frecency, hidden, typed,"
                "  visit_count, guid, foreign_count, recalc_frecency, recalc_alt_frecency)"
                " VALUES (?,?,?,?,-1,0,0,0,?,0,0,0)",
                (url, title, rev_host(url), url_hash(url), new_guid()),
            )
            return cur.lastrowid

        def insert_node(node: dict, parent_id: int, pos: int) -> None:
            ntype    = node.get("type")
            name     = (node.get("name") or "")[:512]
            date_add = chrome_ts_to_prtime(node.get("date_added",    0))
            date_mod = chrome_ts_to_prtime(node.get("date_modified", 0))
            if ntype == "url":
                url = node.get("url", "")
                if not url or any(url.startswith(s) for s in _SKIP_FF):
                    return
                pid = get_or_create_place(url, name)
                cur.execute(
                    "INSERT INTO moz_bookmarks"
                    " (type, fk, parent, position, title, dateAdded, lastModified,"
                    "  guid, syncStatus, syncChangeCounter)"
                    " VALUES (1,?,?,?,?,?,?,?,0,1)",
                    (pid, parent_id, pos, name, date_add, date_mod, new_guid()),
                )
                cur.execute(
                    "UPDATE moz_places"
                    " SET foreign_count = foreign_count + 1 WHERE id = ?", (pid,),
                )
                stats[0] += 1
            elif ntype == "folder":
                cur.execute(
                    "INSERT INTO moz_bookmarks"
                    " (type, fk, parent, position, title, dateAdded, lastModified,"
                    "  guid, syncStatus, syncChangeCounter)"
                    " VALUES (2,NULL,?,?,?,?,?,?,0,1)",
                    (parent_id, pos, name, date_add, date_mod, new_guid()),
                )
                fid = cur.lastrowid
                stats[1] += 1
                for i, child in enumerate(node.get("children", [])):
                    insert_node(child, fid, i)

        for cr, fid in folder_ids.items():
            clear_folder(fid)
        for cr, fid in folder_ids.items():
            for i, child in enumerate(roots.get(cr, {}).get("children", [])):
                insert_node(child, fid, i)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return stats[0], stats[1]

# ── Verificación de procesos ──────────────────────────────────────────────────

def is_running(b: BrowserDef) -> bool:
    for pattern in [b.proc, b.app.stem]:
        if subprocess.run(
            ["pgrep", "-fi", pattern],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            return True
    return False

# ── Main flow ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=t("title"))
    parser.add_argument("--dry-run", action="store_true",
                        help=t("dry_run_note").strip())
    dry = parser.parse_args().dry_run

    title = t("title")
    print("\n╔══════════════════════════════════════════╗")
    print(f"║{title.center(42)}║")
    print("╚══════════════════════════════════════════╝")
    if dry:
        print(t("dry_run_note"))

    browsers = installed_browsers()
    if not browsers:
        print(t("err_no_browsers"))
        sys.exit(1)

    labels = [b.name for b in browsers]

    # ── Source ────────────────────────────────────────────────────────────
    src_b = browsers[menu(t("menu_src_browser"), labels)]
    src_p = pick_profile(t("lbl_src_profile", browser=src_b.name), src_b)

    # ── Destination ───────────────────────────────────────────────────────
    dst_b = browsers[menu(t("menu_dst_browser"), labels)]
    dst_p = pick_profile(t("lbl_dst_profile", browser=dst_b.name), dst_b)

    if src_p.path == dst_p.path:
        print(t("err_same_profile"))
        sys.exit(1)

    # ── Summary and confirmation ───────────────────────────────────────────
    print(f"\n{t('lbl_src', browser=src_b.name, profile=src_p.display)}")
    print(t("lbl_dst", browser=dst_b.name, profile=dst_p.display))
    print(t("warn_overwrite"))

    if not dry:
        try:
            ok = input(t("prompt_confirm")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n" + t("msg_cancelled")); sys.exit(0)
        if ok not in ("s", "si", "sí", "y", "yes"):
            print(t("msg_cancelled")); sys.exit(0)

        # Only destination must be closed; source is read safely
        if is_running(dst_p.browser):
            print(t("err_dst_running", browser=dst_p.browser.name))
            sys.exit(1)

    # ── Backup ─────────────────────────────────────────────────────────────
    backup: Path | None = None
    if not dry:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        orig = (dst_p.path / "Bookmarks" if dst_b.btype == "chromium"
                else dst_p.path / "places.sqlite")
        backup = Path(str(orig) + f".bak_{ts}")
        shutil.copy2(orig, backup)
        print(t("lbl_backup", path=backup))

    # ── Read source ────────────────────────────────────────────────────────
    print(t("msg_reading"))
    try:
        roots = read_roots(src_p)
    except Exception as e:
        print(t("err_read", e=e))
        sys.exit(1)

    roots_info = [
        ("bookmark_bar", t("root_bar")),
        ("other",        t("root_other")),
        ("synced",       t("root_synced")),
    ]
    total = sum(len(roots.get(r, {}).get("children", [])) for r, _ in roots_info)
    print(t("msg_read_total", n=total))

    # ── Write destination ───────────────────────────────────────────────────
    print(t("msg_writing"))
    try:
        n_bm, n_fld = write_roots(dst_p, roots, dry)
    except Exception as e:
        if backup:
            orig = (dst_p.path / "Bookmarks" if dst_b.btype == "chromium"
                    else dst_p.path / "places.sqlite")
            shutil.copy2(backup, orig)
            print(t("msg_restored"))
        print(t("err_write", e=e))
        import traceback; traceback.print_exc()
        sys.exit(1)

    # ── Result ─────────────────────────────────────────────────────────────
    prefix = t("prefix_dry") if dry else t("prefix_ok")
    print(f"\n{t('msg_done', prefix=prefix)}")
    for r_name, r_label in roots_info:
        n = len(roots.get(r_name, {}).get("children", []))
        print(f"  {r_name:<16} → {r_label:<26} {n:>4} {t('lbl_items')}")
    print(t("msg_total", bm=n_bm, fld=n_fld))

    if not dry:
        print(t("msg_restart", browser=dst_b.name))
        if dst_b.btype == "firefox":
            print(t("msg_sync_ff"))
        else:
            print(t("msg_sync_cr"))

if __name__ == "__main__":
    main()
