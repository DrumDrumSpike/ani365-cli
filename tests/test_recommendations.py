import asyncio
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi.testclient import TestClient

from ani365_bot.config import Config
from ani365_bot.recommendations.candidates import select_candidates
from ani365_bot.recommendations.continuations import find_continuations
from ani365_bot.recommendations.llm import ChatCompletionsClient, LLMError
from ani365_bot.recommendations.parser import ParseError, parse_picks
from ani365_bot.recommendations.prompt import build_user_prompt
from ani365_bot.recommendations.resolve import resolve_candidates
from ani365_bot.recommendations.service import RecommendationRequestError, RecommendationService
from ani365_bot.recommendations.taste import genre_weights, relative_signal, score_distribution, taste_profile
from ani365_bot.store import Store
from ani365_bot.web import create_app


BOT_TOKEN = "123456:mini-app-test-token"


def init_data(user_id, *, auth_date=None, token=BOT_TOKEN):
    values = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def drama(identifier="10"):
    return {"id": identifier, "genres": [{"id": "10", "name": "Драма"}], "franchise": "seen-drama"}


class ParserTests(unittest.TestCase):
    def test_accepts_only_zero_to_two_distinct_candidate_ids(self):
        allowed = ["10", "20"]
        self.assertEqual(parse_picks('{"picks": []}', allowed), [])
        self.assertEqual(parse_picks({"picks": [{"id": 10, "reason": "Коротко почему."}]}, allowed),
                         [{"id": "10", "reason": "Коротко почему."}])
        two = {"picks": [{"id": "10", "reason": "Первый."}, {"id": "20", "reason": "Второй."}]}
        self.assertEqual([item["id"] for item in parse_picks(two, allowed)], ["10", "20"])
        for payload in (
            {"picks": [{"id": "10", "reason": "a"}, {"id": "20", "reason": "b"}, {"id": "10", "reason": "c"}]},
            {"picks": [{"id": "10", "reason": "a"}, {"id": "10", "reason": "b"}]},
            {"picks": [{"id": "99", "reason": "чужой"}]},
            {"picks": [{"id": "10", "reason": "ok"}, {"id": "99", "reason": "чужой"}]},
            {"picks": [{"id": 10.0, "reason": "дробный"}]},
            {"picks": [{"id": "10"}]},
            "not-json",
            {"items": []},
            {"picks": None},
        ):
            with self.assertRaises(ParseError):
                parse_picks(payload, allowed)


class TasteTests(unittest.TestCase):
    def test_relative_scores_follow_the_personal_scale(self):
        easy = score_distribution([6, 6, 7, 7, 7, 6, 10])
        usual = relative_signal(7, easy)
        loved = relative_signal(10, easy)
        self.assertEqual(usual["label"], "typical")
        self.assertEqual(loved["label"], "stands_out")
        self.assertGreater(loved["weight"], usual["weight"])
        harsh = score_distribution([9, 9, 10, 10, 9, 10])
        low = relative_signal(7, harsh)
        self.assertEqual(low["label"], "stands_out_low")
        self.assertLess(low["weight"], 0)
        self.assertLess(low["weight"], relative_signal(10, harsh)["weight"])

    def test_dropped_keeps_its_score_and_does_not_change_genre_weights(self):
        completed = [
            {"external_anime_id": "1", "status": "completed", "score": 6, "title": "Обычное"},
            {"external_anime_id": "2", "status": "completed", "score": 7, "title": "Ещё одно"},
            {"external_anime_id": "3", "status": "watching", "score": 10, "title": "Сейчас смотрю"},
        ]
        metadata = {"1": drama(), "2": drama(), "3": drama(),
                    "9": {"genres": [{"id": "40", "name": "Ужасы"}]}}
        before = genre_weights(completed, metadata)
        dropped = completed + [
            {"external_anime_id": "9", "status": "dropped", "score": 8, "title": "Бросил с оценкой"},
            {"external_anime_id": "8", "status": "dropped", "score": None, "title": "Бросил без оценки"},
        ]
        after = genre_weights(dropped, metadata)
        self.assertEqual(before, after)
        self.assertNotIn("40", after)
        profile = taste_profile(dropped, metadata)
        by_title = {item["title"]: item["score"] for item in profile["dropped"]}
        self.assertEqual(by_title["Бросил с оценкой"], 8)
        self.assertIsNone(by_title["Бросил без оценки"])
        self.assertEqual(taste_profile(completed, metadata)["titles"][-1]["signal"], "stands_out")

    def test_low_pair_rating_does_not_change_genre_weights(self):
        rates = [
            {"external_anime_id": "1", "status": "completed", "score": 7, "title": "Обычное"},
            {"external_anime_id": "2", "status": "completed", "score": 10, "title": "Любимое"},
        ]
        metadata = {"1": drama(), "2": drama()}
        before = genre_weights(rates, metadata)
        feedback = [{"source": "openai", "pair_rating": 1, "pair_skipped": False,
                     "interested_titles": [], "interested_ids": []}]
        after = genre_weights(rates, metadata)
        self.assertEqual(before, after)
        prompt = build_user_prompt(taste_profile(rates, metadata), feedback, [], [])
        self.assertIn("оценка пары 1", prompt)
        self.assertEqual(taste_profile(rates, metadata)["genre_weights"], before)


