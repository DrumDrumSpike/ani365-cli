import unittest

from ani365_bot.translations import group_translations, viewing_type


class TranslationTests(unittest.TestCase):
    def test_known_api_types_group_by_kind_and_language(self):
        # Public Anime365 responses use these type/typeKind/typeLang combinations.
        rows = [
            {"id": 1, "type": "raw", "typeKind": "raw", "typeLang": "ja"},
            {"id": 2, "type": "subEn", "typeKind": "sub", "typeLang": "en"},
            {"id": 3, "type": "subRu", "typeKind": "sub", "typeLang": "ru"},
            {"id": 4, "type": "voiceEn", "typeKind": "voice", "typeLang": "en"},
            {"id": 5, "type": "voiceRu", "typeKind": "voice", "typeLang": "ru"},
            {"id": 6, "type": "subRu"},
        ]
        groups = group_translations(rows)
        self.assertEqual([g.label for g in groups], ["Субтитры · Русский", "Озвучка · Русский",
                                                   "Субтитры · Английский", "Озвучка · Английский",
                                                   "Оригинал (RAW)"])
        self.assertEqual([t["id"] for t in groups[0].translations], [3, 6])
        self.assertEqual(sorted(t["id"] for g in groups for t in g.translations), [1, 2, 3, 4, 5, 6])

    def test_type_field_supplies_missing_kind_and_language(self):
        self.assertEqual(viewing_type({"type": "voiceEn"}), ("voice", "en"))
        self.assertEqual(viewing_type({"type": "subRu", "typeKind": "sub"}), ("sub", "ru"))
        self.assertEqual(viewing_type({"typeKind": "subtitles", "typeLang": "DE"}), ("sub", "de"))
        self.assertEqual(viewing_type({"type": "raw"}), ("raw", ""))

    def test_only_available_types_and_other_languages_are_retained(self):
        rows = [{"id": 1, "type": "voiceRu"}, {"id": 2, "type": "subDe"},
                {"id": 3, "typeKind": "sub", "typeLang": "pt"},
                {"id": 4, "type": "custom"}, {"id": 5}]
        groups = group_translations(rows)
        labels = {g.label for g in groups}
        self.assertNotIn("Субтитры · Русский", labels)
        self.assertNotIn("Оригинал (RAW)", labels)
        self.assertTrue({"Субтитры · Немецкий", "Субтитры · PT", "custom", "Другой тип"} <= labels)
        self.assertEqual(sum(len(g.translations) for g in groups), 5)
        self.assertEqual(group_translations([]), [])


if __name__ == "__main__":
    unittest.main()
