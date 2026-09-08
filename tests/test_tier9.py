"""Tests for the Explorer and the cockpit's shell.

Covers:
  Phase 12  The Explorer - orchestrator.explorer's confinement, listing, reading, git overlay
            and name search; the three read-only routes daemon.py exposes them on; and the
            properties of `cockpit.html` that are claims about behaviour rather than taste.

The confinement tests are the ones that matter, and they are written the way a security
property has to be: not "does the happy path work" but "is every way out of the tree the same
answer". A symlink pointing out of the root is included because that is the escape a string
comparison on the *requested* path cannot see - only resolving both ends catches it, which is
what `resolve_within` does.

The page tests read the real `cockpit.html` off disk for the same reason the Phase 11 tests
read `desktop/`: a claim about what a served page may do is only worth making against what is
actually going to be served.
"""

import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from orchestrator.daemon import TOKEN_HEADER, Daemon, serve_daemon
from orchestrator.explorer import (
    MAX_ENTRIES,
    NOISE_DIRECTORIES,
    decorate,
    find_files,
    git_status,
    language_of,
    list_directory,
    looks_binary,
    read_file,
    relative_to,
    resolve_within,
    root_path,
    roots,
    snapshot,
)

WEB_DIR = Path(__file__).resolve().parents[1] / "orchestrator" / "web"

MINIMAL_YAML = """
agents:
  - agent: claude
    model: sonnet
    role: developer
"""


def _cfg():
    from orchestrator.config import load_config

    return load_config(None)


