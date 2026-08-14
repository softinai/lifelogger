"""Invariant tests. Run: /usr/bin/python3 -m unittest discover -s tests -v

These guard the things that are expensive to get wrong: idempotency, the local
day boundary, and the rule that the model may never touch what the owner wrote.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lifelog import (bullets, classify, config, dashboard,     # noqa: E402
                     db, digest, embed, metrics, notify, query, rollup, web)
from lifelog.ingest.base import Event, redact                  # noqa: E402
from lifelog.ingest.browser import chrome_us, from_chrome_us   # noqa: E402
from lifelog.ingest.misc import GitCommits                      # noqa: E402
from lifelog.ingest.obsidian import parse_day_bullets          # noqa: E402


def temp_db():
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    con = db.connect(pathlib.Path(handle.name))
    db.init_db(con)
    classify.sync_seed(con)
    return con


class TestIdempotency(unittest.TestCase):
    """If this fails, something is generating unstable dedupe_keys."""

    def test_double_ingest_inserts_once(self):
        con = temp_db()
        events = [Event.make(datetime.datetime(2026, 8, 11, 9, 0,
                                               tzinfo=datetime.timezone.utc),
                             "test", "note", "t:1", title="hello")]
        self.assertEqual(db.insert_events(con, events), 1)
        self.assertEqual(db.insert_events(con, events), 0)
        self.assertEqual(len(db.events_for_day(con, "2026-08-11")), 1)

    def test_dedupe_key_is_required(self):
        with self.assertRaises(ValueError):
            Event.make(datetime.datetime.now(datetime.timezone.utc), "s", "k", "")


class TestTimezone(unittest.TestCase):
    """East of UTC, a late-evening UTC event belongs to the NEXT local day.

    The zone is pinned here rather than taken from the machine: config.TZ
    follows the system timezone, so an ambient-zone assertion would only hold
    in one part of the world.
    """

    def setUp(self):
        self._tz = config.TZ
        config.TZ = datetime.timezone(datetime.timedelta(hours=5))

    def tearDown(self):
        config.TZ = self._tz

    def test_evening_utc_rolls_to_next_local_day(self):
        event = Event.make(datetime.datetime(2026, 8, 8, 19, 30,
                                             tzinfo=datetime.timezone.utc),
                           "test", "note", "t:tz")
        self.assertEqual(event.day, "2026-08-09")

    def test_morning_utc_stays_same_day(self):
        event = Event.make(datetime.datetime(2026, 8, 8, 6, 0,
                                             tzinfo=datetime.timezone.utc),
                           "test", "note", "t:tz2")
        self.assertEqual(event.day, "2026-08-08")


class TestChromeEpoch(unittest.TestCase):
    def test_roundtrip(self):
        when = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(from_chrome_us(chrome_us(when)), when)

    def test_known_value(self):

        self.assertEqual(
            from_chrome_us(86_400_000_000).date(), datetime.date(1601, 1, 2))


class TestHumanBulletsProtected(unittest.TestCase):
    """The core promise of the product (R-026, D-018)."""

    def test_ai_cannot_edit_a_human_bullet(self):
        con = temp_db()
        bid = bullets.add_human(con, "2026-08-11", "swimming: 300m")
        with self.assertRaises(bullets.HumanBulletProtected):
            bullets.edit(con, bid, "went for a swim", edited_by="ai")
        row = con.execute("SELECT text FROM bullets WHERE id=?", (bid,)).fetchone()
        self.assertEqual(row["text"], "swimming: 300m")

    def test_ai_cannot_reject_a_human_bullet(self):
        con = temp_db()
        bid = bullets.add_human(con, "2026-08-11", "gym 1.5h")
        with self.assertRaises(bullets.HumanBulletProtected):
            bullets.reject(con, bid, by="ai")

    def test_regenerating_a_day_keeps_human_bullets(self):
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "swimming: 300m")
        bullets.propose(con, "2026-08-11", "ai_eng", "old ai bullet")
        bullets.supersede_ai_for_day(con, "2026-08-11")
        bullets.propose(con, "2026-08-11", "ai_eng", "new ai bullet")
        current = bullets.for_day(con, "2026-08-11")
        texts = [r["text"] for r in current]
        self.assertIn("swimming: 300m", texts)
        self.assertIn("new ai bullet", texts)
        self.assertNotIn("old ai bullet", texts)

    def test_human_bullets_come_first(self):
        con = temp_db()
        bullets.propose(con, "2026-08-11", "ai_eng", "ai one")
        bullets.add_human(con, "2026-08-11", "mine")
        self.assertEqual(bullets.for_day(con, "2026-08-11")[0]["origin"], "human")

    def test_verbatim_including_non_english(self):
        con = temp_db()
        text = "learned new sorting algorithms\n  - быстрая сортировка\n  - Сортировка слиянием"
        bid = bullets.add_human(con, "2026-08-12", text)
        stored = con.execute("SELECT text FROM bullets WHERE id=?", (bid,)).fetchone()
        self.assertEqual(stored["text"], text)


class TestDiaryReconcile(unittest.TestCase):
    """Editing a diary line must replace the bullet, not duplicate it.

    Found by the MCP server: a parser improvement changed one bullet's text by
    10 characters and the day silently showed it twice.
    """

    def test_rewritten_diary_line_supersedes_the_old_one(self):
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "old wording", edited_by="diary")
        bullets.add_human(con, "2026-08-11", "new wording", edited_by="diary")
        bullets.reconcile_diary(con, "2026-08-11", ["new wording"])
        texts = [r["text"] for r in bullets.for_day(con, "2026-08-11")]
        self.assertEqual(texts, ["new wording"])

    def test_superseded_version_is_kept_not_deleted(self):
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "old wording", edited_by="diary")
        bullets.reconcile_diary(con, "2026-08-11", ["something else"])
        row = con.execute("SELECT status FROM bullets WHERE text='old wording'").fetchone()
        self.assertEqual(row["status"], "superseded")

    def test_directly_added_bullets_are_never_reconciled_away(self):
        """A note added via ask.py was never in the diary; it must survive."""
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "ran 5k", edited_by="human")
        bullets.reconcile_diary(con, "2026-08-11", ["unrelated diary line"])
        self.assertIn("ran 5k", [r["text"] for r in bullets.for_day(con, "2026-08-11")])


class TestEchoFilter(unittest.TestCase):
    """The model was told not to reproduce the owner's diary bullets and did it
    anyway — translating a diary entry written in another language into English
    and publishing it as its own observation. Enforced in code, not in the prompt.
    """

    GYM = ("learned new sorting algorithms with examples\n"
           "  -  быстрая сортировка\n"
           "  -  Сортировка слиянием\n"
           "  -  Пирамидальная сортировка")

    def test_translated_echo_is_caught(self):
        self.assertTrue(digest.echoes_human(
            "Explored new sorting algorithms including quicksort, merge sort, "
            "heapsort, and worked examples of each.",
            [self.GYM]))

    def test_unrelated_bullets_survive(self):
        for text in ("Fixed an issue where the markitdown MCP server failed on PDFs.",
                     "Authenticated with Claude using OAuth.",
                     "Reviewed documentation for the tracking repository."):
            self.assertFalse(digest.echoes_human(text, [self.GYM]), text)

    def test_no_human_bullets_means_nothing_is_dropped(self):
        self.assertFalse(digest.echoes_human("anything at all here", []))

    def test_short_human_bullet_cannot_trigger_a_drop(self):
        """'Sport:' is 1 token; it must not swallow every sport bullet."""
        self.assertFalse(digest.echoes_human(
            "Ran intervals on the track and logged the split times.", ["Sport:"]))

    def test_empty_ai_bullet_is_not_an_echo(self):
        self.assertFalse(digest.echoes_human("", [self.GYM]))

    NESTED = ("AI automation eng position\n"
              "    - Replied to a recruiter in email\n"
              "    - Studied docs\n"
              "        - n8n crash course doc")

    def test_echo_of_a_deeply_nested_child_line_is_caught(self):
        """Comparing against the whole block diluted this to nothing; the
        matching line is three levels down."""
        self.assertTrue(digest.echoes_human(
            "Replied to an email from a recruiter regarding a job position.",
            [self.NESTED]))

    def test_unrelated_bullet_survives_a_nested_diary_entry(self):
        self.assertFalse(digest.echoes_human(
            "Initiated the development of a new React app application.",
            [self.NESTED]))


class TestEmbeddings(unittest.TestCase):
    """Vectors live in a BLOB column: /usr/bin/python3 has no
    enable_load_extension, so sqlite-vec is unavailable (D-029)."""

    def test_pack_roundtrip_preserves_values(self):
        vector = [0.5, -0.25, 0.125]
        got = list(embed._unpack(embed._pack(vector)))
        for original, restored in zip(vector, got):
            self.assertAlmostEqual(original, restored, places=6)

    def test_norm_of_zero_vector_is_safe(self):
        self.assertEqual(embed._norm([0.0, 0.0]), 1.0)

    def test_coverage_counts_only_current_bullets(self):
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "one")
        bullets.add_human(con, "2026-08-11", "two")
        result = embed.coverage(con)
        self.assertEqual(result["bullets"], 2)
        self.assertEqual(result["embedded"], 0)

    def test_search_returns_empty_when_model_is_unreachable(self):
        """Semantic search degrades to nothing; it never raises into a run."""
        con = temp_db()
        original = embed.embed_texts
        embed.embed_texts = lambda texts, prefix="search_document": []
        try:
            self.assertEqual(embed.semantic_search(con, "anything"), [])
            self.assertEqual(embed.index_new(con), 0)
        finally:
            embed.embed_texts = original


class TestDiaryEviction(unittest.TestCase):
    """iCloud evicts file contents and leaves `.name.icloud`; exists() is then
    False and the source once yielded nothing, silently losing a day of notes.
    Silence is only allowed when the file is genuinely absent."""

    def setUp(self):
        from lifelog.ingest import obsidian
        self.obsidian = obsidian
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self._wait = obsidian.MATERIALISE_WAIT_S
        obsidian.MATERIALISE_WAIT_S = 0.0
        self._diary = config.OBSIDIAN_DIARY_DIR
        config.OBSIDIAN_DIARY_DIR = self.dir

    def tearDown(self):
        self.obsidian.MATERIALISE_WAIT_S = self._wait
        config.OBSIDIAN_DIARY_DIR = self._diary

    def _month_path(self, day):
        return config.diary_file(day)

    def test_readable_file_passes_through(self):
        day = datetime.date(2026, 8, 14)
        path = self._month_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("14/08/2026\n- wrote things\n")
        self.assertEqual(self.obsidian.materialise(path), path)
        events = list(self.obsidian.ObsidianDiary().fetch(day))
        self.assertEqual(len(events), 1)

    def test_genuinely_absent_file_is_silent(self):
        day = datetime.date(2026, 8, 14)
        self._month_path(day).parent.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(self.obsidian.materialise(self._month_path(day)))
        self.assertEqual(list(self.obsidian.ObsidianDiary().fetch(day)), [])

    def test_evicted_file_raises_instead_of_yielding_nothing(self):
        day = datetime.date(2026, 8, 14)
        path = self._month_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / ".{}.icloud".format(path.name)).write_text("stub")
        with self.assertRaises(self.obsidian.DiaryUnavailable):
            list(self.obsidian.ObsidianDiary().fetch(day))

    def test_safe_fetch_reports_the_eviction_as_an_error(self):
        day = datetime.date(2026, 8, 14)
        path = self._month_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / ".{}.icloud".format(path.name)).write_text("stub")
        events, error = self.obsidian.ObsidianDiary().safe_fetch(day)
        self.assertEqual(events, [])
        self.assertIn("evicted", str(error))


class TestApproveUnknownId(unittest.TestCase):
    """`approve.py cooking` for a nonexistent id printed "approved cooking"."""

    def test_approve_returns_false_for_unknown_id(self):
        con = temp_db()
        self.assertFalse(classify.approve(con, "no-such-domain"))

    def test_approve_returns_true_for_real_proposal(self):
        con = temp_db()
        classify.propose_category(con, "Cooking")
        self.assertTrue(classify.approve(con, "cooking"))
        self.assertIn("cooking", [r["id"] for r in classify.active(con)])


class TestDryRunLeavesNoTrace(unittest.TestCase):
    """A --dry-run once recorded runs=ok, so the next real run said "skipped"
    and a new user's first real night produced nothing."""

    def test_dry_run_does_not_block_the_real_run(self):
        import importlib, sys as _sys
        _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
        nightly = importlib.import_module("nightly")

        con = temp_db()
        day = datetime.date(2026, 8, 11)

        class Silent:
            local = True
            id = "test"
            def wake(self, attempts=6, delay=2.0):
                return False
            def generate(self, prompt, system=None):
                raise OSError("no model in tests")

        real = nightly.SOURCES
        nightly.SOURCES = []
        try:
            first = nightly.process(con, day, force=False, dry_run=True,
                                    model=Silent(), quiet=True)
            self.assertIsNone(db.last_run_for_day(con, day.isoformat()),
                              "dry run must not record a run")
            second = nightly.process(con, day, force=False, dry_run=True,
                                     model=Silent(), quiet=True)
            self.assertNotEqual(second, "skipped")
        finally:
            nightly.SOURCES = real


