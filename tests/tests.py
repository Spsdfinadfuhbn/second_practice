"""Тесты для src/main.py (depgraph).

Запуск из корня репозитория:  python -m unittest discover -s tests -v
Сеть не используется: http_get / urlopen подменяются через unittest.mock.
"""
import ast
import functools
import importlib
import io
import json
import os
import socket
import string
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from urllib.error import HTTPError, URLError

SRC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
sys.path.insert(0, os.path.abspath(SRC_DIR))
dg = importlib.import_module("main")

COMPLEXITY_LIMIT = 10
DEEP_CHAIN_LENGTH = 1500


class TempDirMixin:
    """Временный каталог, создаваемый при первом обращении."""

    @functools.cached_property
    def tmp_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name


def make_graph(repo_text_dict, root, sub=""):
    """Строит граф по словарю {пакет: [зависимости]}."""
    children = dg.make_test_children(repo_text_dict)
    return dg.build_graph(root, "", children, sub)


class ValidatePackageTests(unittest.TestCase):
    def test_empty(self):
        for name in ("", "   "):
            with self.assertRaises(dg.ConfigError):
                dg.validate_package(name, False)

    def test_test_mode_only_uppercase(self):
        dg.validate_package("ABC", True)
        for bad in ("abc", "A1", "A-B", "Ab"):
            with self.assertRaises(dg.ConfigError):
                dg.validate_package(bad, True)

    def test_nuget_names(self):
        for ok in ("Newtonsoft.Json", "System.Text-Json", "a_b", "X"):
            dg.validate_package(ok, False)
        for bad in (".Json", "A..B", "A B", "A/B", "Json."):
            with self.assertRaises(dg.ConfigError):
                dg.validate_package(bad, False)