class CandidateAndContinuationTests(unittest.TestCase):
    def test_pool_is_personal_diverse_and_skips_sequels(self):
        rows = []
        for index, year in enumerate((2024, 2024, 2024, 2024, 2024, 2018, 2011, 2001)):
            rows.append({"id": str(100 + index), "mal_id": str(1000 + index), "title": f"Title {year}",
                         "year": year, "kind": "ona" if index == 5 else "tv", "franchise": f"f{index}",
                         "score": 9 - index, "genres": [{"id": "10", "name": "Драма"}]})
        rows.append({"id": "200", "year": 1999, "kind": "special", "franchise": "special", "score": 10})
        rows.append({"id": "201", "year": 2010, "kind": "tv", "franchise": "same", "score": 7,
                     "related": [{"relation": "prequel", "anime": {"id": "1"}}]})
        rows.append({"id": "202", "year": 2010, "kind": "movie", "franchise": "dupe", "score": 6})
        rows.append({"id": "203", "year": 2022, "kind": "movie", "franchise": "dupe", "score": 9})
        rows.append({"id": "204", "year": 2016, "kind": "ova", "franchise": "seen-drama", "score": 9})
        rows.append({"id": "1", "year": 2016, "kind": "tv", "franchise": "fresh", "score": 9})
        rows.append({"id": "300", "year": 2015, "kind": "tv", "franchise": "library", "score": 9,
                     "anime365_series_id": 44})
        selected = select_candidates(
            rows, excluded_shikimori_ids={"1"}, excluded_series_ids={44},
            excluded_franchises={"seen-drama"}, already_recommended_ids={"104"}, limit=6,
            require_resolved=False)
        identifiers = {item["id"] for item in selected}
        self.assertNotIn("200", identifiers)
        self.assertNotIn("201", identifiers)
        self.assertNotIn("204", identifiers)
        self.assertNotIn("1", identifiers)
        self.assertNotIn("300", identifiers)
        self.assertNotIn("104", identifiers)
        self.assertEqual(len([item for item in selected if item["franchise"] == "dupe"]), 1)
        self.assertEqual(next(item["year"] for item in selected if item["franchise"] == "dupe"), 2010)
        self.assertGreaterEqual(len({item["year"] for item in selected}), 3)
        self.assertIn("ona", {item["kind"] for item in selected})

    def test_continuation_detects_unlisted_sequels_only(self):
        rates = [
            {"external_anime_id": "1", "status": "completed", "title": "Сезон 1"},
            {"external_anime_id": "2", "status": "planned", "title": "Уже в планах"},
            {"external_anime_id": "3", "status": "watching", "title": "Смотрю"},
            {"external_anime_id": "4", "status": "dropped", "title": "Брошенный старт"},
            {"external_anime_id": "8", "status": "completed", "title": "Закрытый сиквел"},
        ]
        anime = {
            "1": {"id": "1", "related": [
                {"relation": "sequel", "anime": {"id": "2", "title": "Уже в планах", "kind": "tv", "year": 2024}},
                {"relation": "sequel", "anime": {"id": "9", "title": "Новый сезон", "mal_id": "90",
                                                 "kind": "tv", "year": 2025}},
                {"relation": "side_story", "anime": {"id": "10", "title": "Спин-офф", "kind": "ova", "year": 2025}},
            ]},
            "3": {"id": "3", "related": [
                {"relation": "sequel", "anime": {"id": "11", "title": "Продолжение", "kind": "ona", "year": 2023}},
            ]},
            "4": {"id": "4", "related": [
                {"relation": "sequel", "anime": {"id": "12", "title": "После брошенного", "kind": "tv", "year": 2022}},
            ]},
            "8": {"id": "8", "related": [
                {"relation": "sequel", "anime": {"id": "13", "title": "Уже посмотрен", "kind": "tv", "year": 2021}},
            ]},
            "13": {"id": "13"},
        }
        rates.append({"external_anime_id": "13", "status": "completed", "title": "Уже посмотрен"})
        found = {item["shikimori_anime_id"]: item for item in find_continuations(rates, anime)}
        self.assertEqual(set(found), {"9", "11"})
        self.assertEqual(found["9"]["after_title"], "Сезон 1")
        self.assertEqual(found["9"]["mal_id"], "90")


