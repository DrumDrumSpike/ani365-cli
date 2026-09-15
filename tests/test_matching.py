import unittest

from ani365_bot.matching import normalise_title, rank_anime365_candidates, shikimori_mal_ids, shikimori_titles


class ShikimoriMatchingTests(unittest.TestCase):
    def test_normalise_title_ignores_punctuation_and_yo(self):
        self.assertEqual(normalise_title("Нет игры — нет жизни"), "нет игры нет жизни")
        self.assertEqual(normalise_title("Ёлка!"), "елка")

    def test_uses_shikimori_anime_id_as_mal_bridge_when_mal_id_is_empty(self):
        anime = {"id": 52991, "mal_id": None, "russian": "Фрирен", "aired_on": "2023-09-29",
                 "kind": "tv"}
        rows = [
            {"id": 2, "myAnimeListId": 59978, "year": 2026, "type": "tv",
             "titles": {"ru": "Фрирен 2"}},
            {"id": 1, "myAnimeListId": 52991, "year": 2023, "type": "tv",
             "titles": {"ru": "Провожающая в последний путь Фрирен"}},
        ]
        result = rank_anime365_candidates(anime, rows)
        self.assertEqual(shikimori_mal_ids(anime), {"52991"})
        self.assertEqual(result[0]["series_id"], 1)
        self.assertTrue(result[0]["verified_mal"])
        self.assertEqual(result[0]["match_reason"], "MAL ID совпал")

    def test_combines_alternative_titles_and_deduplicates_search_results(self):
        anime = {"id": 1, "russian": "Русское имя", "name": "Roman name",
                 "english": ["English title"], "synonyms": ["Alternate", "Русское имя"]}
        self.assertEqual(shikimori_titles(anime),
                         ["Русское имя", "Roman name", "English title", "Alternate"])
        row = {"id": 55, "titles": {"en": "English title"}, "year": 2024, "type": "tv"}
        result = rank_anime365_candidates(anime, [row, row], fallback_title="Ignored")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["match_reason"], "точное название")
        self.assertFalse(result[0]["verified_mal"])


if __name__ == "__main__":
    unittest.main()
