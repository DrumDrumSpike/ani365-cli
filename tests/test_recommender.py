import asyncio
import json
import unittest

import httpx

from ani365_recommender.main import refresh
from ani365_recommender.scoring import genre_taste, rank, top_genres
from ani365_recommender.shikimori import ShikimoriPublic


def profile():
    return {"user_id": 42, "excluded_anime365_series_ids": [44], "rates": [
        {"external_anime_id": "1", "status": "completed", "score": 10},
        {"external_anime_id": "2", "status": "completed", "score": 9},
        {"external_anime_id": "3", "status": "completed", "score": 8},
        {"external_anime_id": "4", "status": "completed", "score": 7},
        {"external_anime_id": "5", "status": "completed", "score": 2},
        {"external_anime_id": "6", "status": "dropped", "score": None},
    ]}


def metadata():
    return {
        "1": {"franchise": "watched-drama", "genres": [{"id": "10", "name": "Drama"}]},
        "2": {"genres": [{"id": "10", "name": "Drama"}, {"id": "20", "name": "Fantasy"}]},
        "3": {"genres": [{"id": "20", "name": "Fantasy"}]},
        "4": {"genres": [{"id": "10", "name": "Drama"}]},
        "5": {"genres": [{"id": "30", "name": "Comedy"}]},
        "6": {"genres": [{"id": "40", "name": "Horror"}]},
    }


class RecommendationScoringTests(unittest.TestCase):
    def test_scores_liked_genres_excludes_existing_titles_and_explains_result(self):
        taste = genre_taste(profile(), metadata())
        self.assertEqual(top_genres(taste), ["10", "20"])
        self.assertEqual(taste["40"]["weight"], -4)
        candidates = [
            {"id": "1", "score": 9, "genres": [{"id": "10", "name": "Drama"}]},
            {"id": "7", "score": 8, "kind": "tv", "franchise": "fresh-drama", "genres": [{"id": "10", "name": "Drama"}]},
            {"id": "8", "score": 10, "kind": "tv", "genres": [{"id": "30", "name": "Comedy"}]},
            {"id": "10", "score": 10, "kind": "tv", "genres": [{"id": "40", "name": "Horror"}]},
            {"id": "9", "score": 9, "kind": "tv", "genres": [{"id": "20", "name": "Fantasy"}]},
            {"id": "11", "score": 10, "kind": "special", "franchise": "fresh-drama", "genres": [{"id": "10", "name": "Drama"}]},
            {"id": "12", "score": 10, "kind": "tv", "franchise": "fresh-drama", "genres": [{"id": "10", "name": "Drama"}]},
            {"id": "13", "score": 10, "kind": "tv", "franchise": "watched-drama", "genres": [{"id": "10", "name": "Drama"}]},
            {"id": "14", "score": 10, "kind": "ova", "genres": [{"id": "10", "name": "Drama"}]},
        ]
        resolved = [
            {"shikimori_anime_id": "7", "anime365_series_id": 77, "title": "Drama pick"},
            {"shikimori_anime_id": "8", "anime365_series_id": 88, "title": "Comedy pick"},
            {"shikimori_anime_id": "9", "anime365_series_id": 44, "title": "Already saved"},
            {"shikimori_anime_id": "10", "anime365_series_id": 100, "title": "Dropped genre"},
            {"shikimori_anime_id": "11", "anime365_series_id": 101, "title": "Special"},
            {"shikimori_anime_id": "12", "anime365_series_id": 102, "title": "Better drama pick"},
            {"shikimori_anime_id": "13", "anime365_series_id": 103, "title": "Watched franchise"},
            {"shikimori_anime_id": "14", "anime365_series_id": 104, "title": "OVA"},
        ]
        result = rank(profile(), metadata(), candidates, resolved)
        self.assertEqual([item["anime365_series_id"] for item in result], [102])
        self.assertIn("Drama", result[0]["reason"])

    def test_requires_five_completed_ratings(self):
        small = profile()
        small["rates"] = small["rates"][:4]
        self.assertEqual(rank(small, metadata(), [], []), [])


class ShikimoriPublicTests(unittest.TestCase):
    def test_candidates_request_and_preserve_franchise(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertIn("franchise", payload["query"])
            return httpx.Response(200, json={"data": {"animes": [{
                "id": "7", "malId": "8", "name": "Name", "russian": "", "score": 9,
                "kind": "tv", "franchise": "sample-series", "airedOn": {"year": 2025},
                "genres": [{"id": "1", "name": "Drama"}],
            }]}})

        result = asyncio.run(ShikimoriPublic(transport=httpx.MockTransport(handler)).candidates(["1"]))
        self.assertEqual(result[0]["franchise"], "sample-series")


class RecommendationRefreshTests(unittest.TestCase):
    def test_refresh_uses_only_public_metadata_and_saves_personal_feed(self):
        class Main:
            def __init__(self):
                self.saved = []

            async def profiles(self):
                return [profile()]

            async def resolve(self, candidates):
                self.candidates = candidates
                return [{"shikimori_anime_id": "7", "anime365_series_id": 77, "title": "Drama pick"}]

            async def save(self, user_id, items):
                self.saved.append((user_id, items))
                return {"saved": len(items)}

        class Shikimori:
            async def metadata(self, ids):
                self.ids = ids
                return metadata()

            async def candidates(self, genres):
                self.genres = genres
                return [{"id": "7", "mal_id": "7", "score": 8, "kind": "tv",
                         "genres": [{"id": "10", "name": "Drama"}]}]

        main, shikimori = Main(), Shikimori()
        result = asyncio.run(refresh(main_api=main, shikimori=shikimori,
                                     config=type("Config", (), {})()))
        self.assertEqual(result, {"profiles": 1, "candidates": 1, "saved": 1})
        self.assertEqual(main.saved[0][0], 42)
        self.assertEqual(main.saved[0][1][0]["anime365_series_id"], 77)
