"""Official Raspberry Pi OS images: list, download, verify, manage.

Sources:
  - the OS list Raspberry Pi Imager uses (config `catalog_url`): the current
    releases, with the SHA-256 and size of the *extracted* .img;
  - https://downloads.raspberrypi.com/<series>/images/: every release of every
    series (`--archive`); each image there has a published `<file>.sha256`
    with the SHA-256 of the *download* (.img.xz).

Downloads go to the configured images folder (`piimg config images_dir`),
resume after an interruption (`<file>.part` and an HTTP Range request), are
hashed while they download and checked against the published .sha256 before
they get their final name. What is known about each file (where it came from,
the published hashes, when it was last verified) is kept in
`<images_dir>/.piimg-images.json`.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, progress
from .errors import PiImgError
from .output import Table

BASE = "https://downloads.raspberrypi.com"
OFFICIAL_HOST = "downloads.raspberrypi.com"
USER_AGENT = "piimg (Raspberry Pi OS image tool)"
INDEX_NAME = ".piimg-images.json"
IMAGE_SUFFIXES = (".img.xz", ".img", ".zip", ".img.gz")
CHUNK = 1 << 20


# ------------------------------------------------------------------ http

def _open(url, start=0, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if start:
        req.add_header("Range", f"bytes={start}-")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise PiImgError(f"{url}: HTTP {e.code} {e.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PiImgError(f"cannot reach {url}: {getattr(e, 'reason', e)}")


def _get_text(url, timeout=30):
    with _open(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def published_sha256(url):
    """The SHA-256 published next to a download (`<url>.sha256`), or None."""
    try:
        text = _get_text(url + ".sha256")
    except PiImgError:
        return None
    m = re.match(r"\s*([0-9a-fA-F]{64})", text)
    return m.group(1).lower() if m else None


# ------------------------------------------------------------------ catalog

def _slug(url):
    name = url.rsplit("/", 1)[-1]
    for s in IMAGE_SUFFIXES:
        if name.endswith(s):
            return name[: -len(s)]
    return name


def _series(url):
    m = re.search(r"downloads\.raspberrypi\.com/([^/]+)/images/", url)
    return m.group(1) if m else ""


def fetch_catalog(refresh=False):
    """Every image in the Imager OS list (sublists included), as dicts with
    name, group, url, slug, series, release_date, size (download), extract_size,
    extract_sha256, devices, official. Cached in ~/.cache/piimg/."""
    cache = config.cache_dir() / "os_list.json"
    max_age = float(config.get("catalog_max_age_hours")) * 3600
    data = None
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        try:
            data = json.loads(cache.read_text())
        except ValueError:
            data = None
    if data is None:
        url = config.get("catalog_url")
        with progress.task("Fetching the Raspberry Pi OS image list"):
            text = _get_text(url)
        try:
            data = json.loads(text)
        except ValueError:
            raise PiImgError(f"{url} did not return a JSON OS list")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
    out = []

    def walk(items, group):
        for it in items:
            if it.get("subitems"):
                walk(it["subitems"], it.get("name", ""))
            elif it.get("url") and it["url"].startswith("http"):
                url = it["url"]
                out.append({
                    "name": it.get("name", ""), "group": group, "url": url, "slug": _slug(url),
                    "series": _series(url), "release_date": it.get("release_date", ""),
                    "size": it.get("image_download_size"), "extract_size": it.get("extract_size"),
                    "extract_sha256": it.get("extract_sha256"), "download_sha256": it.get("image_download_sha256"),
                    "devices": it.get("devices") or [], "description": it.get("description", ""),
                    "official": url.split("/")[2] == OFFICIAL_HOST and "/raspios" in url,
                })
    walk(data.get("os_list", []), "")
    return out


def cached_catalog_slugs():
    """Slugs of official images from the cached list, without touching the network (for the TUI)."""
    cache = config.cache_dir() / "os_list.json"
    if not cache.exists():
        return []
    try:
        return [e["slug"] for e in fetch_catalog() if e["official"]]
    except PiImgError:
        return []


def archive_releases(series_filter=None):
    """Every release of every official series, from the download server's index pages."""
    with progress.task("Reading the release archive of downloads.raspberrypi.com") as t:
        top = _get_text(BASE + "/")
        series = sorted(set(re.findall(r'href="(raspios[a-z0-9_]*)/"', top)))
        if series_filter:
            series = [s for s in series if series_filter in s]
        t.update(0, total=len(series))
        out = []
        for n, s in enumerate(series):
            page = _get_text(f"{BASE}/{s}/images/")
            for d in sorted(set(re.findall(rf'href="({re.escape(s)}-(\d{{4}}-\d{{2}}-\d{{2}}))/"', page))):
                out.append({"series": s, "release_date": d[1], "dir": f"{BASE}/{s}/images/{d[0]}/"})
            t.update(n + 1, note=s)
    return out