class TestParseDay(unittest.TestCase):
    """`--day yesterday` is the command the README hands a new user; it used to
    raise a bare ValueError traceback."""

    REF = datetime.date(2026, 8, 15)

    def test_keywords(self):
        self.assertEqual(config.parse_day("yesterday", self.REF),
                         datetime.date(2026, 8, 14))
        self.assertEqual(config.parse_day("today", self.REF), self.REF)
        self.assertEqual(config.parse_day("  YESTERDAY ", self.REF),
                         datetime.date(2026, 8, 14))

    def test_iso_date(self):
        self.assertEqual(config.parse_day("2026-01-02", self.REF),
                         datetime.date(2026, 1, 2))

    def test_garbage_explains_itself(self):
        with self.assertRaises(ValueError) as caught:
            config.parse_day("last tuesday", self.REF)
        self.assertIn("YYYY-MM-DD", str(caught.exception))


class TestGitScan(unittest.TestCase):
    """Repos are commonly grouped one directory down; a one-level glob missed
    every <root>/<group>/<repo> checkout."""

    def test_finds_repos_nested_two_levels(self):
        import subprocess
        root = pathlib.Path(tempfile.mkdtemp())
        nested = root / "group" / "proj"
        nested.mkdir(parents=True)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
               "PATH": os.environ.get("PATH", "")}
        subprocess.run(["git", "init", "-q", "-b", "main", str(nested)], check=True, env=env)
        (nested / "a.txt").write_text("hi")
        subprocess.run(["git", "-C", str(nested), "add", "a.txt"], check=True, env=env)
        subprocess.run(["git", "-C", str(nested), "commit", "-qm", "feat: nested"],
                       check=True, env=env)

        real = config.GIT_SCAN_ROOTS
        config.GIT_SCAN_ROOTS = [root]
        try:
            source = GitCommits()
            self.assertTrue(source.available())
            today = datetime.datetime.now(config.TZ).date()
            found = list(source.fetch(today))
        finally:
            config.GIT_SCAN_ROOTS = real
        self.assertEqual(len(found), 1, "a repo two levels down was not scanned")
        self.assertEqual(found[0].body, "feat: nested")