class ResolveTests(unittest.TestCase):
    def test_resolve_uses_exact_mal_id_only(self):
        class Anime:
            def __init__(self):
                self.calls = []

            async def series_by_mal_id(self, mal_id):
                self.calls.append(str(mal_id))
                if str(mal_id) != "1000":
                    return []
                return [{"id": 55, "myAnimeListId": 1000, "titles": {"ru": "Точный"}, "year": 2016,
                         "type": "tv", "posterUrlSmall": "https://smotret-anime.app/posters/55.jpg"}]

        anime = Anime()
        result = asyncio.run(resolve_candidates(anime, [
            {"id": "100", "mal_id": "1000", "title": "Точный", "kind": "tv"},
            {"id": "50", "mal_id": None, "title": "Без MAL", "kind": "tv"},
            {"id": "77", "title": "Тоже без MAL", "kind": "tv"},
        ], anime_url="https://smotret-anime.app/api"))
        self.assertEqual(anime.calls, ["1000"])
        self.assertEqual(result[0]["anime365_series_id"], 55)
        self.assertEqual(len(result), 1)


class ScriptedClient:
    def __init__(self, responses=(), *, configured=True, error=None):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []
        self._configured = configured
        self.error = error

    def configured(self):
        return self._configured

    async def complete(self, system, user):
        self.calls += 1
        self.prompts.append(user)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class ScriptedCatalog:
    def __init__(self, metadata, candidates):
        self.metadata_payload = metadata
        self.candidates_payload = candidates

    async def metadata(self, ids, *, related=False):
        return {key: value for key, value in self.metadata_payload.items() if key in {str(item) for item in ids}}

    async def candidates(self, genres):
        return self.candidates_payload


class ScriptedAnime:
    def __init__(self):
        self.calls = []

    async def series_by_mal_id(self, mal_id):
        self.calls.append(str(mal_id))
        return [{"id": int(mal_id), "myAnimeListId": int(mal_id), "titles": {"ru": f"Тайтл {mal_id}"},
                 "year": 2016, "type": "tv",
                 "posterUrlSmall": f"https://smotret-anime.app/posters/{mal_id}.jpg"}]


def seed_user(store, user_id=1):
    store.save_external_account(user_id, "shikimori", "access", "refresh", time.time() + 3600, "99")
    store.import_external_rates(user_id, "shikimori", [
        {"external_rate_id": "1", "external_anime_id": "1", "status": "completed", "episodes": 12,
         "score": 6, "title": "Обычное"},
        {"external_rate_id": "2", "external_anime_id": "2", "status": "completed", "episodes": 12,
         "score": 7, "title": "Ещё одно"},
        {"external_rate_id": "3", "external_anime_id": "3", "status": "completed", "episodes": 12,
         "score": 7, "title": "Третье"},
        {"external_rate_id": "4", "external_anime_id": "4", "status": "completed", "episodes": 12,
         "score": 10, "title": "Любимое"},
        {"external_rate_id": "9", "external_anime_id": "9", "status": "dropped", "episodes": 3,
         "score": 8, "title": "Брошенный ужас"},
    ])


def metadata():
    return {str(index): drama() for index in range(1, 5)} | {
        "9": {"genres": [{"id": "40", "name": "Ужасы"}], "franchise": "horror"},
    }