def resolve_archive(entry):
    """Find the image file inside an archive release directory."""
    page = _get_text(entry["dir"])
    files = [f for f in re.findall(r'href="([^"?/]+\.img\.xz)"', page)] or \
            [f for f in re.findall(r'href="([^"?/]+\.zip)"', page)]
    if not files:
        raise PiImgError(f"no image file in {entry['dir']}")
    url = entry["dir"] + files[0]
    return {"name": f"{entry['series']} {entry['release_date']}", "group": "archive", "url": url,
            "slug": _slug(url), "series": entry["series"], "release_date": entry["release_date"],
            "size": None, "extract_size": None, "extract_sha256": None, "download_sha256": None,
            "devices": [], "description": "", "official": True}


def find(spec, refresh=False):
    """An image by slug, file name, URL or a unique part of its slug ('trixie-arm64-lite').
    Official images only, unless the spec is a full URL. 'SERIES@DATE' picks an archive release."""
    if spec.startswith(("http://", "https://")):
        return {"name": _slug(spec), "group": "", "url": spec, "slug": _slug(spec), "series": _series(spec),
                "release_date": "", "size": None, "extract_size": None, "extract_sha256": None,
                "download_sha256": None, "devices": [], "description": "",
                "official": spec.split("/")[2] == OFFICIAL_HOST}
    m = re.fullmatch(r"(raspios[a-z0-9_]*)@(\d{4}-\d{2}-\d{2})", spec)
    if m:
        rel = [r for r in archive_releases(m.group(1)) if r["series"] == m.group(1) and r["release_date"] == m.group(2)]
        if not rel:
            raise PiImgError(f"no release {m.group(2)} of {m.group(1)} (see `piimg images list --archive`)")
        return resolve_archive(rel[0])
    cat = [e for e in fetch_catalog(refresh) if e["official"]]
    want = _slug(spec)
    exact = [e for e in cat if e["slug"] == want]
    if exact:
        return exact[0]
    part = [e for e in cat if want in e["slug"]]
    if len(part) == 1:
        return part[0]
    if not part:
        raise PiImgError(f"no official image matches {spec!r} (see `piimg images list`)")
    raise PiImgError(f"{spec!r} matches several images: " + ", ".join(e["slug"] for e in part))


def catalog_table(show_all=False, refresh=False, archive=False, series=None):
    idx = Index()
    if archive:
        t = Table(["image", "series", "release", "downloaded"], title="Official releases (archive)")
        for r in archive_releases(series):
            spec = f"{r['series']}@{r['release_date']}"
            have = any(e.get("series") == r["series"] and e.get("release_date") == r["release_date"]
                       for e in idx.entries.values())
            t.add(spec, r["series"], r["release_date"], "yes" if have else "")
        t.footer = "download one with: piimg images download SERIES@DATE"
        return t
    t = Table(["image", "name", "release", "download", "unpacked", "for", "status"],
              title="Raspberry Pi OS images" if not show_all else "All images in the Imager list")
    for e in fetch_catalog(refresh):
        if not (e["official"] or show_all):
            continue
        status = idx.status_for(e)
        devices = ", ".join(sorted({re.sub(r"-(32|64)bit$", "", d) for d in e["devices"]}))
        t.add(e["slug"] if e["official"] else e["url"], (e["group"] + ": " if e["group"] and not e["official"]
                                                         else "") + e["name"],
              e["release_date"], progress.fmt_size(e["size"]) if e["size"] else "",
              progress.fmt_size(e["extract_size"]) if e["extract_size"] else "", devices, status)
    t.footer = (f"images folder: {config.images_dir()} — download with: piimg images download IMAGE "
                "(a unique part of the name is enough, e.g. trixie-arm64-lite)")
    return t


# ------------------------------------------------------------------ the local index

