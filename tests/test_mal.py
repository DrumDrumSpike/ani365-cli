import unittest

import httpx

from ani365_bot.mal import MyAnimeList


class MyAnimeListTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_metadata_uses_client_id_and_keeps_only_safe_cover(self):
        seen = []

        def upstream(request):
            seen.append(request)
            return httpx.Response(200, json={
                "id": 5114, "title": "Fullmetal Alchemist: Brotherhood",
                "main_picture": {
                    "large": "https://cdn.myanimelist.net/images/anime/1208/94745.jpg",
                },
                "media_type": "tv", "start_date": "2009-04-05",
            })

        client = MyAnimeList("public-client-id", transport=httpx.MockTransport(upstream))
        metadata = await client.anime(5114)
        self.assertEqual(metadata, {
            "title": "Fullmetal Alchemist: Brotherhood",
            "poster_url": "https://cdn.myanimelist.net/images/anime/1208/94745.jpg",
            "kind": "tv", "aired_on": "2009-04-05",
        })
        self.assertEqual(seen[0].headers["x-mal-client-id"], "public-client-id")
        self.assertEqual(seen[0].url.params["fields"], MyAnimeList.fields)

    def test_public_metadata_rejects_non_mal_or_tracking_poster_urls(self):
        self.assertIsNone(MyAnimeList.public_metadata({"main_picture": {
            "large": "https://example.test/images/anime/1.jpg",
        }})["poster_url"])
        self.assertIsNone(MyAnimeList.public_metadata({"main_picture": {
            "large": "https://cdn.myanimelist.net/images/anime/1.jpg?tracking=1",
        }})["poster_url"])
