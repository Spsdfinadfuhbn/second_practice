"""Тесты для practice.py (depgraph). Запуск:  python -m unittest -v test_practice
Сеть не используется: http_get / urlopen подменяются через unittest.mock."""
import ast
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import practice as P


def make_graph(repo_text_dict, root, sub=""):
    """Строит граф по словарю {пакет: [зависимости]}."""
    children = P.make_test_children(repo_text_dict)
    return P.build_graph(root, "", children, sub)



class ValidatePackageTests(unittest.TestCase):
    def test_empty(self):
        for name in ("", "   "):
            with self.assertRaises(P.ConfigError):
                P.validate_package(name, False)

    def test_test_mode_only_uppercase(self):
        P.validate_package("ABC", True)
        for bad in ("abc", "A1", "A-B", "Ab"):
            with self.assertRaises(P.ConfigError):
                P.validate_package(bad, True)

    def test_nuget_names(self):
        for ok in ("Newtonsoft.Json", "System.Text-Json", "a_b", "X"):
            P.validate_package(ok, False)
        for bad in (".Json", "A..B", "A B", "A/B", "Json."):
            with self.assertRaises(P.ConfigError):
                P.validate_package(bad, False)


class ValidateRepoTests(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(P.ConfigError):
            P.validate_repo("  ", False)

    def test_url_ok_and_bad(self):
        P.validate_repo("https://api.nuget.org/v3/index.json", False)
        P.validate_repo("http://localhost:5000/index.json", False)
        for bad in ("ftp://x/y", "api.nuget.org", "https://", "/tmp/file"):
            with self.assertRaises(P.ConfigError):
                P.validate_repo(bad, False)

    def test_file_mode(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "r.txt")
            open(f, "w").close()
            P.validate_repo(f, True)
            with self.assertRaises(P.ConfigError):
                P.validate_repo(os.path.join(d, "missing"), True)
            with self.assertRaises(P.ConfigError):
                P.validate_repo(d, True)


class ValidateVersionTests(unittest.TestCase):
    def test_required_outside_test_mode(self):
        with self.assertRaises(P.ConfigError):
            P.validate_version("", False)
        P.validate_version("", True)

    def test_formats(self):
        for ok in ("1.0", "13.0.3", "1.2.3.4", "1.0.0-beta.1", "1.0.0+build.5", "1.0.0-rc.1+meta"):
            P.validate_version(ok, False)
        for bad in ("1", "a.b", "1.0.", "1.0.0-", "v1.0", "1..0"):
            with self.assertRaises(P.ConfigError):
                P.validate_version(bad, False)


class ValidateOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_ok(self):
        P.validate_output(os.path.join(self.d, "g.png"))
        P.validate_output("G.PNG")

    def test_bad_names(self):
        for bad in ("", "  ", "a<b.png", "a?.png", "graph.jpg", ".png", "graph"):
            with self.assertRaises(P.ConfigError, msg=repr(bad)):
                P.validate_output(bad)

    def test_location_errors(self):
        with self.assertRaises(P.ConfigError):
            P.validate_output(os.path.join(self.d, "nope", "g.png"))
        os.mkdir(os.path.join(self.d, "dir.png"))
        with self.assertRaises(P.ConfigError):
            P.validate_output(os.path.join(self.d, "dir.png"))

    def test_no_write_permission(self):
        with mock.patch("os.access", return_value=False):
            with self.assertRaises(P.ConfigError):
                P.validate_output(os.path.join(self.d, "g.png"))


class ValidateFilterTests(unittest.TestCase):
    def test_cases(self):
        P.validate_filter("", "A")
        P.validate_filter("Test", "Newtonsoft.Json")
        with self.assertRaises(P.ConfigError):
            P.validate_filter("  ", "A")
        with self.assertRaises(P.ConfigError):
            P.validate_filter("json", "Newtonsoft.Json")


class ValidateAggregateTests(unittest.TestCase):
    def _args(self, **kw):
        base = dict(package="A", repo="x", test_mode=True, version="", output="g.png",
                    ascii_tree=False, filter="", load_order=False, plantuml_jar="")
        base.update(kw)
        return mock.Mock(**base)

    def test_collects_all_errors(self):
        a = self._args(package="a", repo="/no/such", output="g.txt")
        with self.assertRaises(P.ConfigError) as cm:
            P.validate(a)
        self.assertEqual(len(str(cm.exception).splitlines()), 3)

    def test_returns_config(self):
        with tempfile.NamedTemporaryFile() as f:
            cfg = P.validate(self._args(repo=f.name, ascii_tree=True, load_order=True))
        self.assertIsInstance(cfg, P.Config)
        self.assertTrue(cfg.ascii_tree and cfg.load_order)
        self.assertEqual(cfg.package, "A")


class ParserTests(unittest.TestCase):
    def test_defaults_and_flags(self):
        a = P.build_parser().parse_args(["-p", "A", "-r", "x"])
        self.assertEqual((a.output, a.version, a.filter), ("graph.png", "", ""))
        self.assertFalse(a.test_mode or a.ascii_tree or a.load_order)
        a = P.build_parser().parse_args(["-p", "A", "-r", "x", "-t", "-a", "-l", "--plantuml-jar", "j.jar"])
        self.assertTrue(a.test_mode and a.ascii_tree and a.load_order)
        self.assertEqual(a.plantuml_jar, "j.jar")

    def test_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            P.build_parser().parse_args([])



class HttpGetTests(unittest.TestCase):
    def _resp(self, data=b"ok"):
        m = mock.MagicMock()
        m.__enter__.return_value.read.return_value = data
        return m

    def test_success_sends_user_agent(self):
        with mock.patch.object(P, "urlopen", return_value=self._resp(b"data")) as u:
            self.assertEqual(P.http_get("https://x"), b"data")
        req = u.call_args[0][0]
        self.assertEqual(req.get_header("User-agent"), P.USER_AGENT)

    def test_errors(self):
        cases = [
            (HTTPError("u", 404, "nf", {}, None), "HTTP 404"),
            (HTTPError("u", 500, "err", {}, None), "HTTP 500"),
            (socket.timeout(), "ожидания"),
            (URLError("dns fail"), "dns fail"),
        ]
        for exc, text in cases:
            with mock.patch.object(P, "urlopen", side_effect=exc):
                with self.assertRaises(P.RepoError) as cm:
                    P.http_get("https://x")
                self.assertIn(text, str(cm.exception))


class ServiceIndexTests(unittest.TestCase):
    def _find(self, payload, raw=None):
        raw = raw if raw is not None else json.dumps(payload).encode()
        with mock.patch.object(P, "http_get", return_value=raw):
            return P.find_package_base_address("https://idx")

    def test_list_type_and_trailing_slash(self):
        r = {"resources": [
            {"@id": "https://a/search", "@type": "SearchQueryService"},
            {"@id": "https://a/flat", "@type": ["PackageBaseAddress/3.0.0"]}]}
        self.assertEqual(self._find(r), "https://a/flat/")

    def test_string_type_keeps_slash(self):
        r = {"resources": [{"@id": "https://a/flat/", "@type": "PackageBaseAddress/3.0.0"}]}
        self.assertEqual(self._find(r), "https://a/flat/")

    def test_bom_is_accepted(self):
        raw = b"\xef\xbb\xbf" + json.dumps(
            {"resources": [{"@id": "https://a/f", "@type": "PackageBaseAddress/3.0.0"}]}).encode()
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
            with self.assertRaises(P.RepoError, msg=str(payload or raw)):
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


class NuspecTests(unittest.TestCase):
    def test_groups_merge_and_frameworks(self):
        deps = {d.id.lower(): d for d in P.parse_dependencies(NUSPEC_NS)}
        self.assertEqual(set(deps), {"bar", "baz", "nofw"})
        self.assertEqual(deps["bar"].frameworks, [".NETStandard2.0", "net6.0"])
        self.assertEqual(deps["nofw"].frameworks, ["any"])
        self.assertEqual(deps["bar"].min_version, "1.0")

    def test_flat_dependencies_without_groups(self):
        x = b"<package><metadata><dependencies><dependency id='A' version='1.0'/></dependencies></metadata></package>"
        (d,) = P.parse_dependencies(x)
        self.assertEqual((d.id, d.frameworks), ("A", ["any"]))

    def test_dependency_without_id_is_ignored(self):
        x = b"<package><metadata><dependencies><dependency version='1.0'/></dependencies></metadata></package>"
        self.assertEqual(P.parse_dependencies(x), [])

    def test_no_dependencies_element(self):
        self.assertEqual(P.parse_dependencies(b"<package><metadata/></package>"), [])

    def test_errors(self):
        with self.assertRaises(P.RepoError):
            P.parse_dependencies(b"<package><unclosed>")
        with self.assertRaises(P.RepoError):
            P.parse_dependencies(b"<package></package>")

    def test_resolve_version(self):
        cases = {"": "", "  ": "", "1.0": "1.0", "[1.0, )": "1.0", "[1.0]": "1.0",
                 "(, 2.0]": "2.0", "(1.0,2.0)": "1.0", "(,)": ""}
        for rng, want in cases.items():
            self.assertEqual(P.resolve_version(rng), want, msg=rng)


class DirectDependenciesTests(unittest.TestCase):
    def test_url_is_lowercased_and_build_metadata_stripped(self):
        with mock.patch.object(P, "http_get", return_value=NUSPEC_NS) as g:
            P.get_direct_dependencies("idx", "Foo.Bar", "1.0.0-Beta+abc", base="https://b/")
        g.assert_called_once_with("https://b/foo.bar/1.0.0-beta/foo.bar.nuspec")

    def test_base_discovered_when_missing(self):
        with mock.patch.object(P, "find_package_base_address", return_value="https://b/") as f, \
             mock.patch.object(P, "http_get", return_value=NUSPEC_NS):
            P.get_direct_dependencies("idx", "Foo", "1.0")
        f.assert_called_once_with("idx")

    def test_404_becomes_friendly_error(self):
        with mock.patch.object(P, "http_get", side_effect=P.RepoError("... HTTP 404 ...")):
            with self.assertRaisesRegex(P.RepoError, "не найден в репозитории"):
                P.get_direct_dependencies("idx", "Foo", "1.0", base="https://b/")

    def test_other_errors_propagate(self):
        with mock.patch.object(P, "http_get", side_effect=P.RepoError("HTTP 500")):
            with self.assertRaisesRegex(P.RepoError, "500"):
                P.get_direct_dependencies("idx", "Foo", "1.0", base="https://b/")


class NugetChildrenTests(unittest.TestCase):
    def _deps(self, *pairs):
        return [P.Dependency(i, r) for i, r in pairs]

    def test_cache_and_min_version(self):
        with mock.patch.object(P, "find_package_base_address", return_value="b/"), \
             mock.patch.object(P, "get_direct_dependencies",
                               return_value=self._deps(("Bar", "[1.5, )"))) as g:
            ch = P.make_nuget_children("idx")
            self.assertEqual(ch("Foo", "1.0"), [("Bar", "1.5")])
            self.assertEqual(ch("FOO", "1.0"), [("Bar", "1.5")])
        self.assertEqual(g.call_count, 1)

    def test_missing_or_bad_version(self):
        for rng in ("", "(,)", "abc"):
            with mock.patch.object(P, "find_package_base_address", return_value="b/"), \
                 mock.patch.object(P, "get_direct_dependencies", return_value=self._deps(("Bar", rng))):
                with self.assertRaises(P.RepoError, msg=rng):
                    P.make_nuget_children("idx")("Foo", "1.0")



class TestRepoParsingTests(unittest.TestCase):
    def _parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, f.name)
        return P.parse_test_repo(f.name)

    def test_valid_with_comments_and_blank_lines(self):
        r = self._parse("# comment\n\nA: B C  # tail\nB:\nC: B\n")
        self.assertEqual(r, {"A": ["B", "C"], "B": [], "C": ["B"]})

    def test_errors(self):
        for text in ("A B\n", "a: B\n", "A: b\n", "A: B\nA: C\n", "# only comment\n", ""):
            with self.assertRaises(P.RepoError, msg=repr(text)):
                self._parse(text)

    def test_error_contains_line_number(self):
        with self.assertRaisesRegex(P.RepoError, r":3:"):
            self._parse("A: B\nB:\nbad line\n")

    def test_unreadable_file(self):
        with self.assertRaises(P.RepoError):
            P.parse_test_repo("/no/such/file.txt")


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
        r = P.build_graph("A", "", children)
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
        r = make_graph({"AX": ["BXY", "C"], "BXY": ["D"], "C": [], "D": []}, "AX", sub="xy")
        self.assertNotIn("BXY", r.graph)
        self.assertNotIn("D", r.graph)
        self.assertEqual(r.skipped, ["BXY"])
        self.assertEqual(r.graph["AX"], ["C"])

    def test_filter_reported_once(self):
        r = make_graph({"A": ["XB", "C"], "C": ["XB"], "XB": []}, "A", sub="x")
        self.assertEqual(r.skipped, ["XB"])

    def test_names_differing_in_case_are_merged(self):
        repo = {"A": ["Foo", "B"], "B": ["FOO"], "Foo": [], "FOO": []}
        r = make_graph(repo, "A")
        self.assertEqual(sorted(k for k in r.graph if k.lower() == "foo"), ["Foo"])

    def test_versions_recorded(self):
        ch = lambda n, v: [("B", "2.0")] if n == "A" else []
        r = P.build_graph("A", "1.0", ch)
        self.assertEqual(r.versions, {"A": "1.0", "B": "2.0"})

    def test_duplicate_edges_collapsed(self):
        r = make_graph({"A": ["B", "B"], "B": []}, "A")
        self.assertEqual(r.graph["A"], ["B"])