class ValidateRepoTests(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(dg.ConfigError):
            dg.validate_repo("  ", False)

    def test_url_ok_and_bad(self):
        dg.validate_repo("https://api.nuget.org/v3/index.json", False)
        dg.validate_repo("http://localhost:5000/index.json", False)
        for bad in ("ftp://x/y", "api.nuget.org", "https://", "/tmp/file"):
            with self.assertRaises(dg.ConfigError):
                dg.validate_repo(bad, False)

    def test_file_mode(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "r.txt")
            open(f, "w").close()
            dg.validate_repo(f, True)
            with self.assertRaises(dg.ConfigError):
                dg.validate_repo(os.path.join(d, "missing"), True)
            with self.assertRaises(dg.ConfigError):
                dg.validate_repo(d, True)


class ValidateVersionTests(unittest.TestCase):
    def test_required_outside_test_mode(self):
        with self.assertRaises(dg.ConfigError):
            dg.validate_version("", False)
        dg.validate_version("", True)

    def test_formats(self):
        good = ("1.0", "13.0.3", "1.2.3.4", "1.0.0-beta.1",
                "1.0.0+build.5", "1.0.0-rc.1+meta")
        for ok in good:
            dg.validate_version(ok, False)
        for bad in ("1", "a.b", "1.0.", "1.0.0-", "v1.0", "1..0"):
            with self.assertRaises(dg.ConfigError):
                dg.validate_version(bad, False)


class ValidateOutputTests(TempDirMixin, unittest.TestCase):
    def test_ok(self):
        dg.validate_output(os.path.join(self.tmp_dir, "g.png"))
        dg.validate_output("G.PNG")

    def test_bad_names(self):
        bad_names = ("", "  ", "a<b.png", "a?.png", "graph.jpg", ".png",
                     "graph")
        for bad in bad_names:
            with self.assertRaises(dg.ConfigError, msg=repr(bad)):
                dg.validate_output(bad)

    def test_location_errors(self):
        with self.assertRaises(dg.ConfigError):
            dg.validate_output(os.path.join(self.tmp_dir, "nope", "g.png"))
        os.mkdir(os.path.join(self.tmp_dir, "dir.png"))
        with self.assertRaises(dg.ConfigError):
            dg.validate_output(os.path.join(self.tmp_dir, "dir.png"))

    def test_no_write_permission(self):
        with mock.patch("os.access", return_value=False):
            with self.assertRaises(dg.ConfigError):
                dg.validate_output(os.path.join(self.tmp_dir, "g.png"))


class ValidateFilterTests(unittest.TestCase):
    def test_cases(self):
        dg.validate_filter("", "A")
        dg.validate_filter("Test", "Newtonsoft.Json")
        with self.assertRaises(dg.ConfigError):
            dg.validate_filter("  ", "A")
        with self.assertRaises(dg.ConfigError):
            dg.validate_filter("json", "Newtonsoft.Json")


class ValidateAggregateTests(unittest.TestCase):
    def _args(self, **kw):
        base = dict(package="A", repo="x", test_mode=True, version="",
                    output="g.png", ascii_tree=False, filter="",
                    load_order=False, plantuml_jar="")
        base.update(kw)
        return mock.Mock(**base)

    def test_collects_all_errors(self):
        a = self._args(package="a", repo="/no/such", output="g.txt")
        with self.assertRaises(dg.ConfigError) as cm:
            dg.validate(a)
        self.assertEqual(len(str(cm.exception).splitlines()), 3)

    def test_returns_config(self):
        with tempfile.NamedTemporaryFile() as f:
            args = self._args(repo=f.name, ascii_tree=True, load_order=True)
            cfg = dg.validate(args)
        self.assertIsInstance(cfg, dg.Config)
        self.assertTrue(cfg.ascii_tree and cfg.load_order)
        self.assertEqual(cfg.package, "A")


class ParserTests(unittest.TestCase):
    def test_defaults_and_flags(self):
        parser = dg.build_parser()
        a = parser.parse_args(["-p", "A", "-r", "x"])
        self.assertEqual((a.output, a.version, a.filter),
                         ("graph.png", "", ""))
        self.assertFalse(a.test_mode or a.ascii_tree or a.load_order)
        a = parser.parse_args(["-p", "A", "-r", "x", "-t", "-a", "-l",
                               "--plantuml-jar", "j.jar"])
        self.assertTrue(a.test_mode and a.ascii_tree and a.load_order)
        self.assertEqual(a.plantuml_jar, "j.jar")

    def test_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            dg.build_parser().parse_args([])


class HttpGetTests(unittest.TestCase):
    def _resp(self, data=b"ok"):
        m = mock.MagicMock()
        m.__enter__.return_value.read.return_value = data
        return m

    def test_success_sends_user_agent(self):
        resp = self._resp(b"data")
        with mock.patch.object(dg, "urlopen", return_value=resp) as u:
            self.assertEqual(dg.http_get("https://x"), b"data")
        req = u.call_args[0][0]
        self.assertEqual(req.get_header("User-agent"), dg.USER_AGENT)

    def test_errors(self):
        cases = [
            (HTTPError("u", 404, "nf", {}, None), "HTTP 404"),
            (HTTPError("u", 500, "err", {}, None), "HTTP 500"),
            (socket.timeout(), "ожидания"),
            (URLError("dns fail"), "dns fail"),
        ]
        for exc, text in cases:
            with mock.patch.object(dg, "urlopen", side_effect=exc):
                with self.assertRaises(dg.RepoError) as cm:
                    dg.http_get("https://x")
                self.assertIn(text, str(cm.exception))


class ServiceIndexTests(unittest.TestCase):
    def _find(self, payload, raw=None):
        raw = raw if raw is not None else json.dumps(payload).encode()
        with mock.patch.object(dg, "http_get", return_value=raw):
            return dg.find_package_base_address("https://idx")

    def test_list_type_and_trailing_slash(self):
        r = {"resources": [
            {"@id": "https://a/search", "@type": "SearchQueryService"},
            {"@id": "https://a/flat", "@type": ["PackageBaseAddress/3.0.0"]},
        ]}
        self.assertEqual(self._find(r), "https://a/flat/")

    def test_string_type_keeps_slash(self):
        r = {"resources": [
            {"@id": "https://a/flat/", "@type": "PackageBaseAddress/3.0.0"},
        ]}
        self.assertEqual(self._find(r), "https://a/flat/")

    def test_bom_is_accepted(self):
        payload = {"resources": [
            {"@id": "https://a/f", "@type": "PackageBaseAddress/3.0.0"},
        ]}
        raw = b"\xef\xbb\xbf" + json.dumps(payload).encode()
        self.assertEqual(self._find(None, raw), "https://a/f/")

    def test_errors(self):
        bad = [
            (None, b"not json"),
            (None, b"\xff\xfe\x00"),
            ([1, 2], None),
            ({"resources": "x"}, None),
            ({"resources": [{"@id": "u", "@type": "Other"}]}, None),
            ({"resources": [{"@type": "PackageBaseAddress/3.0.0"}]}, None),
            ({"resources": ["str", 5]}, None),
        ]
        for payload, raw in bad:
            with self.assertRaises(dg.RepoError, msg=str(payload or raw)):
                self._find(payload, raw)


NUSPEC_NS = b"""<?xml version="1.0"?>
<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">
 <metadata><id>Foo</id>
  <dependencies>
   <group targetFramework=".NETStandard2.0">
     <dependency id="Bar" version="[1.0, )"/>
     <dependency id="Baz" version="2.0.0"/>
   </group>
   <group targetFramework="net6.0">
     <dependency id="bar" version="[1.0, )"/>
   </group>
   <group><dependency id="NoFw" version="1.0"/></group>
  </dependencies>
 </metadata></package>"""

NUSPEC_FLAT = (
    b"<package><metadata><dependencies>"
    b"<dependency id='A' version='1.0'/>"
    b"</dependencies></metadata></package>"
)
NUSPEC_NO_ID = (
    b"<package><metadata><dependencies>"
    b"<dependency version='1.0'/>"
    b"</dependencies></metadata></package>"
)


class NuspecTests(unittest.TestCase):
    def test_groups_merge_and_frameworks(self):
        deps = {d.id.lower(): d for d in dg.parse_dependencies(NUSPEC_NS)}
        self.assertEqual(set(deps), {"bar", "baz", "nofw"})
        self.assertEqual(deps["bar"].frameworks,
                         [".NETStandard2.0", "net6.0"])
        self.assertEqual(deps["nofw"].frameworks, ["any"])
        self.assertEqual(deps["bar"].min_version, "1.0")

    def test_flat_dependencies_without_groups(self):
        (d,) = dg.parse_dependencies(NUSPEC_FLAT)
        self.assertEqual((d.id, d.frameworks), ("A", ["any"]))

    def test_dependency_without_id_is_ignored(self):
        self.assertEqual(dg.parse_dependencies(NUSPEC_NO_ID), [])

    def test_no_dependencies_element(self):
        nuspec = b"<package><metadata/></package>"
        self.assertEqual(dg.parse_dependencies(nuspec), [])

    def test_errors(self):
        with self.assertRaises(dg.RepoError):
            dg.parse_dependencies(b"<package><unclosed>")
        with self.assertRaises(dg.RepoError):
            dg.parse_dependencies(b"<package></package>")

    def test_resolve_version(self):
        cases = {"": "", "  ": "", "1.0": "1.0", "[1.0, )": "1.0",
                 "[1.0]": "1.0", "(, 2.0]": "2.0", "(1.0,2.0)": "1.0",
                 "(,)": ""}
        for rng, want in cases.items():
            self.assertEqual(dg.resolve_version(rng), want, msg=rng)


class DirectDependenciesTests(unittest.TestCase):
    def test_url_is_lowercased_and_build_metadata_stripped(self):
        with mock.patch.object(dg, "http_get",
                               return_value=NUSPEC_NS) as g:
            dg.get_direct_dependencies("idx", "Foo.Bar", "1.0.0-Beta+abc",
                                       base="https://b/")
        g.assert_called_once_with(
            "https://b/foo.bar/1.0.0-beta/foo.bar.nuspec")

    def test_base_discovered_when_missing(self):
        with mock.patch.object(dg, "find_package_base_address",
                               return_value="https://b/") as f, \
                mock.patch.object(dg, "http_get", return_value=NUSPEC_NS):
            dg.get_direct_dependencies("idx", "Foo", "1.0")
        f.assert_called_once_with("idx")

    def test_404_becomes_friendly_error(self):
        err = dg.RepoError("... HTTP 404 ...")
        with mock.patch.object(dg, "http_get", side_effect=err):
            with self.assertRaisesRegex(dg.RepoError,
                                        "не найден в репозитории"):
                dg.get_direct_dependencies("idx", "Foo", "1.0",
                                           base="https://b/")

    def test_other_errors_propagate(self):
        err = dg.RepoError("HTTP 500")
        with mock.patch.object(dg, "http_get", side_effect=err):
            with self.assertRaisesRegex(dg.RepoError, "500"):
                dg.get_direct_dependencies("idx", "Foo", "1.0",
                                           base="https://b/")


class NugetChildrenTests(unittest.TestCase):
    def _deps(self, *pairs):
        return [dg.Dependency(i, r) for i, r in pairs]

    def test_cache_and_min_version(self):
        deps = self._deps(("Bar", "[1.5, )"))
        with mock.patch.object(dg, "find_package_base_address",
                               return_value="b/"), \
                mock.patch.object(dg, "get_direct_dependencies",
                                  return_value=deps) as g:
            ch = dg.make_nuget_children("idx")
            self.assertEqual(ch("Foo", "1.0"), [("Bar", "1.5")])
            self.assertEqual(ch("FOO", "1.0"), [("Bar", "1.5")])
        self.assertEqual(g.call_count, 1)

    def test_missing_or_bad_version(self):
        for rng in ("", "(,)", "abc"):
            deps = self._deps(("Bar", rng))
            with mock.patch.object(dg, "find_package_base_address",
                                   return_value="b/"), \
                    mock.patch.object(dg, "get_direct_dependencies",
                                      return_value=deps):
                with self.assertRaises(dg.RepoError, msg=rng):
                    dg.make_nuget_children("idx")("Foo", "1.0")


class TestRepoParsingTests(unittest.TestCase):
    def _parse(self, text):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, f.name)
        return dg.parse_test_repo(f.name)

    def test_valid_with_comments_and_blank_lines(self):
        r = self._parse("# comment\n\nA: B C  # tail\nB:\nC: B\n")
        self.assertEqual(r, {"A": ["B", "C"], "B": [], "C": ["B"]})

    def test_errors(self):
        bad_texts = ("A B\n", "a: B\n", "A: b\n", "A: B\nA: C\n",
                     "# only comment\n", "")
        for text in bad_texts:
            with self.assertRaises(dg.RepoError, msg=repr(text)):
                self._parse(text)

    def test_error_contains_line_number(self):
        with self.assertRaisesRegex(dg.RepoError, r":3:"):
            self._parse("A: B\nB:\nbad line\n")

    def test_unreadable_file(self):
        with self.assertRaises(dg.RepoError):
            dg.parse_test_repo("/no/such/file.txt")