def candidate(identifier, mal_id, year=2016):
    return {"id": str(identifier), "mal_id": str(mal_id), "title": f"Кандидат {identifier}",
            "year": year, "kind": "tv", "franchise": f"fresh-{identifier}", "score": 8,
            "genres": [{"id": "10", "name": "Драма"}]}


def answer(identifier):
    return json.dumps({"picks": [{"id": str(identifier), "reason": "Похоже на «Любимое», без спойлеров."}]},
                      ensure_ascii=False)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        seed_user(self.store)
        self.anime = ScriptedAnime()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def service(self, clients, candidates=None, refresh_rates=None):
        return RecommendationService(
            self.store, self.anime, ScriptedCatalog(metadata(), candidates if candidates is not None else [candidate(100, 1000)]),
            clients, refresh_rates=refresh_rates, candidate_limit=36)

    def test_idempotent_request_calls_the_model_once(self):
        client = ScriptedClient([answer(100)])
        service = self.service({"openai": client, "xai": ScriptedClient()})
        first, created = service.request(1, "openai")
        second, again = service.request(1, "openai")
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first["id"], second["id"])
        asyncio.run(service.execute(first["id"]))
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.anime.calls, ["1000"])
        self.assertNotIn("100", self.anime.calls)
        pick = self.store.latest_recommendation_pick(1, "openai")
        self.assertEqual(pick["items"][0]["shikimori_anime_id"], "100")
        self.assertIn("Любимое", pick["items"][0]["reason"])

    def test_invalid_output_retries_once_then_keeps_the_previous_pick(self):
        client = ScriptedClient(["nope", answer(100)])
        service = self.service({"openai": client, "xai": ScriptedClient()},
                               [candidate(100, 1000), candidate(300, 3000, 2004)])
        job, _ = service.request(1, "openai")
        asyncio.run(service.execute(job["id"]))
        self.assertEqual(client.calls, 2)
        self.assertEqual(self.store.recommendation_job(job["id"])["state"], "ready")

        client.responses = ["still-bad", json.dumps({"picks": [{"id": "999", "reason": "чужой"}]})]
        job, _ = service.request(1, "openai")
        asyncio.run(service.execute(job["id"]))
        self.assertEqual(client.calls, 4)
        self.assertEqual(self.store.recommendation_job(job["id"])["state"], "failed")
        self.assertEqual(self.store.latest_recommendation_pick(1, "openai")["items"][0]["shikimori_anime_id"], "100")

    def test_sources_and_users_stay_isolated_and_history_does_not_repeat(self):
        saved, _ = self.store.enqueue_recommendation_job(1, "openai", now=10)
        self.store.mark_recommendation_job_running(saved["id"], now=11)
        self.store.complete_recommendation_job(saved["id"], 1, "openai", [{
            "id": "100", "mal_id": "1000", "anime365_series_id": 1000, "title": "Прошлый",
            "year": 2016, "kind": "tv", "genres": ["Драма"], "reason": "Старое объяснение.",
        }], now=12)
        failing = ScriptedClient(error=LLMError(
            "rate_limited", "У ChatGPT закончился лимит или баланс. Попробуйте позже."))
        grok = ScriptedClient([answer(200)])
        service = self.service({"openai": failing, "xai": grok},
                               [candidate(100, 1000), candidate(200, 2000, 2008)])
        openai_job, _ = service.request(1, "openai")
        xai_job, _ = service.request(1, "xai")
        asyncio.run(service.execute(openai_job["id"]))
        asyncio.run(service.execute(xai_job["id"]))
        self.assertEqual(self.store.recommendation_job(openai_job["id"])["error_code"], "rate_limited")
        self.assertEqual(self.store.latest_recommendation_pick(1, "openai")["items"][0]["title"], "Прошлый")
        self.assertEqual(self.store.latest_recommendation_pick(1, "xai")["items"][0]["shikimori_anime_id"], "200")
        self.assertIsNone(self.store.latest_recommendation_pick(2, "xai"))

        seed_user(self.store, 2)
        other = ScriptedClient([answer(300)])
        other_service = self.service({"openai": other, "xai": ScriptedClient()}, [candidate(300, 3000, 2001)])
        other_job, _ = other_service.request(2, "openai")
        asyncio.run(other_service.execute(other_job["id"]))
        self.assertEqual(self.store.recommended_shikimori_ids(2), {"300"})
        self.assertNotIn("200", self.store.recommended_shikimori_ids(2))
        self.assertEqual(self.store.recommendation_history(2, "xai"), [])

        repeat = ScriptedClient([answer(400)])
        repeat_service = self.service({"openai": ScriptedClient(), "xai": repeat},
                                      [candidate(200, 2000, 2008), candidate(400, 4000, 1998)])
        again, _ = repeat_service.request(1, "xai")
        asyncio.run(repeat_service.execute(again["id"]))
        self.assertEqual(repeat.calls, 1)
        self.assertNotIn("200|", repeat.prompts[0])
        self.assertIn("400|", repeat.prompts[0])
        self.assertIn("200", repeat.prompts[0])
        self.assertEqual(self.store.latest_recommendation_pick(1, "xai")["items"][0]["shikimori_anime_id"], "400")
        self.assertIn("200", {item["shikimori_anime_id"]
                              for pick in self.store.recommendation_history(1, "xai") for item in pick["items"]})

    def test_restart_resumes_or_fails_without_a_second_api_call(self):
        directory = Path(self.temp.name)
        job, _ = self.store.enqueue_recommendation_job(1, "openai", now=1)
        self.assertTrue(self.store.mark_recommendation_job_running(job["id"], now=2))
        self.store.complete_recommendation_job(job["id"], 1, "openai", [{
            "id": "50", "anime365_series_id": 50, "title": "Сохранённый", "reason": "Уже было.",
            "genres": ["Драма"],
        }], now=3)
        inflight, _ = self.store.enqueue_recommendation_job(1, "xai", now=4)
        self.assertTrue(self.store.mark_recommendation_job_running(inflight["id"], now=5))
        self.assertTrue(self.store.mark_recommendation_api_called(inflight["id"]))
        queued, _ = self.store.enqueue_recommendation_job(1, "openai", now=6)
        self.store.close()

        reopened = Store(directory)
        client = ScriptedClient([answer(100)])
        service = RecommendationService(
            reopened, self.anime, ScriptedCatalog(metadata(), [candidate(100, 1000)]),
            {"openai": client, "xai": ScriptedClient()})
        resumed = service.recover()
        self.assertEqual(resumed, [queued["id"]])
        failed = reopened.recommendation_job(inflight["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error_code"], "interrupted")
        self.assertIn("не отправлялся", failed["error_message"])
        self.assertEqual(reopened.latest_recommendation_pick(1, "openai")["items"][0]["title"], "Сохранённый")
        asyncio.run(service.execute(queued["id"]))
        self.assertEqual(client.calls, 1)
        reopened.close()
        self.store = Store(directory)

    def test_feedback_is_stored_per_pick_and_does_not_change_weights(self):
        rates = self.store.external_user_rates(1, "shikimori", limit=20)
        before = genre_weights(rates, metadata())
        job, _ = self.store.enqueue_recommendation_job(1, "openai", now=1)
        self.store.mark_recommendation_job_running(job["id"], now=2)
        pick = self.store.complete_recommendation_job(job["id"], 1, "openai", [{
            "id": "100", "anime365_series_id": 1000, "title": "Первый", "reason": "Потому что.",
            "genres": ["Драма"],
        }, {
            "id": "101", "anime365_series_id": 1001, "title": "Второй", "reason": "И это тоже.",
            "genres": ["Драма"],
        }], now=3)
        saved = self.store.save_recommendation_feedback(
            1, "openai", pick["id"], pair_rating=2, interested_ids=["101"], now=4)
        self.assertEqual(saved["pair_rating"], 2)
        self.assertEqual([item["interested"] for item in saved["items"]], [False, True])
        self.assertIsNone(self.store.save_recommendation_feedback(2, "openai", pick["id"], pair_rating=1))
        self.assertEqual(genre_weights(self.store.external_user_rates(1, "shikimori", limit=20), metadata()), before)
        self.assertEqual(self.store.recent_recommendation_feedback(1)[0]["pair_rating"], 2)

    def test_missing_key_does_not_block_the_other_source(self):
        grok = ScriptedClient([answer(200)])
        service = self.service({"openai": ScriptedClient(configured=False), "xai": grok},
                               [candidate(200, 2000)])
        openai_job, _ = service.request(1, "openai")
        xai_job, _ = service.request(1, "xai")
        asyncio.run(service.execute(openai_job["id"]))
        asyncio.run(service.execute(xai_job["id"]))
        self.assertEqual(self.store.recommendation_job(openai_job["id"])["error_code"], "missing_key")
        self.assertFalse(self.store.recommendation_job(openai_job["id"])["api_called"])
        self.assertEqual(grok.calls, 1)
        self.assertEqual(self.store.latest_recommendation_pick(1, "xai")["items"][0]["shikimori_anime_id"], "200")

    def test_daily_limit_is_per_user_and_source(self):
        service = self.service({"openai": ScriptedClient(), "xai": ScriptedClient()})
        service.daily_limit = 1
        job, _ = service.request(1, "openai")
        self.assertTrue(self.store.mark_recommendation_job_running(job["id"]))
        self.assertTrue(self.store.mark_recommendation_api_called(job["id"]))
        self.store.fail_recommendation_job(job["id"], "rate_limited", "У ChatGPT закончился лимит или баланс.")
        with self.assertRaises(RecommendationRequestError) as limited:
            service.request(1, "openai")
        self.assertEqual(limited.exception.status, 429)
        self.assertIn("ChatGPT", str(limited.exception))
        other, created = service.request(1, "xai")
        self.assertTrue(created)
        self.assertNotEqual(other["id"], job["id"])
        seed_user(self.store, 2)
        _, created_for_other_user = service.request(2, "openai")
        self.assertTrue(created_for_other_user)

    def test_shikimori_refresh_failure_uses_the_saved_snapshot(self):
        async def refresh(_user_id):
            raise RuntimeError("shikimori down")

        client = ScriptedClient([answer(100)])
        service = self.service({"openai": client, "xai": ScriptedClient()}, refresh_rates=refresh)
        job, _ = service.request(1, "openai")
        asyncio.run(service.execute(job["id"]))
        self.assertEqual(self.store.recommendation_job(job["id"])["state"], "ready")
        self.assertIn("Любимое", client.prompts[0])