class TestWebServer(unittest.TestCase):
    """The server can read the whole diary, so its guards are tested, not assumed."""

    def setUp(self):
        import secrets, threading, time


        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self._db_path = pathlib.Path(handle.name)
        db.init_db(db.connect(self._db_path))
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self._db_path

        self.token = secrets.token_urlsafe(8)
        self.server = web.serve(0, self.token, read_only=False)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        time.sleep(0.2)

    def tearDown(self):
        self.server.shutdown()
        config.DB_PATH = self._real_db_path
        self._db_path.unlink(missing_ok=True)

    def _get(self, path, token=None):
        import urllib.error, urllib.request
        headers = {"X-Lifelog-Token": token} if token else {}
        request = urllib.request.Request(
            "http://127.0.0.1:{}{}".format(self.port, path), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_api_requires_a_token(self):
        self.assertEqual(self._get("/api/stats"), 403)

    def test_wrong_token_is_rejected(self):
        self.assertEqual(self._get("/api/stats", "wrong"), 403)

    def test_correct_token_is_accepted(self):
        self.assertEqual(self._get("/api/stats", self.token), 200)

    def test_read_only_write_is_403_and_refused(self):
        import json, urllib.request, urllib.error
        server = web.serve(0, self.token + "ro", read_only=True)
        port = server.server_address[1]
        import threading, time
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.2)
        request = urllib.request.Request(
            "http://127.0.0.1:{}/api/note".format(port),
            data=json.dumps({"text": "should not land"}).encode(),
            headers={"X-Lifelog-Token": self.token + "ro",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        server.shutdown()
        self.assertEqual(status, 403)

    def test_root_serves_fallback_page_without_ui_dir(self):
        if web.UI_DIR.is_dir():
            self.skipTest("a real ui/ exists in this checkout")
        self.assertEqual(self._get("/"), 200)

    def test_unknown_endpoint_is_404_not_500(self):
        self.assertEqual(self._get("/api/nope", self.token), 404)

    def test_path_traversal_cannot_escape_ui(self):
        self.assertIn(self._get("/../lifelog/config.py"), (403, 404))

    def test_binds_loopback_only(self):
        """Never 0.0.0.0 — nothing on the LAN may reach the diary."""
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class TestWebReadOnly(unittest.TestCase):
    def test_read_only_router_refuses_writes(self):
        con = temp_db()
        router = web.Router(con, read_only=True)
        with self.assertRaises(web.ReadOnlyServer):
            router.post("/api/note", {"text": "hi"})

    def test_writable_router_accepts_a_note(self):
        con = temp_db()
        result = web.Router(con, read_only=False).post(
            "/api/note", {"text": "ran 5k", "day": "2026-08-13"})
        self.assertTrue(result["added"])

    def test_empty_note_is_rejected(self):
        con = temp_db()
        self.assertIn("error", web.Router(con, False).post("/api/note", {"text": "  "}))


class TestCategoryEditing(unittest.TestCase):
    """R-024: the owner files their own bullets, and the model's proposals are
    invisible until they say otherwise."""

    def test_owner_refiling_locks_out_automation(self):
        con = temp_db()
        bid = bullets.add_human(con, "2026-08-13", "swam 300m", "other", edited_by="diary")
        query.set_bullet_category(con, bid, "sport")
        row = con.execute("SELECT category_id, edited_by FROM bullets WHERE id=?",
                          (bid,)).fetchone()
        self.assertEqual(row["category_id"], "sport")
        self.assertEqual(row["edited_by"], "human-category")

        bullets.reclassify(con, bid, "psych")
        self.assertEqual(
            con.execute("SELECT category_id FROM bullets WHERE id=?", (bid,)).fetchone()[0],
            "sport")

    def test_owner_created_category_is_active_immediately(self):
        con = temp_db()
        result = query.create_category(con, "Cooking")
        self.assertTrue(result["ok"])
        self.assertIn("cooking", [c["id"] for c in classify.active(con)])

    def test_model_proposal_needs_approval(self):
        con = temp_db()
        classify.propose_category(con, "Woodwork", "made a shelf")
        self.assertNotIn("woodwork", [c["id"] for c in classify.active(con)])
        query.decide_category(con, "woodwork", "approve")
        self.assertIn("woodwork", [c["id"] for c in classify.active(con)])

    def test_rejecting_a_proposal_archives_it(self):
        con = temp_db()
        classify.propose_category(con, "Woodwork", "one-off")
        query.decide_category(con, "woodwork", "reject")
        ids = [c["id"] for c in query.list_categories(con)]
        self.assertNotIn("woodwork", ids)

    def test_unknown_action_is_rejected(self):
        con = temp_db()
        self.assertIn("error", query.decide_category(con, "x", "destroy"))

    def test_list_categories_includes_proposals(self):
        con = temp_db()
        classify.propose_category(con, "Woodwork", "why")
        statuses = {c["id"]: c["status"] for c in query.list_categories(con)}
        self.assertEqual(statuses.get("woodwork"), "proposed")


class TestBulletEditing(unittest.TestCase):
    """R-026: correct any bullet, any day — and the fix must survive tonight."""

    def test_correcting_an_ai_bullet_survives_regeneration(self):
        con = temp_db()
        bid = bullets.propose(con, "2026-08-13", "ai_eng", "model got this wrong")
        query.edit_bullet(con, bid, "the correct version")
        bullets.supersede_ai_for_day(con, "2026-08-13")
        self.assertEqual([r["text"] for r in bullets.for_day(con, "2026-08-13")],
                         ["the correct version"])

    def test_uncorrected_ai_bullets_are_still_replaced(self):
        con = temp_db()
        bullets.propose(con, "2026-08-13", "ai_eng", "stale")
        bullets.supersede_ai_for_day(con, "2026-08-13")
        self.assertEqual(bullets.for_day(con, "2026-08-13"), [])

    def test_every_version_is_retained(self):
        con = temp_db()
        bid = bullets.propose(con, "2026-08-13", "ai_eng", "v1")
        query.edit_bullet(con, bid, "v2")
        self.assertEqual(
            con.execute("SELECT count(*) FROM bullets").fetchone()[0], 2)

    def test_empty_edit_is_refused(self):
        con = temp_db()
        bid = bullets.propose(con, "2026-08-13", "ai_eng", "v1")
        self.assertIn("error", query.edit_bullet(con, bid, "   "))

    def test_editing_a_missing_bullet_errors_cleanly(self):
        self.assertIn("error", query.edit_bullet(temp_db(), 9999, "text"))


class TestDwellFiltering(unittest.TestCase):
    """"Opened it" is not "did it" — the source of two hallucinated bullets."""

    def test_brief_visits_are_excluded_from_the_prompt(self):
        con = temp_db()
        stamp = datetime.datetime(2026, 8, 13, 9, 0, tzinfo=datetime.timezone.utc)
        db.insert_events(con, [
            Event.make(stamp, "browser", "visit", "b:1",
                       title="Some Long Book", body="http://x", duration_s=4),
            Event.make(stamp, "browser", "visit", "b:2",
                       title="n8n crash course", body="http://y", duration_s=900),
        ])
        _, user = digest.build_prompt(con, "2026-08-13")
        self.assertIn("n8n crash course", user)
        self.assertNotIn("Some Long Book", user)

    def test_thresholds_are_configured_not_hardcoded(self):
        self.assertGreater(config.MIN_DWELL_S, 0)
        self.assertGreater(config.MIN_APP_S, config.MIN_DWELL_S)


class TestQueryLayer(unittest.TestCase):
    def test_search_finds_and_filters_by_origin(self):
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "studied n8n crash course")
        bullets.propose(con, "2026-08-11", "ai_eng", "explored n8n workflows")
        self.assertEqual(len(query.search_bullets(con, "n8n")), 2)
        self.assertEqual(len(query.search_bullets(con, "n8n", origin="human")), 1)

    def test_unknown_tool_returns_error_not_exception(self):
        con = temp_db()
        self.assertIn("error", query.call(con, "no_such_tool", {}))

    def test_bad_arguments_return_error_not_exception(self):
        con = temp_db()
        self.assertIn("error", query.call(con, "get_day", {"wrong": 1}))

    def test_add_note_is_excluded_from_read_only(self):
        self.assertNotIn("add_note", query.READ_ONLY)
        self.assertIn("search_bullets", query.READ_ONLY)

    def test_every_tool_has_a_dispatch_entry(self):
        for tool in query.TOOLS:
            self.assertIn(tool["name"], query.DISPATCH)

    def test_stats_on_an_empty_database(self):
        result = query.stats(temp_db())
        self.assertEqual(result["days_logged"], 0)
        self.assertIsNone(result["first_day"])


class TestEditHistory(unittest.TestCase):
    def test_edit_inserts_and_supersedes(self):
        con = temp_db()
        first = bullets.propose(con, "2026-08-11", "ai_eng", "v1")
        second = bullets.edit(con, first, "v2", edited_by="human")
        rows = con.execute("SELECT id,status FROM bullets ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        statuses = {r["id"]: r["status"] for r in rows}
        self.assertEqual(statuses[first], "superseded")
        self.assertEqual(statuses[second], "current")
        self.assertEqual(len(bullets.history(con, first)), 2)


class TestObsidianParser(unittest.TestCase):
    DIARY = (
        "12/08/2026\n"
        "- \n"
        "- learned new sorting algorithms with examples \n"
        "     -  быстрая сортировка\n"
        "     -  Сортировка слиянием\n"
        "\n"
        "11/08/2026\n"
        "\t- Checked repo\n"
        "\t- Learned about **Goodhart's Law:**\n"
        "\t\t- when a measure becomes a target\n"
    )

    def test_nested_children_are_kept(self):
        got = parse_day_bullets(self.DIARY, datetime.date(2026, 8, 12))
        self.assertEqual(len(got), 1)
        self.assertIn("быстрая сортировка", got[0])
        self.assertIn("Сортировка слиянием", got[0])

    def test_empty_bullet_does_not_swallow_the_day(self):
        """A stray '-' at column 0 once reset the base indent and dropped
        every bullet for that date."""
        got = parse_day_bullets(self.DIARY, datetime.date(2026, 8, 12))
        self.assertTrue(got)

    def test_stops_at_next_date(self):
        got = parse_day_bullets(self.DIARY, datetime.date(2026, 8, 11))
        self.assertEqual(len(got), 2)
        self.assertIn("Goodhart", got[1])

    def test_missing_date_returns_empty(self):
        self.assertEqual(parse_day_bullets(self.DIARY, datetime.date(2026, 1, 1)), [])


class TestClassifier(unittest.TestCase):
    def test_goodharts_law_is_psychology_not_finance(self):
        """A sub-point mentioning money must not drag the bullet into Finance."""
        con = temp_db()
        text = ("Learned about **Goodhart's Law:**\n"
                "  - Cobra effect: pay money for dead cobras and people breed them")
        self.assertEqual(classify.rule_match(con, text), "psych")

    def test_falls_back_to_children_when_heading_is_bare(self):
        con = temp_db()
        self.assertEqual(classify.rule_match(con, "Sport:\n  - swimming: 300m"), "sport")

    def test_no_substring_false_positive(self):
        con = temp_db()
        self.assertIsNone(classify.rule_match(con, "moneyball is a film about baseball xyz"))

    def test_proposed_category_is_not_active(self):
        con = temp_db()
        key = classify.propose_category(con, "Cooking", "made bread twice")
        self.assertIsNotNone(key)
        self.assertNotIn(key, [r["id"] for r in classify.active(con)])
        classify.approve(con, key)
        self.assertIn(key, [r["id"] for r in classify.active(con)])

    def test_cap_blocks_sprawl(self):
        con = temp_db()
        for n in range(30):
            classify.propose_category(con, "Domain {}".format(n))
        total = len(classify.active(con)) + len(classify.proposed(con))
        self.assertLessEqual(total, config.DOMAIN_CAP)

    def test_merge_relabels_without_rewriting(self):
        con = temp_db()
        classify.merge(con, "language", "learning")
        self.assertEqual(classify.canonical(con, "language"), "learning")


class TestDigestParser(unittest.TestCase):
    def test_accepts_bracket_bold_and_colon_forms(self):
        raw = ("## OVERVIEW\nA day.\n\n## BULLETS\n"
               "- [AI Engineering] alpha\n"
               "- **Career** beta\n"
               "- Finance: gamma\n"
               "- untagged delta\n")
        overview, parsed = digest.parse(raw)
        self.assertEqual(overview, "A day.")
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[0], ("AI Engineering", "alpha"))
        self.assertEqual(parsed[1], ("Career", "beta"))
        self.assertEqual(parsed[3][0], "")

    def test_no_bullets_header_still_parses(self):
        _, parsed = digest.parse("- [Career] applied somewhere")
        self.assertEqual(len(parsed), 1)


class TestRedaction(unittest.TestCase):
    def test_secrets_are_stripped(self):
        self.assertIn("redacted", redact("export GITHUB_TOKEN=ghp_abc123"))
        self.assertEqual(redact("git status"), "git status")


class TestRunRecording(unittest.TestCase):
    def test_failed_run_is_recorded_and_visible(self):
        con = temp_db()
        started = db.start_run(con, "nightly", "2026-08-11")
        db.finish_run(con, started, "failed", 3, {"model": {"error": "ollama unreachable"}})
        row = db.last_run_for_day(con, "2026-08-11")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(len(db.recent_failures(con)), 1)


class TestRollupPeriods(unittest.TestCase):
    def test_week_and_month_keys(self):
        self.assertEqual(rollup.week_key(datetime.date(2026, 8, 12)), "2026-W33")
        self.assertEqual(rollup.month_key(datetime.date(2026, 8, 12)), "2026-08")

    def test_week_days_are_monday_to_sunday(self):
        days = rollup.week_days("2026-W33")
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0], "2026-08-10")
        self.assertEqual(days[-1], "2026-08-16")

    def test_month_days_length(self):
        self.assertEqual(len(rollup.month_days("2026-08")), 31)
        self.assertEqual(len(rollup.month_days("2026-02")), 28)
        self.assertEqual(len(rollup.month_days("2026-12")), 31)

    def test_weekly_is_due_after_sunday_only(self):
        self.assertEqual(rollup.due(datetime.date(2026, 8, 16)),
                         [("week", "2026-W33")])
        self.assertEqual(rollup.due(datetime.date(2026, 8, 12)), [])

    def test_monthly_is_due_after_the_last_day(self):
        self.assertIn(("month", "2026-08"), rollup.due(datetime.date(2026, 8, 31)))

    def test_both_can_be_due_together(self):

        jobs = rollup.due(datetime.date(2026, 5, 31))
        self.assertEqual(len(jobs), 2)