class BuildGraphTests(unittest.TestCase):
    def test_linear(self):
        r = make_graph({"A": ["B"], "B": ["C"], "C": []}, "A")
        self.assertEqual(r.graph, {"A": ["B"], "B": ["C"], "C": []})
        self.assertEqual((r.cycles, r.skipped), ([], []))

    def test_diamond_expands_shared_node_once(self):
        calls = []
        repo = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}

        def children(n, v):
            calls.append(n)
            return [(d, "") for d in repo[n]]
        r = dg.build_graph("A", "", children)
        self.assertEqual(calls.count("D"), 1)
        self.assertEqual(r.graph["C"], ["D"])
        self.assertEqual(r.cycles, [])

    def test_cycle(self):
        r = make_graph({"A": ["B"], "B": ["C"], "C": ["A"]}, "A")
        self.assertEqual(r.cycles, [["A", "B", "C", "A"]])
        self.assertIn("A", r.graph["C"])

    def test_self_loop(self):
        r = make_graph({"A": ["A"]}, "A")
        self.assertEqual(r.cycles, [["A", "A"]])

    def test_filter_skips_and_does_not_descend(self):
        repo = {"AX": ["BXY", "C"], "BXY": ["D"], "C": [], "D": []}
        r = make_graph(repo, "AX", sub="xy")
        self.assertNotIn("BXY", r.graph)
        self.assertNotIn("D", r.graph)
        self.assertEqual(r.skipped, ["BXY"])
        self.assertEqual(r.graph["AX"], ["C"])

    def test_filter_reported_once(self):
        repo = {"A": ["XB", "C"], "C": ["XB"], "XB": []}
        r = make_graph(repo, "A", sub="x")
        self.assertEqual(r.skipped, ["XB"])

    def test_names_differing_in_case_are_merged(self):
        repo = {"A": ["Foo", "B"], "B": ["FOO"], "Foo": [], "FOO": []}
        r = make_graph(repo, "A")
        merged = sorted(k for k in r.graph if k.lower() == "foo")
        self.assertEqual(merged, ["Foo"])

    def test_versions_recorded(self):
        def ch(n, v):
            return [("B", "2.0")] if n == "A" else []
        r = dg.build_graph("A", "1.0", ch)
        self.assertEqual(r.versions, {"A": "1.0", "B": "2.0"})

    def test_duplicate_edges_collapsed(self):
        r = make_graph({"A": ["B", "B"], "B": []}, "A")
        self.assertEqual(r.graph["A"], ["B"])