class _Tree(unittest.TestCase):
    """A small real tree on disk. Nothing here is mocked: the module's whole job is the
    filesystem, and a mocked filesystem would prove nothing about traversal."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t9_")
        self.outside = tempfile.mkdtemp(prefix="orch_t9_out_")
        os.makedirs(os.path.join(self.root, "src", "deep"))
        os.makedirs(os.path.join(self.root, "__pycache__"))
        os.makedirs(os.path.join(self.root, ".hidden"))
        self._write("README.md", "# hello\n")
        self._write("src/app.py", "def main():\n    return 1\n")
        self._write("src/deep/note.txt", "deep\n")
        self._write("__pycache__/x.pyc", "cached\n")
        self._write(".hidden/secret.txt", "shh\n")
        self._write(".dotfile", "dot\n")
        with open(os.path.join(self.outside, "prize.txt"), "w", encoding="utf-8") as handle:
            handle.write("should never be readable\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def _write(self, relative, text):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _names(self, listing):
        return [entry["name"] for entry in listing["entries"]]


# ---------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------


class TestConfinement(_Tree):
    def test_the_root_itself_resolves(self):
        self.assertEqual(
            os.path.normcase(resolve_within(self.root, "")),
            os.path.normcase(os.path.realpath(self.root)),
        )

    def test_a_path_inside_resolves(self):
        self.assertTrue(resolve_within(self.root, "src/app.py").endswith("app.py"))

    def test_dot_segments_are_harmless(self):
        self.assertTrue(resolve_within(self.root, "./src/./app.py").endswith("app.py"))

    def test_climbing_out_is_refused(self):
        for attempt in ("..", "../..", "../../etc/passwd", "src/../..", "src/../../x"):
            with self.subTest(attempt=attempt):
                self.assertIsNone(resolve_within(self.root, attempt))

    def test_an_absolute_path_is_refused(self):
        for attempt in ("/etc/passwd", "C:/Windows/win.ini", "\\\\server\\share"):
            with self.subTest(attempt=attempt):
                self.assertIsNone(resolve_within(self.root, attempt))

    def test_a_leading_slash_is_refused_rather_than_reinterpreted(self):
        """The bug this closes: stripping separators first turns "/etc/passwd" into the
        root-relative "etc/passwd", so the absolute check never sees what it is checking."""
        self.assertIsNone(resolve_within(self.root, "/etc/passwd"))

    def test_a_sibling_whose_name_merely_starts_with_the_root_is_refused(self):
        """Prefix matching without a separator would accept this; the separator is the fix."""
        sibling = self.root + "-secrets"
        os.makedirs(sibling, exist_ok=True)
        try:
            self.assertIsNone(resolve_within(self.root, "../" + os.path.basename(sibling)))
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

    def test_a_symlink_out_of_the_tree_is_refused(self):
        link = os.path.join(self.root, "escape")
        try:
            os.symlink(self.outside, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("this platform will not create a symlink without privileges")
        self.assertIsNone(resolve_within(self.root, "escape"))
        self.assertIsNone(resolve_within(self.root, "escape/prize.txt"))

    def test_reading_through_an_escape_is_refused_not_just_listed(self):
        result = read_file(self.root, "../../etc/passwd")
        self.assertIn("outside", result["error"])
        self.assertNotIn("text", result)

    def test_listing_through_an_escape_is_refused(self):
        self.assertIn("outside", list_directory(self.root, "..")["error"])

    def test_relative_to_is_forward_slashed(self):
        deep = os.path.join(self.root, "src", "deep")
        self.assertEqual(relative_to(self.root, deep), "src/deep")
        self.assertEqual(relative_to(self.root, self.root), "")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListing(_Tree):
    def test_noise_and_dotfiles_are_hidden_by_default(self):
        names = self._names(list_directory(self.root))
        self.assertIn("src", names)
        self.assertIn("README.md", names)
        self.assertNotIn("__pycache__", names)
        self.assertNotIn(".hidden", names)
        self.assertNotIn(".dotfile", names)

    def test_hidden_can_be_asked_for(self):
        names = self._names(list_directory(self.root, include_hidden=True))
        self.assertIn("__pycache__", names)
        self.assertIn(".dotfile", names)

    def test_pycache_is_noise_everywhere(self):
        self.assertIn("__pycache__", NOISE_DIRECTORIES)
        self.assertIn("node_modules", NOISE_DIRECTORIES)

    def test_directories_come_first_then_case_insensitive_name(self):
        self._write("Alpha.md", "a")
        self._write("zebra.md", "z")
        os.makedirs(os.path.join(self.root, "zzz_dir"), exist_ok=True)
        entries = list_directory(self.root)["entries"]
        kinds = [entry["kind"] for entry in entries]
        self.assertEqual(kinds, sorted(kinds, key=lambda k: 0 if k == "dir" else 1))
        files = [e["name"] for e in entries if e["kind"] == "file"]
        self.assertEqual(files, sorted(files, key=str.lower))

    def test_a_file_carries_its_size_and_language(self):
        entry = next(e for e in list_directory(self.root)["entries"] if e["name"] == "README.md")
        self.assertEqual(entry["language"], "markdown")
        self.assertGreater(entry["size"], 0)
        self.assertEqual(entry["kind"], "file")

    def test_a_directory_is_not_recursed_into(self):
        """One scandir per request is the property that makes this cheap on a monorepo."""
        paths = [entry["path"] for entry in list_directory(self.root)["entries"]]
        self.assertIn("src", paths)
        self.assertNotIn("src/app.py", paths)

    def test_listing_a_file_is_an_error_not_a_crash(self):
        self.assertIn("not a directory", list_directory(self.root, "README.md")["error"])

    def test_a_big_directory_says_it_was_capped(self):
        crowd = os.path.join(self.root, "crowd")
        os.makedirs(crowd, exist_ok=True)
        for index in range(12):
            with open(os.path.join(crowd, "f%02d.txt" % index), "w", encoding="utf-8") as h:
                h.write("x")
        listing = list_directory(self.root, "crowd", max_entries=5)
        self.assertEqual(len(listing["entries"]), 5)
        self.assertTrue(listing["truncated"])
        self.assertEqual(listing["total"], 12)

    def test_the_default_cap_is_a_real_ceiling(self):
        self.assertGreater(MAX_ENTRIES, 0)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class TestReading(_Tree):
    def test_text_comes_back_with_its_language_and_line_count(self):
        result = read_file(self.root, "src/app.py")
        self.assertEqual(result["language"], "python")
        self.assertEqual(result["lines"], 2)
        self.assertFalse(result["truncated"])
        self.assertFalse(result["binary"])
        self.assertIn("def main", result["text"])

    def test_a_long_file_is_cut_and_says_so(self):
        self._write("big.txt", "x" * 5000)
        result = read_file(self.root, "big.txt", max_bytes=1000)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), 1000)
        self.assertEqual(result["size"], 5000)

    def test_a_binary_file_is_named_not_decoded(self):
        path = os.path.join(self.root, "blob.bin")
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\x00\x01\x02binary\x00")
        result = read_file(self.root, "blob.bin")
        self.assertTrue(result["binary"])
        self.assertEqual(result["text"], "")

    def test_looks_binary_uses_the_nul_byte(self):
        self.assertTrue(looks_binary(b"abc\x00def"))
        self.assertFalse(looks_binary(b"plain text\n"))
        self.assertFalse(looks_binary("héllo wörld".encode("utf-8")))

    def test_a_truncated_multibyte_character_is_not_binary(self):
        """A sniff that ends mid-character must not condemn a UTF-8 file."""
        sample = ("a" * 10 + "é").encode("utf-8")[:-1]
        self.assertFalse(looks_binary(sample))

    def test_reading_a_directory_is_an_error(self):
        self.assertIn("not a file", read_file(self.root, "src")["error"])

    def test_language_of_knows_names_as_well_as_extensions(self):
        self.assertEqual(language_of("Dockerfile"), "docker")
        self.assertEqual(language_of("app.py"), "python")
        self.assertEqual(language_of("weird.zzz"), "")


# ---------------------------------------------------------------------------
# The git overlay
# ---------------------------------------------------------------------------


class TestGitOverlay(_Tree):
    def test_a_tree_that_is_not_a_repository_gets_an_empty_overlay(self):
        """Decoration degrades to nothing rather than to an error the UI must handle."""
        self.assertEqual(git_status(self.root), {})

    def test_a_missing_directory_gets_an_empty_overlay(self):
        self.assertEqual(git_status(os.path.join(self.root, "nope")), {})

    def test_decorate_gives_a_directory_the_state_of_what_is_under_it(self):
        entries = [
            {"name": "src", "path": "src", "kind": "dir"},
            {"name": "README.md", "path": "README.md", "kind": "file"},
            {"name": "quiet", "path": "quiet", "kind": "dir"},
        ]
        decorated = decorate(entries, {"src/app.py": "modified", "README.md": "untracked"})
        by_name = {entry["name"]: entry["git"] for entry in decorated}
        self.assertEqual(by_name["src"], "modified")
        self.assertEqual(by_name["README.md"], "untracked")
        self.assertEqual(by_name["quiet"], "")

    def test_decorate_does_not_match_a_prefix_that_is_not_a_directory_boundary(self):
        decorated = decorate(
            [{"name": "src", "path": "src", "kind": "dir"}], {"srcfile.py": "modified"}
        )
        self.assertEqual(decorated[0]["git"], "")

    def test_decorate_is_pure(self):
        entries = [{"name": "a", "path": "a", "kind": "file"}]
        decorate(entries, {"a": "modified"})
        self.assertNotIn("git", entries[0])


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestFindFiles(_Tree):
    def test_an_empty_query_finds_nothing_rather_than_everything(self):
        self.assertEqual(find_files(self.root, "")["matches"], [])
        self.assertEqual(find_files(self.root, "   ")["matches"], [])

    def test_it_matches_case_insensitively_on_the_name(self):
        paths = [m["path"] for m in find_files(self.root, "APP")["matches"]]
        self.assertIn("src/app.py", paths)

    def test_it_skips_noise_directories(self):
        paths = [m["path"] for m in find_files(self.root, "x.pyc")["matches"]]
        self.assertEqual(paths, [])

    def test_an_exact_name_sorts_first(self):
        self._write("src/deep/app.py", "deeper\n")
        self._write("application.py", "other\n")
        matches = find_files(self.root, "app.py")["matches"]
        self.assertEqual(matches[0]["name"], "app.py")
        self.assertEqual(matches[0]["path"], "src/app.py")   # shallowest of the exact ones

    def test_the_visit_ceiling_stops_it_and_says_so(self):
        result = find_files(self.root, "e", max_visited=1)
        self.assertTrue(result["truncated"])

    def test_the_limit_stops_it_and_says_so(self):
        for index in range(6):
            self._write("many/thing%d.txt" % index, "x")
        result = find_files(self.root, "thing", limit=2)
        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["truncated"])


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------


class TestRoots(_Tree):
    def test_the_project_is_always_the_first_root(self):
        found = roots(self.root, None)
        self.assertEqual(found[0]["id"], "project")
        self.assertEqual(found[0]["kind"], "project")

    def test_a_worktree_on_disk_becomes_a_root(self):
        os.makedirs(os.path.join(self.root, ".orchestrator", "worktrees", "run-1"))
        ids = [root["id"] for root in roots(self.root, None)]
        self.assertIn("worktree:run-1", ids)

    def test_a_worktree_that_was_cleaned_up_simply_stops_being_offered(self):
        path = os.path.join(self.root, ".orchestrator", "worktrees", "run-1")
        os.makedirs(path)
        self.assertIn("worktree:run-1", [r["id"] for r in roots(self.root, None)])
        shutil.rmtree(path)
        self.assertNotIn("worktree:run-1", [r["id"] for r in roots(self.root, None)])

    def test_the_run_store_appears_only_when_it_exists(self):
        self.assertNotIn("runs", [root["id"] for root in roots(self.root, None)])
        os.makedirs(os.path.join(self.root, ".orchestrator", "runs"))
        self.assertIn("runs", [root["id"] for root in roots(self.root, None)])

    def test_an_unknown_root_id_has_no_path(self):
        """Lookup is by identity, so an id that is not a root is not a place to start."""
        self.assertIsNone(root_path(self.root, None, "worktree:../.."))
        self.assertIsNone(root_path(self.root, None, "made-up"))

    def test_snapshot_refuses_an_unknown_root_but_still_lists_the_real_ones(self):
        payload = snapshot(self.root, None, root_id="nope")
        self.assertIn("no root", payload["error"])
        self.assertEqual(payload["roots"][0]["id"], "project")

    def test_snapshot_decorates_and_carries_the_roots(self):
        payload = snapshot(self.root, None)
        self.assertEqual(payload["root"], "project")
        self.assertTrue(payload["roots"])
        self.assertTrue(all("git" in entry for entry in payload["entries"]))


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


class TestTheExplorerRoutes(unittest.TestCase):
    """A real daemon on a real port. The refusals are the point of the class."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t9serve_")
        os.makedirs(os.path.join(self.root, "src"))
        with open(os.path.join(self.root, "src", "app.py"), "w", encoding="utf-8") as handle:
            handle.write("value = 1\n")
        config_file = os.path.join(self.root, "orchestrator.yaml")
        with open(config_file, "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_YAML)

        self.daemon = Daemon(
            self.root, _cfg(), config_path=config_file, token="test-token",
            goal_runner=lambda daemon, job: {"summary": {}},
        )
        self.server = serve_daemon(
            self.root, _cfg(), port=0, open_browser=False, serve_forever=False,
            printer=lambda *_: None, daemon=self.daemon,
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.daemon.stopping.set()
        self.daemon.capture_agent_output(False)
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _get(self, path, token="test-token", origin=None, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            request.add_header(TOKEN_HEADER, token)
        if origin:
            request.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(text or "{}")
            except ValueError:
                return exc.code, text

    def test_the_tree_reads(self):
        status, body = self._get("/api/explorer")
        self.assertEqual(status, 200)
        self.assertIn("src", [entry["name"] for entry in body["entries"]])
        self.assertEqual(body["roots"][0]["id"], "project")

    def test_a_subdirectory_reads(self):
        _, body = self._get("/api/explorer?path=src")
        self.assertEqual([entry["name"] for entry in body["entries"]], ["app.py"])

    def test_a_file_reads(self):
        status, body = self._get("/api/file?path=src/app.py")
        self.assertEqual(status, 200)
        self.assertEqual(body["language"], "python")
        self.assertIn("value = 1", body["text"])

    def test_the_name_search_reads(self):
        _, body = self._get("/api/find?q=app")
        self.assertEqual([match["path"] for match in body["matches"]], ["src/app.py"])

    def test_hidden_is_opt_in_over_the_wire(self):
        os.makedirs(os.path.join(self.root, ".secretdir"))
        _, plain = self._get("/api/explorer")
        _, shown = self._get("/api/explorer?hidden=1")
        self.assertNotIn(".secretdir", [entry["name"] for entry in plain["entries"]])
        self.assertIn(".secretdir", [entry["name"] for entry in shown["entries"]])

    # -- the refusals ------------------------------------------------------

    def test_every_explorer_route_needs_the_token(self):
        for route in ("/api/explorer", "/api/file?path=src/app.py", "/api/find?q=app"):
            with self.subTest(route=route):
                self.assertEqual(self._get(route, token=None)[0], 401)

    def test_every_explorer_route_refuses_a_foreign_origin(self):
        for route in ("/api/explorer", "/api/file?path=src/app.py", "/api/find?q=app"):
            with self.subTest(route=route):
                self.assertEqual(self._get(route, origin="http://evil.example")[0], 403)

    def test_a_traversal_over_the_wire_is_refused(self):
        _, body = self._get("/api/explorer?path=../..")
        self.assertIn("outside", body["error"])

    def test_reading_a_file_outside_the_root_is_refused(self):
        status, body = self._get("/api/file?path=../../etc/passwd")
        self.assertEqual(status, 404)
        self.assertIn("outside", body["error"])

    def test_an_invented_root_is_refused(self):
        _, body = self._get("/api/explorer?root=worktree:../../..")
        self.assertIn("no root", body["error"])

    def test_the_explorer_is_not_reachable_by_post(self):
        """Browsing is watching. There is no write here, and the method says so."""
        for route in ("/api/explorer", "/api/file", "/api/find"):
            with self.subTest(route=route):
                status, _ = self._get(route, method="POST", body={})
                self.assertEqual(status, 404)

    def test_control_is_still_a_closed_list_after_phase_12(self):
        self.assertEqual(
            self.daemon.control("explore", {"path": "src"}),
            {"ok": False, "unknown": True, "error": "no control action 'explore'"},
        )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


class TestVendoredLibraries(TestTheExplorerRoutes):
    """`/vendor/<name>` is an allow-list, not a static-file server.

    Inherits the daemon fixture above rather than standing up a second one: the property
    being tested is about the same server.
    """

    def test_a_vendored_file_is_served(self):
        request = urllib.request.Request(self.base + "/vendor/gsap.min.js")
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
        self.assertEqual(response.status, 200)
        self.assertIn(b"GSAP", body[:200])

    def test_it_needs_no_token_because_it_is_an_asset(self):
        """Like the page itself: a library is not data about this project."""
        request = urllib.request.Request(self.base + "/vendor/gsap.min.js")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)

    def test_anything_not_on_the_list_is_refused(self):
        for name in ("jquery.js", "nested/evil.js", "../daemon.py", "../../etc/passwd", ""):
            with self.subTest(name=name):
                request = urllib.request.Request(self.base + "/vendor/" + name)
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        served = response.status
                except urllib.error.HTTPError as exc:
                    served = exc.code
                self.assertEqual(served, 404)

    def test_the_file_is_actually_on_disk_so_the_page_can_rely_on_it(self):
        """The whole point of vendoring: no network is consulted to render the cockpit."""
        self.assertTrue((WEB_DIR / "vendor" / "gsap.min.js").is_file())


class TestTheCockpitPage(unittest.TestCase):
    """Properties of the served page that are behaviour, not taste."""

    @classmethod
    def setUpClass(cls):
        cls.text = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")

    def test_the_token_placeholder_is_present_for_the_daemon_to_fill(self):
        self.assertIn("__ORCHESTRATOR_TOKEN__", self.text)

    def test_the_token_is_read_from_the_document_not_the_url(self):
        self.assertIn('meta[name="orchestrator-token"]', self.text)

    def test_nothing_is_loaded_from_the_network(self):
        """The daemon is a loopback process. A page that needs a CDN is a page that breaks
        when the network does, and this one must render with it unplugged."""
        for marker in ("https://", "http://cdn", "src=\"//", "@import url("):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.text.replace("http://127.0.0.1", ""))

    def test_it_asks_the_explorer_routes_by_name(self):
        for route in ("/api/explorer", "/api/file", "/api/find"):
            with self.subTest(route=route):
                self.assertIn(route, self.text)

    def test_the_explorer_is_only_ever_read(self):
        """A GET is the whole of the Explorer's vocabulary in the page as well as the server."""
        for route in ("/api/explorer", "/api/file", "/api/find"):
            with self.subTest(route=route):
                self.assertNotIn('api("' + route + '", {', self.text)

    def test_the_only_writes_are_the_control_routes_and_the_team_editor(self):
        posted = set()
        for index in range(len(self.text)):
            if self.text.startswith('method: "POST"', index):
                window = self.text[max(0, index - 220):index]
                for candidate in ("/api/control/", "/api/team"):
                    if candidate in window:
                        posted.add(candidate)
        self.assertEqual(posted, {"/api/control/", "/api/team"})

    def test_motion_is_disabled_for_anyone_who_asked_for_less_of_it(self):
        self.assertIn("prefers-reduced-motion", self.text)

    def test_the_page_declares_the_keyboard_it_advertises(self):
        for shortcut in ("Ctrl K", "Ctrl B", "Ctrl `", "Ctrl I"):
            with self.subTest(shortcut=shortcut):
                self.assertIn(shortcut, self.text)


if __name__ == "__main__":
    unittest.main()
