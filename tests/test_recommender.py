import asyncio
import unittest

from ani365_recommender.main import refresh
from ani365_recommender.scoring import genre_taste, rank, top_genres


def profile():
    return {"user_id": 42, "excluded_anime365_series_ids": [44], "rates": [
        {"external_anime_id": "1", "status": "completed", "score": 10},
        {"external_anime_id": "2", "status": "completed", "score": 9},
        {"external_anime_id": "3", "status": "completed", "score": 8},
        {"external_anime_id": "4", "status": "completed", "score": 7},
        {"external_anime_id": "5", "status": "completed", "score": 2},
        {"external_anime_id": "6", "status": "planned", "score": 10},
    ]}


def metadata():
    return {
        "1": {"genres": [{"id": "10", "name": "Drama"}]},
        "2": {"genres": [{"id": "10", "name": "Drama"}, {"id": "20", "name": "Fantasy"}]},
        "3": {"genres": [{"id": "20", "name": "Fantasy"}]},
        "4": {"genres": [{"id": "10", "name": "Drama"}]},
        "5": {"genres": [{"id": "30", "name": "Comedy"}]},
    }


class RecommendationScoringTests(unittest.TestCase):
    def test_scores_liked_genres_excludes_existing_titles_and_explains_result(self):
        taste = genre_taste(profile(), metadata())
        self.assertEqual(top_genres(taste), ["10", "20"])
        candidates = [
            {"id": "1", "score": 9, "genres": [{"id": "10", "name": "Drama"}]},
            {"id": "7", "score": 8, "genres": [{"id": "10", "name": "Drama"}]},
            {"id": "8", "score": 10, "genres": [{"id": "30", "name": "Comedy"}]},
            {"id": "9", "score": 9, "genres": [{"id": "20", "name": "Fantasy"}]},
        ]
        resolved = [
            {"shikimori_anime_id": "7", "anime365_series_id": 77, "title": "Drama pick"},
            {"shikimori_anime_id": "8", "anime365_series_id": 88, "title": "Comedy pick"},
            {"shikimori_anime_id": "9", "anime365_series_id": 44, "title": "Already saved"},
        ]
        result = rank(profile(), metadata(), candidates, resolved)
        self.assertEqual([item["anime365_series_id"] for item in result], [77])
        self.assertIn("Drama", result[0]["reason"])

    def test_requires_five_completed_ratings(self):
        small = profile()
        small["rates"] = small["rates"][:4]
        self.assertEqual(rank(small, metadata(), [], []), [])


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
                return [{"id": "7", "mal_id": "7", "score": 8,
                         "genres": [{"id": "10", "name": "Drama"}]}]

        main, shikimori = Main(), Shikimori()
        result = asyncio.run(refresh(main_api=main, shikimori=shikimori,
                                     config=type("Config", (), {})()))
        self.assertEqual(result, {"profiles": 1, "candidates": 1, "saved": 1})
        self.assertEqual(main.saved[0][0], 42)
        self.assertEqual(main.saved[0][1][0]["anime365_series_id"], 77)