class LoadOrderTests(unittest.TestCase):
    def test_dependencies_before_dependents_root_last(self):
        repo = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}
        r = make_graph(repo, "A")
        order = dg.load_order(r)
        self.assertEqual(order[-1], "A")
        self.assertEqual(len(order), len(set(order)))
        for name, deps in r.graph.items():
            for d in deps:
                self.assertLess(order.index(d), order.index(name))

    def test_cycle_does_not_hang(self):
        r = make_graph({"A": ["B"], "B": ["A"]}, "A")
        self.assertEqual(dg.load_order(r), ["B", "A"])

    def test_single_package(self):
        r = make_graph({"A": []}, "A")
        self.assertEqual(dg.load_order(r), ["A"])


class AsciiTreeTests(unittest.TestCase):
    def test_simple(self):
        repo = {"A": ["B", "C"], "B": ["D"], "C": [], "D": []}
        r = make_graph(repo, "A")
        self.assertEqual(dg.ascii_tree(r), "A\n├── B\n│   └── D\n└── C")

    def test_shared_subtree_and_cycle_marks(self):
        repo = {"A": ["B", "C"], "B": ["D"], "C": ["D", "A"],
                "D": ["E"], "E": []}
        t = dg.ascii_tree(make_graph(repo, "A"))
        self.assertIn("(*)", t)
        self.assertIn("A (цикл)", t)
        self.assertIn("возврат к пакету-предку", t)

    def test_leaf_repeated_has_no_star(self):
        repo = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}
        r = make_graph(repo, "A")
        self.assertNotIn("(*)", dg.ascii_tree(r))

    def test_label_with_version(self):
        def ch(n, v):
            return [("B", "2.0")] if n == "A" else []
        r = dg.build_graph("A", "1.0", ch)
        self.assertEqual(dg.ascii_tree(r), "A 1.0\n└── B 2.0")