class LoadOrderTests(unittest.TestCase):
    def test_dependencies_before_dependents_root_last(self):
        r = make_graph({"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}, "A")
        order = P.load_order(r)
        self.assertEqual(order[-1], "A")
        self.assertEqual(len(order), len(set(order)))
        for name, deps in r.graph.items():
            for d in deps:
                self.assertLess(order.index(d), order.index(name))

    def test_cycle_does_not_hang(self):
        r = make_graph({"A": ["B"], "B": ["A"]}, "A")
        self.assertEqual(P.load_order(r), ["B", "A"])

    def test_single_package(self):
        self.assertEqual(P.load_order(make_graph({"A": []}, "A")), ["A"])


class AsciiTreeTests(unittest.TestCase):
    def test_simple(self):
        r = make_graph({"A": ["B", "C"], "B": ["D"], "C": [], "D": []}, "A")
        self.assertEqual(P.ascii_tree(r), "A\n├── B\n│   └── D\n└── C")

    def test_shared_subtree_and_cycle_marks(self):
        r = make_graph({"A": ["B", "C"], "B": ["D"], "C": ["D", "A"], "D": ["E"], "E": []}, "A")
        t = P.ascii_tree(r)
        self.assertIn("(*)", t)
        self.assertIn("A (цикл)", t)
        self.assertIn("возврат к пакету-предку", t)

    def test_leaf_repeated_has_no_star(self):
        r = make_graph({"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}, "A")
        self.assertNotIn("(*)", P.ascii_tree(r))

    def test_label_with_version(self):
        r = P.build_graph("A", "1.0", lambda n, v: [("B", "2.0")] if n == "A" else [])
        self.assertEqual(P.ascii_tree(r), "A 1.0\n└── B 2.0")


class PrintTests(unittest.TestCase):
    def _out(self, fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()

    def test_print_graph_with_cycle_and_filter(self):
        r = make_graph({"A": ["B", "ZZ"], "B": ["A"]}, "A", sub="zz")
        out = self._out(P.print_graph, r, "zz")
        self.assertIn("пакетов 2, рёбер 2", out)
        self.assertIn("A -> B -> A", out)
        self.assertIn("Пропущено по фильтру 'zz': ZZ", out)

    def test_print_graph_no_cycles(self):
        out = self._out(P.print_graph, make_graph({"A": []}, "A"), "")
        self.assertIn("(нет зависимостей)", out)
        self.assertIn("Циклических зависимостей нет", out)

    def test_print_load_order(self):
        r = make_graph({"A": ["B"], "B": ["A"]}, "A")
        out = self._out(P.print_load_order, r, "")
        self.assertIn("1. B", out)
        self.assertIn("2. A", out)
        self.assertIn("Циклы разорваны по рёбрам: B -> A", out)


def puml_decode(s):
    """Обратная операция к plantuml_encode (для проверки round-trip)."""
    alpha = P._PUML_ALPHABET
    vals = [alpha.index(c) for c in s]
    data = bytearray()
    for i in range(0, len(vals), 4):
        c1, c2, c3, c4 = vals[i:i + 4]
        data += bytes([(c1 << 2) | (c2 >> 4), ((c2 & 0xF) << 4) | (c3 >> 2), ((c3 & 0x3) << 6) | c4])
    return zlib.decompress(bytes(data), -15)


class PlantUmlTests(unittest.TestCase):
    def test_encode_roundtrip(self):
        for text in ("@startuml\nA --> B\n@enduml\n", "Привет, мир " * 50, "x"):
            enc = P.plantuml_encode(text)
            self.assertTrue(set(enc) <= set(P._PUML_ALPHABET))
            self.assertEqual(puml_decode(enc).decode("utf-8").rstrip("\x00"), text)

    def test_to_plantuml(self):
        r = make_graph({"A": ["B"], "B": ["A"]}, "A")
        u = P.to_plantuml(r)
        self.assertTrue(u.startswith("@startuml") and u.rstrip().endswith("@enduml"))
        self.assertIn('rectangle "A" as n0 #LightSkyBlue', u)
        self.assertIn('rectangle "B" as n1\n', u)
        self.assertIn("n0 --> n1", u)
        self.assertIn("n1 -[#red,dashed]-> n0 : цикл", u)

    def test_isolated_node_declared(self):
        self.assertIn('rectangle "A" as n0', P.to_plantuml(make_graph({"A": []}, "A")))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "g.png")

    def test_server_ok(self):
        with mock.patch.object(P, "http_get", return_value=b"\x89PNG\r\n..."):
            P.render_png_server("@startuml\n@enduml", self.out)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(4), b"\x89PNG")

    def test_server_not_png(self):
        with mock.patch.object(P, "http_get", return_value=b"<html>"):
            with self.assertRaisesRegex(P.RenderError, "не PNG"):
                P.render_png_server("x", self.out)

    def test_server_unavailable(self):
        with mock.patch.object(P, "http_get", side_effect=P.RepoError("down")):
            with self.assertRaisesRegex(P.RenderError, "недоступен"):
                P.render_png_server("x", self.out)

    def test_server_url_too_long(self):
        big = os.urandom(20000).hex()
        with mock.patch.object(P, "http_get") as g:
            with self.assertRaisesRegex(P.RenderError, "слишком велика"):
                P.render_png_server(big, self.out)
        g.assert_not_called()

    def test_jar_errors(self):
        with mock.patch.object(P.shutil, "which", return_value=None):
            with self.assertRaisesRegex(P.RenderError, "Java"):
                P.render_png_jar("a.puml", "p.jar", self.out)
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/java"):
            with mock.patch.object(P.subprocess, "run",
                                   side_effect=P.subprocess.TimeoutExpired("java", 120)):
                with self.assertRaisesRegex(P.RenderError, "120"):
                    P.render_png_jar("a.puml", "p.jar", self.out)
            bad = mock.Mock(returncode=1, stderr="boom", stdout="")
            with mock.patch.object(P.subprocess, "run", return_value=bad):
                with self.assertRaisesRegex(P.RenderError, "boom"):
                    P.render_png_jar("a.puml", "p.jar", self.out)
            ok = mock.Mock(returncode=0, stderr="", stdout="")
            with mock.patch.object(P.subprocess, "run", return_value=ok):
                with self.assertRaisesRegex(P.RenderError, "не создал"):
                    P.render_png_jar("a.puml", "p.jar", self.out)

    def test_save_visualization_writes_puml_and_picks_backend(self):
        r = make_graph({"A": []}, "A")
        with mock.patch.object(P, "render_png_server") as s, mock.patch.object(P, "render_png_jar") as j, \
             redirect_stdout(io.StringIO()):
            path = P.save_visualization(r, self.out)
            self.assertTrue(path.endswith("g.puml") and os.path.isfile(path))
            s.assert_called_once()
            j.assert_not_called()
            P.save_visualization(r, self.out, jar="p.jar")
            j.assert_called_once()

    def test_save_visualization_write_error(self):
        r = make_graph({"A": []}, "A")
        bad = os.path.join(self.tmp.name, "no_dir", "g.png")
        with self.assertRaises(P.RenderError):
            P.save_visualization(r, bad)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo.txt")
        with open(self.repo, "w") as f:
            f.write("A: B C\nB: D\nC: D A\nD:\n")
        self.out = os.path.join(self.tmp.name, "g.png")

    def run_main(self, *argv):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = P.main(list(argv))
        return code, o.getvalue(), e.getvalue()

    def test_config_error_exit_2(self):
        code, _, err = self.run_main("-p", "a", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 2)
        self.assertIn("Ошибка конфигурации", err)

    def test_unknown_package_exit_3(self):
        code, _, err = self.run_main("-p", "Z", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 3)
        self.assertIn("не описан", err)

    def test_broken_repo_file_exit_3(self):
        with open(self.repo, "w") as f:
            f.write("garbage\n")
        code, _, err = self.run_main("-p", "A", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 3)

    def test_load_order_mode_exit_0_and_no_image(self):
        code, out, _ = self.run_main("-p", "A", "-r", self.repo, "-t", "-l", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertIn("Порядок загрузки", out)
        self.assertFalse(os.path.exists(self.out))

    def test_full_run_with_ascii_tree(self):
        with mock.patch.object(P, "http_get", return_value=b"\x89PNG data"):
            code, out, _ = self.run_main("-p", "A", "-r", self.repo, "-t", "-a", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertIn("ASCII-дерево", out)
        self.assertIn("Изображение графа сохранено", out)
        self.assertTrue(os.path.isfile(self.out))
        self.assertTrue(os.path.isfile(os.path.splitext(self.out)[0] + ".puml"))

    def test_render_error_exit_4(self):
        with mock.patch.object(P, "http_get", side_effect=P.RepoError("down")):
            code, _, err = self.run_main("-p", "A", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 4)
        self.assertIn("Ошибка визуализации", err)

    def test_filter_applied(self):
        code, out, _ = self.run_main("-p", "A", "-r", self.repo, "-t", "-l", "-f", "D")
        self.assertEqual(code, 0)
        self.assertIn("Пропущено по фильтру 'D': D", out)

    def test_recursion_error_exit_3(self):
        with mock.patch.object(P, "build_graph", side_effect=RecursionError):
            code, _, err = self.run_main("-p", "A", "-r", self.repo, "-t", "-o", self.out)
        self.assertEqual(code, 3)
        self.assertIn("слишком глубокая", err)

    def test_deep_chain_does_not_crash(self):
        import string
        names = ["P" + "".join(string.ascii_uppercase[int(c)] for c in str(i)) for i in range(1500)]
        with open(self.repo, "w") as f:
            for a, b in zip(names, names[1:]):
                f.write(f"{a}: {b}\n")
            f.write(f"{names[-1]}:\n")
        code, out, _ = self.run_main("-p", names[0], "-r", self.repo, "-t", "-l")
        self.assertEqual(code, 0)
        self.assertIn("(1500)", out)


def cyclomatic(fn, include_nested):
    class V(ast.NodeVisitor):
        c = 1

        def generic_visit(self, n):
            if isinstance(n, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.Assert, ast.ExceptHandler)):
                self.c += 1
            elif isinstance(n, ast.BoolOp):
                self.c += len(n.values) - 1
            elif isinstance(n, ast.comprehension):
                self.c += 1 + len(n.ifs)
            super().generic_visit(n)

        def visit_FunctionDef(self, n):
            if include_nested:
                self.generic_visit(n)

        def visit_Lambda(self, n):
            if include_nested:
                self.generic_visit(n)
    v = V()
    for ch in ast.iter_child_nodes(fn):
        v.visit(ch)
    return v.c


class ComplexityTests(unittest.TestCase):
    LIMIT = 10

    def test_every_function_within_limit(self):
        with open(P.__file__, encoding="utf-8") as f:
            src = f.read()
        too_complex = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef):
                for nested in (False, True):
                    c = cyclomatic(node, nested)
                    if c > self.LIMIT:
                        too_complex.append((node.name, node.lineno, c, "с вложенными" if nested else ""))
        self.assertEqual(too_complex, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
