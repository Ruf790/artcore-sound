"""The names of a disc's album and tracks from MusicBrainz (2.0): by the disc ID, else by the table of contents (a
near match). Free, no key; the data is CC0. Only the IDs and times of the disc are sent."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .rip import Meta

URL = "https://musicbrainz.org/ws/2/discid/{id}?{query}"
TIMEOUT = 15


def user_agent() -> str:
    from .. import VERSION
    return f"ArtCoreSound/{VERSION} ( https://github.com/Ruf790/artcore-sound )"


def _get(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def lookup(toc) -> list[Meta]:
    """The releases this disc may be (best first); [] — unknown. Raises OSError without the network."""
    q = urllib.parse.urlencode({"inc": "recordings+artist-credits", "fmt": "json", "cdstubs": "no"}, safe="+")
    data = _get(URL.format(id=toc.musicbrainz_id(), query=q))
    if not data or not data.get("releases"):
        q = urllib.parse.urlencode({"toc": toc.musicbrainz_toc(), "inc": "recordings+artist-credits", "fmt": "json",
                                    "cdstubs": "no"}, safe="+")
        data = _get(URL.format(id="-", query=q))
    return parse(data or {}, toc.musicbrainz_id(), len(toc.audio))


def _credit(ac) -> str:
    return "".join((c.get("name") or c.get("artist", {}).get("name", "")) + (c.get("joinphrase") or "")
                   for c in ac or [])


def parse(data: dict, disc_id: str, n_tracks: int) -> list[Meta]:
    out = []
    for rel in data.get("releases") or []:
        media = rel.get("media") or []
        medium = next((m for m in media if any(d.get("id") == disc_id for d in m.get("discs") or [])), None)
        if medium is None:
            medium = next((m for m in media if len(m.get("tracks") or []) == n_tracks), None)
        if medium is None:
            continue
        artist = _credit(rel.get("artist-credit"))
        tracks = []
        for tr in sorted(medium.get("tracks") or [], key=lambda x: int(x.get("position") or 0)):
            ta = _credit(tr.get("artist-credit")) or _credit((tr.get("recording") or {}).get("artist-credit"))
            tracks.append({"title": tr.get("title") or (tr.get("recording") or {}).get("title", ""),
                           "artist": ta if ta and ta != artist else ""})
        m = Meta(album=rel.get("title", ""), artist=artist, date=(rel.get("date") or "")[:4], tracks=tracks)
        m.source = " · ".join(x for x in (rel.get("country") or "", rel.get("date") or "",
                                          (medium.get("format") or "")) if x)
        out.append(m)
    return out