class TestRollupGuard(unittest.TestCase):
    """The rollup waits for the daily run to FINISH, not to succeed."""

    def test_blocks_while_daily_is_running(self):
        con = temp_db()
        db.start_run(con, "nightly", "2026-08-11")
        self.assertFalse(rollup.daily_run_finished(con, "2026-08-11"))

    def test_allows_after_a_failed_daily_run(self):
        con = temp_db()
        started = db.start_run(con, "nightly", "2026-08-11")
        db.finish_run(con, started, "failed", 3, {"model": {"error": "down"}})
        self.assertTrue(rollup.daily_run_finished(con, "2026-08-11"))

    def test_blocks_when_the_day_was_never_run(self):
        con = temp_db()
        self.assertFalse(rollup.daily_run_finished(con, "2026-01-01"))

    def test_midperiod_review_is_not_treated_as_done(self):
        """A month summarised on the 12th must be regenerated at month end,
        otherwise August is described forever by its first week."""
        con = temp_db()
        db.save_digest(con, "month", "2026-08", "m", "partial, written on the 12th")
        con.execute("UPDATE digests SET generated_at='2026-08-12T00:00:00Z'")
        con.commit()
        self.assertFalse(rollup._is_current(con, "month", "2026-08"))

    def test_review_written_after_the_period_closed_is_kept(self):
        con = temp_db()
        db.save_digest(con, "month", "2026-08", "m", "complete")
        con.execute("UPDATE digests SET generated_at='2026-09-01T00:01:00Z'")
        con.commit()
        self.assertTrue(rollup._is_current(con, "month", "2026-08"))

    def test_skips_a_period_with_too_little_data(self):
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "only one day")
        status, error = rollup.generate(con, "week", "2026-W33")
        self.assertEqual(status, "skipped")
        self.assertIn("logged day", error)