class PrintTests(unittest.TestCase):
    def _out(self, fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()

    def test_print_graph_with_cycle_and_filter(self):
        r = make_graph({"A": ["B", "ZZ"], "B": ["A"]}, "A", sub="zz")
        out = self._out(dg.print_graph, r, "zz")
        self.assertIn("пакетов 2, рёбер 2", out)
        self.assertIn("A -> B -> A", out)
        self.assertIn("Пропущено по фильтру 'zz': ZZ", out)

    def test_print_graph_no_cycles(self):
        r = make_graph({"A": []}, "A")
        out = self._out(dg.print_graph, r, "")
        self.assertIn("(нет зависимостей)", out)
        self.assertIn("Циклических зависимостей нет", out)

    def test_print_load_order(self):
        r = make_graph({"A": ["B"], "B": ["A"]}, "A")
        out = self._out(dg.print_load_order, r, "")
        self.assertIn("1. B", out)
        self.assertIn("2. A", out)
        self.assertIn("Циклы разорваны по рёбрам: B -> A", out)


def puml_decode(s):
    """Обратная операция к plantuml_encode (для проверки round-trip)."""
    alpha = dg._PUML_ALPHABET
    vals = [alpha.index(c) for c in s]
    data = bytearray()
    for i in range(0, len(vals), 4):
        c1, c2, c3, c4 = vals[i:i + 4]
        data += bytes([(c1 << 2) | (c2 >> 4),
                       ((c2 & 0xF) << 4) | (c3 >> 2),
                       ((c3 & 0x3) << 6) | c4])
    return zlib.decompress(bytes(data), -15)


class PlantUmlTests(unittest.TestCase):
    def test_encode_roundtrip(self):
        texts = ("@startuml\nA --> B\n@enduml\n", "Привет, мир " * 50, "x")
        for text in texts:
            enc = dg.plantuml_encode(text)
            self.assertTrue(set(enc) <= set(dg._PUML_ALPHABET))
            decoded = puml_decode(enc).decode("utf-8").rstrip("\x00")
            self.assertEqual(decoded, text)

    def test_to_plantuml(self):
        r = make_graph({"A": ["B"], "B": ["A"]}, "A")
        u = dg.to_plantuml(r)
        self.assertTrue(u.startswith("@startuml"))
        self.assertTrue(u.rstrip().endswith("@enduml"))
        self.assertIn('rectangle "A" as n0 #LightSkyBlue', u)
        self.assertIn('rectangle "B" as n1\n', u)
        self.assertIn("n0 --> n1", u)
        self.assertIn("n1 -[#red,dashed]-> n0 : цикл", u)

    def test_isolated_node_declared(self):
        u = dg.to_plantuml(make_graph({"A": []}, "A"))
        self.assertIn('rectangle "A" as n0', u)


class RenderTests(TempDirMixin, unittest.TestCase):
    @property
    def out(self):
        return os.path.join(self.tmp_dir, "g.png")

    def test_server_ok(self):
        with mock.patch.object(dg, "http_get",
                               return_value=b"\x89PNG\r\n..."):
            dg.render_png_server("@startuml\n@enduml", self.out)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(4), b"\x89PNG")

    def test_server_not_png(self):
        with mock.patch.object(dg, "http_get", return_value=b"<html>"):
            with self.assertRaisesRegex(dg.RenderError, "не PNG"):
                dg.render_png_server("x", self.out)

    def test_server_unavailable(self):
        err = dg.RepoError("down")
        with mock.patch.object(dg, "http_get", side_effect=err):
            with self.assertRaisesRegex(dg.RenderError, "недоступен"):
                dg.render_png_server("x", self.out)

    def test_server_url_too_long(self):
        big = os.urandom(20000).hex()
        with mock.patch.object(dg, "http_get") as g:
            with self.assertRaisesRegex(dg.RenderError, "слишком велика"):
                dg.render_png_server(big, self.out)
        g.assert_not_called()

    def test_jar_no_java(self):
        with mock.patch.object(dg.shutil, "which", return_value=None):
            with self.assertRaisesRegex(dg.RenderError, "Java"):
                dg.render_png_jar("a.puml", "p.jar", self.out)

    def test_jar_errors(self):
        timeout = dg.subprocess.TimeoutExpired("java", dg.JAR_TIMEOUT)
        bad = mock.Mock(returncode=1, stderr="boom", stdout="")
        ok = mock.Mock(returncode=0, stderr="", stdout="")
        cases = ((timeout, None, str(dg.JAR_TIMEOUT)),
                 (None, bad, "boom"),
                 (None, ok, "не создал"))
        for side_effect, value, text in cases:
            with mock.patch.object(dg.shutil, "which",
                                   return_value="/usr/bin/java"), \
                    mock.patch.object(dg.subprocess, "run",
                                      side_effect=side_effect,
                                      return_value=value):
                with self.assertRaisesRegex(dg.RenderError, text):
                    dg.render_png_jar("a.puml", "p.jar", self.out)

    def test_save_visualization_writes_puml_and_picks_backend(self):
        r = make_graph({"A": []}, "A")
        with mock.patch.object(dg, "render_png_server") as s, \
                mock.patch.object(dg, "render_png_jar") as j, \
                redirect_stdout(io.StringIO()):
            path = dg.save_visualization(r, self.out)
            self.assertTrue(path.endswith("g.puml"))
            self.assertTrue(os.path.isfile(path))
            s.assert_called_once()
            j.assert_not_called()
            dg.save_visualization(r, self.out, jar="p.jar")
            j.assert_called_once()

    def test_save_visualization_write_error(self):
        r = make_graph({"A": []}, "A")
        bad = os.path.join(self.tmp_dir, "no_dir", "g.png")
        with self.assertRaises(dg.RenderError):
            dg.save_visualization(r, bad)


