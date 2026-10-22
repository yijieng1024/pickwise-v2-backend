"""Discovery decodes HTML entities in video titles (Review B3).

search.list returns titles HTML-escaped; every entity must be decoded on the
way in, not only &amp; (production also had &#39; and &quot;).
"""
from types import SimpleNamespace

from ._adapters import review_discovery


class _Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _YouTube:
    def search(self):
        return SimpleNamespace(list=lambda **kw: _Exec({"items": [
            {"id": {"videoId": "v1"},
             "snippet": {"title": "MacBook Air 13 &amp; 15: Don&#39;t buy the 14&quot; &lt;one&gt;",
                         "publishedAt": "2026-01-01T00:00:00Z"}},
        ]}))


def test_titles_are_decoded_at_discovery(monkeypatch):
    monkeypatch.setattr(review_discovery, "_get_youtube_client", lambda: _YouTube())
    monkeypatch.setattr(review_discovery, "fetch_descriptions", lambda ids, yt: {})
    channel = SimpleNamespace(active=True, channel_id="UC1", channel_name="Chan")

    [row] = review_discovery.discover_videos("Apple", "MacBook Air", [channel])

    assert row["video_title"] == "MacBook Air 13 & 15: Don't buy the 14\" <one>"