class TestYearlyRollup(unittest.TestCase):
    def test_year_is_due_after_31_december(self):
        jobs = rollup.due(datetime.date(2026, 12, 31))
        self.assertIn(("month", "2026-12"), jobs)
        self.assertIn(("year", "2026"), jobs)

    def test_month_is_written_before_the_year(self):
        jobs = rollup.due(datetime.date(2026, 12, 31))
        self.assertLess(jobs.index(("month", "2026-12")), jobs.index(("year", "2026")))

    def test_year_not_due_on_any_other_month_end(self):
        self.assertNotIn(("year", "2026"), rollup.due(datetime.date(2026, 8, 31)))

    def test_year_days_handles_leap_years(self):
        self.assertEqual(len(rollup.year_days("2026")), 365)
        self.assertEqual(len(rollup.year_days("2028")), 366)


class TestPushIsContentFree(unittest.TestCase):
    """A push leaves the machine. Diary content must never be in it."""

    def test_only_safe_fields_survive_filtering(self):
        facts = {"day": "2026-08-12", "status": "ok", "bullets_ai": 6,
                 "bullet_text": "swimming 300m", "overview": "private prose"}
        safe = {k: v for k, v in facts.items() if k in notify.SAFE_FIELDS}
        self.assertNotIn("bullet_text", safe)
        self.assertNotIn("overview", safe)
        self.assertEqual(set(safe), {"day", "status", "bullets_ai"})

    def test_header_is_latin1_encodable(self):
        """An em dash in the title raised UnicodeEncodeError inside urllib and
        the push disappeared with no error anywhere."""
        cleaned = notify._header_safe("Life Log ⚠️ — 2026-08-12 · ok")
        cleaned.encode("latin-1")
        self.assertNotIn("—", cleaned)

    def test_push_is_a_noop_without_a_topic(self):
        original = config.LOCAL_CONFIG
        config.LOCAL_CONFIG = pathlib.Path("/nonexistent/local.json")
        try:
            self.assertFalse(notify.push("t", {"day": "x"}))
        finally:
            config.LOCAL_CONFIG = original