class Index:
    """<images_dir>/.piimg-images.json: file name -> what we know about it."""

    def __init__(self, folder=None):
        self.dir = Path(folder or config.images_dir())
        self.path = self.dir / INDEX_NAME
        try:
            self.entries = json.loads(self.path.read_text()) if self.path.exists() else {}
        except ValueError:
            self.entries = {}

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".part")
        tmp.write_text(json.dumps(self.entries, indent=1, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def record(self, filename, **info):
        e = self.entries.setdefault(filename, {})
        e.update({k: v for k, v in info.items() if v is not None})
        self.save()

    def forget(self, filename):
        if self.entries.pop(filename, None) is not None:
            self.save()

    def status_for(self, cat_entry):
        name = cat_entry["url"].rsplit("/", 1)[-1]
        img = cat_entry["slug"] + ".img"
        bits = []
        for f, label in ((name, "downloaded"), (img, "unpacked")):
            p = self.dir / f
            if p.exists():
                e = self.entries.get(f, {})
                ok = e.get("verified_sha256") and e.get("verified_size") == p.stat().st_size \
                    and e.get("verified_mtime") == p.stat().st_mtime_ns
                bits.append(label + (" ✔" if ok else ""))
            elif (self.dir / (f + ".part")).exists():
                bits.append("partial download")
        return ", ".join(bits)


# ------------------------------------------------------------------ hashing

def sha256_file(path, description=None):
    path = Path(path)
    h = hashlib.sha256()
    total = path.stat().st_size
    with progress.task(description or f"Checking {path.name}", total=total, as_bytes=True) as t, \
            open(path, "rb") as f:
        done = 0
        while chunk := f.read(4 * CHUNK):
            h.update(chunk)
            done += len(chunk)
            t.update(done)
    return h.hexdigest()


# ------------------------------------------------------------------ download

def download(spec, unpack=False, refresh=False, folder=None):
    """Download an official image to the images folder, verified. Returns a report."""
    e = find(spec, refresh)
    folder = Path(folder or config.images_dir())
    folder.mkdir(parents=True, exist_ok=True)
    idx = Index(folder)
    name = e["url"].rsplit("/", 1)[-1]
    dest, part = folder / name, folder / (name + ".part")
    expected = e.get("download_sha256") or published_sha256(e["url"])
    if expected is None:
        raise PiImgError(f"no published SHA-256 for {e['url']}; refusing to download unverifiable data")
    report = []
    if dest.exists():
        report.append(f"{name} is already downloaded")
    else:
        h = hashlib.sha256()
        start = part.stat().st_size if part.exists() else 0
        if start:
            with open(part, "rb") as f:  # resume: the hash must cover what is already there
                while chunk := f.read(4 * CHUNK):
                    h.update(chunk)
        with _open(e["url"], start=start, timeout=60) as r:
            if start and r.status != 206:  # server ignored Range: start again
                start, h = 0, hashlib.sha256()
            length = r.headers.get("Content-Length")
            total = (start + int(length)) if length else e.get("size")
            with progress.task(f"Downloading {name}", total=total, as_bytes=True, stall=(30, 120)) as t, \
                    open(part, "ab" if start else "wb") as out:
                done = start
                t.update(done)
                while chunk := r.read(CHUNK):
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    t.update(done)
                t.result = progress.fmt_size(done)
        got = h.hexdigest()
        if got != expected:
            part.unlink(missing_ok=True)
            raise PiImgError(f"{name}: SHA-256 mismatch (got {got}, published {expected}); the download "
                             "was deleted, try again")
        part.rename(dest)
        report.append(f"downloaded {dest} ({progress.fmt_size(dest.stat().st_size)}), SHA-256 verified")
    st = dest.stat()
    idx.record(name, url=e["url"], name=e["name"], series=e["series"], release_date=e["release_date"],
               published_sha256=expected, extract_sha256=e.get("extract_sha256"),
               **({"verified_sha256": expected, "verified_size": st.st_size, "verified_mtime": st.st_mtime_ns,
                   "verified_at": time.strftime("%Y-%m-%d %H:%M")} if "verified" in report[-1] else {}))
    if unpack:
        report.append(_unpack(dest, e, idx))
    return "\n".join(report)


def _unpack(src, e, idx):
    from .image import unpack as do_unpack
    img = src.parent / (e["slug"] + ".img")
    if img.exists():
        return f"{img.name} is already unpacked"
    do_unpack(src, img)
    msg = f"unpacked to {img}"
    if e.get("extract_sha256"):
        got = sha256_file(img, f"Checking {img.name}")
        if got != e["extract_sha256"]:
            return msg + f"\nWARNING: {img.name} SHA-256 {got} does not match the published {e['extract_sha256']}"
        st = img.stat()
        idx.record(img.name, url=e["url"], name=e["name"], series=e["series"], release_date=e["release_date"],
                   extract_sha256=e["extract_sha256"], verified_sha256=got, verified_size=st.st_size,
                   verified_mtime=st.st_mtime_ns, verified_at=time.strftime("%Y-%m-%d %H:%M"))
        msg += ", SHA-256 verified"
    return msg


# ------------------------------------------------------------------ local files

def _local_files(folder):
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and (p.name.endswith(IMAGE_SUFFIXES) or p.name.endswith(".part"))
                  and not p.name.startswith("."))


def local_table():
    folder = config.images_dir()
    idx = Index(folder)
    t = Table(["file", "size", "release", "image", "verified", "note"], title=f"Images in {folder}")
    latest = {}
    try:
        for e in fetch_catalog():
            if e["official"]:
                latest[e["series"]] = e
    except PiImgError:
        pass  # offline: no "newer release" hints
    for p in _local_files(folder):
        info = idx.entries.get(p.name, {})
        st = p.stat()
        ok = info.get("verified_sha256") and info.get("verified_size") == st.st_size \
            and info.get("verified_mtime") == st.st_mtime_ns
        verified = (f"✔ {info.get('verified_at', '')}" if ok else
                    "changed since verified" if info.get("verified_sha256") else "")
        note = ""
        if p.name.endswith(".part"):
            note = "partial download: run the same download again to resume"
        series = info.get("series") or _series_from_name(p.name)
        if series in latest and info.get("release_date") and latest[series]["release_date"] > info["release_date"]:
            note = f"newer release: {latest[series]['release_date']}"
        if p.suffix == ".img" and info.get("verified_sha256") and not ok:
            note = note or "modified since unpacked (used as a writable image?)"
        t.add(p.name, progress.fmt_size(st.st_size), info.get("release_date") or _date_from_name(p.name),
              info.get("name", ""), verified, note)
    t.footer = "verify with: piimg images verify FILE; delete with: piimg images delete FILE"
    return t


def _date_from_name(name):
    m = re.match(r"(\d{4}-\d{2}-\d{2})-", name)
    return m.group(1) if m else ""


def _series_from_name(name):
    url = _guess_url(name)
    return _series(url) if url else ""


def _local_path(name):
    folder = config.images_dir()
    p = Path(name)
    if not p.is_absolute() and len(p.parts) == 1:
        p = folder / p
    p = p.resolve()
    if p.parent != folder.resolve():
        raise PiImgError(f"{name}: only files in the images folder ({folder}) are managed here")
    if not p.exists():
        raise PiImgError(f"{name}: no such file in {folder}")
    return p


def verify(names):
    """Check local images against their published SHA-256 (fetched if not known yet)."""
    idx = Index()
    t = Table(["file", "status", "detail"], title="Verification")
    for name in names:
        p = _local_path(name)
        info = idx.entries.setdefault(p.name, {})
        expected, source = None, ""
        if p.name.endswith(".img"):
            expected, source = info.get("extract_sha256"), "Imager list (extracted image)"
            if not expected:
                e = next((e for e in fetch_catalog() if e["slug"] + ".img" == p.name), None)
                expected = e and e.get("extract_sha256")
        else:
            expected, source = info.get("published_sha256"), "published .sha256"
            if not expected:
                url = info.get("url")
                if not url:
                    e = next((e for e in fetch_catalog() if e["url"].endswith("/" + p.name)), None)
                    url = e and e["url"]
                if not url:
                    url = _guess_url(p.name)
                expected = url and published_sha256(url)
                if expected:
                    info.setdefault("url", url)
        if not expected:
            t.add(p.name, "warn", "no published SHA-256 known for this file (not an official image, "
                                  "or an older release whose extracted hash is not published)")
            continue
        got = sha256_file(p)
        st = p.stat()
        if got == expected:
            idx.record(p.name, **{k: v for k, v in info.items() if k in ("url", "name", "series", "release_date")},
                       published_sha256=info.get("published_sha256") or (expected if not p.name.endswith(".img")
                                                                         else None),
                       extract_sha256=expected if p.name.endswith(".img") else info.get("extract_sha256"),
                       verified_sha256=got, verified_size=st.st_size, verified_mtime=st.st_mtime_ns,
                       verified_at=time.strftime("%Y-%m-%d %H:%M"))
            t.add(p.name, "ok", f"SHA-256 matches the {source}")
        else:
            for k in ("verified_sha256", "verified_size", "verified_mtime", "verified_at"):
                info.pop(k, None)
            idx.save()
            t.add(p.name, "FAIL", f"SHA-256 {got} != {expected} ({source})")
    return t


def _guess_url(filename):
    """Official file names encode their series: 2025-05-13-raspios-bookworm-arm64-lite.img.xz
    lives in raspios_lite_arm64/images/raspios_lite_arm64-2025-05-13/."""
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-raspios-([a-z]+)-(arm64|armhf)(?:-(lite|full))?\.img\.xz", filename)
    if not m:
        return None
    date, _codename, arch, flavour = m.groups()
    series = "raspios_" + (f"{flavour}_" if flavour else "") + arch
    return f"{BASE}/{series}/images/{series}-{date}/{filename}"


def delete(names):
    idx = Index()
    out = []
    for name in names:
        p = _local_path(name)
        p.unlink()
        idx.forget(p.name)
        out.append(f"deleted {p}")
    return "\n".join(out)