class MainTests(TempDirMixin, unittest.TestCase):
    @functools.cached_property
    def repo(self):
        path = os.path.join(self.tmp_dir, "repo.txt")
        with open(path, "w") as f:
            f.write("A: B C\nB: D\nC: D A\nD:\n")
        return path

    @property
    def out(self):
        return os.path.join(self.tmp_dir, "g.png")

    def run_main(self, *argv):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = dg.main(list(argv))
        return code, o.getvalue(), e.getvalue()

    def test_config_error_exit_2(self):
        code, _, err = self.run_main(
            "-p", "a", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 2)
        self.assertIn("Ошибка конфигурации", err)

    def test_unknown_package_exit_3(self):
        code, _, err = self.run_main(
            "-p", "Z", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 3)
        self.assertIn("не описан", err)

    def test_broken_repo_file_exit_3(self):
        with open(self.repo, "w") as f:
            f.write("garbage\n")
        code, _, _ = self.run_main(
            "-p", "A", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 3)

    def test_load_order_mode_exit_0_and_no_image(self):
        code, out, _ = self.run_main(
            "-p", "A", "-r", self.repo, "-t", "-l", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertIn("Порядок загрузки", out)
        self.assertFalse(os.path.exists(self.out))

    def test_full_run_with_ascii_tree(self):
        with mock.patch.object(dg, "http_get",
                               return_value=b"\x89PNG data"):
            code, out, _ = self.run_main(
                "-p", "A", "-r", self.repo, "-t", "-a", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertIn("ASCII-дерево", out)
        self.assertIn("Изображение графа сохранено", out)
        self.assertTrue(os.path.isfile(self.out))
        puml = os.path.splitext(self.out)[0] + ".puml"
        self.assertTrue(os.path.isfile(puml))

    def test_render_error_exit_4(self):
        err = dg.RepoError("down")
        with mock.patch.object(dg, "http_get", side_effect=err):
            code, _, stderr = self.run_main(
                "-p", "A", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 4)
        self.assertIn("Ошибка визуализации", stderr)

    def test_filter_applied(self):
        code, out, _ = self.run_main(
            "-p", "A", "-r", self.repo, "-t", "-l", "-f", "D")
        self.assertEqual(code, 0)
        self.assertIn("Пропущено по фильтру 'D': D", out)

    def test_recursion_error_exit_3(self):
        with mock.patch.object(dg, "build_graph",
                               side_effect=RecursionError):
            code, _, err = self.run_main(
                "-p", "A", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 3)
        self.assertIn("слишком глубокая", err)

    def test_deep_chain_does_not_crash(self):
        letters = string.ascii_uppercase
        names = ["P" + "".join(letters[int(c)] for c in str(i))
                 for i in range(DEEP_CHAIN_LENGTH)]
        with open(self.repo, "w") as f:
            for a, b in zip(names, names[1:]):
                f.write(f"{a}: {b}\n")
            f.write(f"{names[-1]}:\n")
        code, out, _ = self.run_main(
            "-p", names[0], "-r", self.repo, "-t", "-l")
        self.assertEqual(code, 0)
        self.assertIn(f"({DEEP_CHAIN_LENGTH})", out)


BRANCH_NODES = (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While,
                ast.Assert, ast.ExceptHandler)
NESTED_SCOPES = (ast.FunctionDef, ast.Lambda)


def node_complexity(node, include_nested):
    """Число точек ветвления в поддереве node."""
    count = 0
    if isinstance(node, BRANCH_NODES):
        count += 1
    elif isinstance(node, ast.BoolOp):
        count += len(node.values) - 1
    elif isinstance(node, ast.comprehension):
        count += 1 + len(node.ifs)
    for child in ast.iter_child_nodes(node):
        if include_nested or not isinstance(child, NESTED_SCOPES):
            count += node_complexity(child, include_nested)
    return count


def cyclomatic(fn, include_nested):
    """Цикломатическая сложность функции (опционально — с вложенными)."""
    return 1 + sum(
        node_complexity(child, include_nested)
        for child in ast.iter_child_nodes(fn)
        if include_nested or not isinstance(child, NESTED_SCOPES))


class ComplexityTests(unittest.TestCase):
    def test_every_function_within_limit(self):
        with open(dg.__file__, encoding="utf-8") as f:
            src = f.read()
        too_complex = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef):
                for nested in (False, True):
                    c = cyclomatic(node, nested)
                    if c > COMPLEXITY_LIMIT:
                        too_complex.append((node.name, node.lineno, c,
                                            "с вложенными" if nested else ""))
        self.assertEqual(too_complex, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