class TestMetrics(unittest.TestCase):
    def test_counts_split_by_origin(self):
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "mine one")
        bullets.add_human(con, "2026-08-11", "mine two")
        bullets.propose(con, "2026-08-11", "ai_eng", "model one")
        result = metrics.compute_day(con, "2026-08-11")
        self.assertEqual(result["bullets_human"], 2)
        self.assertEqual(result["bullets_ai"], 1)
        self.assertEqual(result["bullets_total"], 3)

    def test_recomputing_does_not_double(self):
        con = temp_db()
        bullets.add_human(con, "2026-08-11", "one")
        metrics.compute_day(con, "2026-08-11")
        metrics.compute_day(con, "2026-08-11")
        n = con.execute("SELECT count(*) c FROM metrics WHERE day=? AND metric='bullets_total'",
                        ("2026-08-11",)).fetchone()["c"]
        self.assertEqual(n, 1)


class TestDashboardHelpers(unittest.TestCase):
    def test_sparkline_length_and_bounds(self):
        self.assertEqual(len(dashboard.sparkline([1, 5, 3, 9])), 4)
        self.assertEqual(dashboard.sparkline([]), "")
        self.assertEqual(dashboard.sparkline([4, 4, 4]), "▄▄▄")

    def test_streak_counts_consecutive_days(self):
        self.assertEqual(dashboard.streak(
            ["2026-08-10", "2026-08-11", "2026-08-12"]), 3)

    def test_streak_stops_at_a_gap(self):
        self.assertEqual(dashboard.streak(
            ["2026-08-01", "2026-08-11", "2026-08-12"]), 2)

    def test_streak_of_nothing(self):
        self.assertEqual(dashboard.streak([]), 0)


if __name__ == "__main__":
    unittest.main()