class LLMHttpTests(unittest.TestCase):
    def test_status_codes_become_russian_errors_without_the_body(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            if request.url.path.endswith("/429"):
                return httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": "secret-body"}})
            if request.url.path.endswith("/401"):
                return httpx.Response(401, json={"error": {"message": "bad key"}})
            if request.url.path.endswith("/slow"):
                raise httpx.ReadTimeout("slow reasoning")
            return httpx.Response(200, json={"choices": [{"message": {"content": "{\"picks\": []}"}}]})

        async def run():
            limited = ChatCompletionsClient(source="openai", api_key="secret", model="gpt-5.4-mini",
                                            url="https://api.openai.com/429", transport=httpx.MockTransport(handler))
            with self.assertRaises(LLMError) as quota:
                await limited.complete("system", "user")
            self.assertEqual(quota.exception.code, "rate_limited")
            self.assertNotIn("secret", str(quota.exception))
            self.assertEqual(seen["body"]["model"], "gpt-5.4-mini")
            self.assertEqual(seen["body"]["reasoning_effort"], "medium")
            self.assertGreaterEqual(seen["body"]["max_completion_tokens"], 4096)
            self.assertEqual(seen["body"]["response_format"]["type"], "json_schema")
            self.assertNotIn("temperature", seen["body"])
            denied = ChatCompletionsClient(source="xai", api_key="secret", model="grok-4.7",
                                           url="https://api.x.ai/401", reasoning_effort="medium",
                                           transport=httpx.MockTransport(handler))
            with self.assertRaises(LLMError) as auth:
                await denied.complete("system", "user")
            self.assertEqual(auth.exception.code, "unauthorized")
            self.assertIn("XAI_API_KEY", str(auth.exception))
            self.assertEqual(seen["body"]["reasoning_effort"], "medium")
            slow = ChatCompletionsClient(source="openai", api_key="secret", model="gpt-5.4-mini",
                                         url="https://api.openai.com/slow", timeout=180,
                                         transport=httpx.MockTransport(handler))
            with self.assertRaises(LLMError) as timed_out:
                await slow.complete("system", "user")
            self.assertEqual(timed_out.exception.code, "timeout")
            self.assertEqual(slow.timeout, 180)
            missing = ChatCompletionsClient(source="openai", api_key="", model="gpt-5.4-mini",
                                            url="https://api.openai.com/v1/chat/completions")
            with self.assertRaises(LLMError) as absent:
                await missing.complete("system", "user")
            self.assertEqual(absent.exception.code, "missing_key")

        asyncio.run(run())


class WebRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.config = Config(BOT_TOKEN, 42, data_dir=Path(self.temp.name),
                             media_dir=Path(self.temp.name) / "jobs", web_cookie_secure=False,
                             anime_token="server-anime365-token")
        self.anime = type("Anime", (), {})()
        self.anime.series_by_mal_id = ScriptedAnime().series_by_mal_id
        self.anime.search = self.anime.episodes = self.anime.translations = None
        self.app = create_app(self.config, self.store, self.anime)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.temp.cleanup()

    def headers(self, user_id):
        return {"X-Telegram-Init-Data": init_data(user_id)}

    def test_shikimori_must_be_connected_before_a_pick(self):
        response = self.client.get("/api/recommendations", headers=self.headers(42))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["shikimori_connected"])
        denied = self.client.post("/api/recommendations/openai", headers=self.headers(42))
        self.assertEqual(denied.status_code, 409)
        self.assertIn("Shikimori", denied.json()["detail"])

    def test_click_is_idempotent_and_picks_stay_private(self):
        self.store.save_external_account(42, "shikimori", "access-secret", "refresh-secret", time.time() + 3600, "7")
        calls = []

        async def hang(job_id):
            calls.append(job_id)
            await asyncio.Event().wait()

        self.app.state.recommendations.execute = hang
        first = self.client.post("/api/recommendations/openai", headers=self.headers(42))
        second = self.client.post("/api/recommendations/openai", headers=self.headers(42))
        self.assertEqual(first.status_code, 202)
        self.assertTrue(first.json()["created"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(first.json()["job"]["id"], second.json()["job"]["id"])
        self.assertEqual(calls, [first.json()["job"]["id"]])
        self.assertNotIn("access-secret", first.text)
        self.store.add_allowed_user(7, owner_id=42)
        job, _ = self.store.enqueue_recommendation_job(42, "xai", now=1)
        self.store.mark_recommendation_job_running(job["id"], now=2)
        self.store.complete_recommendation_job(job["id"], 42, "xai", [{
            "id": "100", "anime365_series_id": 55, "title": "Только владельцу", "reason": "Потому что.",
            "genres": ["Драма"],
        }], now=3)
        self.app.state.recommendations.catalog = ScriptedCatalog({}, [])
        owner = self.client.get("/api/recommendations", headers=self.headers(42))
        other = self.client.get("/api/recommendations", headers=self.headers(7))
        self.assertEqual(owner.json()["sources"]["xai"]["pick"]["items"][0]["title"], "Только владельцу")
        self.assertIsNone(other.json()["sources"]["xai"]["pick"])
        history = self.client.get("/api/recommendations/history?source=xai", headers=self.headers(7))
        self.assertEqual(history.json()["items"], [])

    def test_new_app_instance_keeps_picks_and_closes_an_inflight_call(self):
        self.store.save_external_account(42, "shikimori", "access", "refresh", time.time() + 3600, "7")
        done, _ = self.store.enqueue_recommendation_job(42, "openai", now=1)
        self.store.mark_recommendation_job_running(done["id"], now=2)
        self.store.complete_recommendation_job(done["id"], 42, "openai", [{
            "id": "100", "anime365_series_id": 55, "title": "Живая подборка", "reason": "Остаётся.",
            "genres": ["Драма"],
        }], now=3)
        inflight, _ = self.store.enqueue_recommendation_job(42, "xai", now=4)
        self.store.mark_recommendation_job_running(inflight["id"], now=5)
        self.store.mark_recommendation_api_called(inflight["id"])
        self.client.close()
        app = create_app(self.config, self.store, self.anime)
        app.state.recommendations.catalog = ScriptedCatalog({}, [])
        with TestClient(app) as client:
            body = client.get("/api/recommendations", headers=self.headers(42)).json()
        self.assertEqual(body["sources"]["openai"]["pick"]["items"][0]["title"], "Живая подборка")
        self.assertEqual(body["sources"]["xai"]["job"]["state"], "failed")
        self.assertEqual(body["sources"]["xai"]["job"]["error_code"], "interrupted")
        self.assertIsNone(body["sources"]["xai"]["pick"])


if __name__ == "__main__":
    unittest.main()
