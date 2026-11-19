import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as etree
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


NUGET_ID_RE = re.compile(r"^[A-Za-z0-9_]+([.-][A-Za-z0-9_]+)*$")
TEST_ID_RE = re.compile(r"^[A-Z]+$")
_PRERELEASE = r"(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?"
_BUILD_META = r"(\+[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?"
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}" + _PRERELEASE + _BUILD_META + "$")
FORBIDDEN_FILENAME_CHARS = set('<>:"|?*\0')
HTTP_TIMEOUT = 15
HTTP_NOT_FOUND = 404
USER_AGENT = "depgraph/0.3 (educational project)"
PLANTUML_SERVER = "https://www.plantuml.com/plantuml"
MAX_URL_LENGTH = 8000
JAR_TIMEOUT = 120
RECURSION_LIMIT = 5000
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_REPO_ERROR = 3
EXIT_RENDER_ERROR = 4


class ConfigError(Exception):
    """Ошибка пользовательской конфигурации."""


class RepoError(Exception):
    """Ошибка получения или разбора данных репозитория."""


@dataclass
class Config:
    package: str
    repo: str
    test_mode: bool
    version: str
    output: str
    ascii_tree: bool
    filter: str
    load_order: bool = False
    plantuml_jar: str = ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="depgraph",
        description="Визуализация графа зависимостей пакета (формат NuGet).",
    )
    p.add_argument("-p", "--package", required=True,
                   help="имя анализируемого пакета")
    p.add_argument("-r", "--repo", required=True,
                   help="URL репозитория "
                        "(https://api.nuget.org/v3/index.json) "
                        "или путь к файлу тестового репозитория "
                        "(с --test-mode)")
    p.add_argument("-t", "--test-mode", action="store_true",
                   help="режим работы с тестовым репозиторием "
                        "(--repo — путь к файлу)")
    p.add_argument("-v", "--version", default="",
                   help="версия пакета, например 13.0.3 "
                        "(обязательна вне тестового режима)")
    p.add_argument("-o", "--output", default="graph.png",
                   help="имя файла с изображением графа (.png), "
                        "по умолчанию graph.png")
    p.add_argument("-a", "--ascii-tree", action="store_true",
                   help="вывести зависимости в виде ASCII-дерева")
    p.add_argument("-f", "--filter", default="",
                   help="подстрока: пакеты, в имени которых она есть, "
                        "не анализируются")
    p.add_argument("-l", "--load-order", action="store_true",
                   help="вывести порядок загрузки зависимостей "
                        "вместо графа")
    p.add_argument("--plantuml-jar", default="",
                   help="путь к plantuml.jar (если не указан, "
                        "используется сервер PlantUML)")
    return p


def validate_package(name: str, test_mode: bool) -> None:
    if not name or not name.strip():
        raise ConfigError("--package: имя пакета не может быть пустым")
    if test_mode:
        if not TEST_ID_RE.fullmatch(name):
            raise ConfigError(
                f"--package: в тестовом режиме имя должно состоять из "
                f"заглавных латинских букв, получено '{name}'")
    elif not NUGET_ID_RE.fullmatch(name):
        raise ConfigError(
            f"--package: недопустимое имя NuGet-пакета '{name}' "
            f"(разрешены буквы, цифры, '_', '.', '-')")


def validate_repo(repo: str, test_mode: bool) -> None:
    if not repo or not repo.strip():
        raise ConfigError("--repo: значение не может быть пустым")
    if test_mode:
        if not os.path.exists(repo):
            raise ConfigError(f"--repo: файл '{repo}' не найден")
        if not os.path.isfile(repo):
            raise ConfigError(f"--repo: '{repo}' не является файлом")
        if not os.access(repo, os.R_OK):
            raise ConfigError(f"--repo: нет прав на чтение файла '{repo}'")
    else:
        u = urlparse(repo)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ConfigError(
                f"--repo: '{repo}' не является корректным URL "
                f"(нужен http:// или https://). "
                f"Для пути к файлу используйте --test-mode")


def validate_version(version: str, test_mode: bool) -> None:
    if not version:
        if not test_mode:
            raise ConfigError(
                "--version: версия обязательна вне тестового режима")
        return
    if not VERSION_RE.fullmatch(version):
        raise ConfigError(
            f"--version: '{version}' не соответствует формату версии "
            f"(пример: 1.2.3, 1.0.0-beta.1)")


def _validate_output_name(output: str) -> None:
    if not output or not output.strip():
        raise ConfigError("--output: имя файла не может быть пустым")
    if any(c in FORBIDDEN_FILENAME_CHARS for c in output):
        raise ConfigError(
            f"--output: имя '{output}' содержит недопустимые символы")
    if not output.lower().endswith(".png"):
        raise ConfigError(
            f"--output: файл должен иметь расширение .png, "
            f"получено '{output}'")
    if os.path.basename(output) in ("", ".png"):
        raise ConfigError("--output: у файла отсутствует имя")


def _validate_output_location(output: str) -> None:
    directory = os.path.dirname(output) or "."
    if not os.path.isdir(directory):
        raise ConfigError(f"--output: каталог '{directory}' не существует")
    if os.path.isdir(output):
        raise ConfigError(f"--output: '{output}' — это каталог")
    if not os.access(directory, os.W_OK):
        raise ConfigError(
            f"--output: нет прав на запись в каталог '{directory}'")


def validate_output(output: str) -> None:
    _validate_output_name(output)
    _validate_output_location(output)


def validate_filter(substr: str, package: str) -> None:
    if substr != "" and not substr.strip():
        raise ConfigError(
            "--filter: подстрока не может состоять только из пробелов")
    if substr and package and substr.lower() in package.lower():
        raise ConfigError(
            f"--filter: подстрока '{substr}' входит в имя анализируемого "
            f"пакета '{package}', анализировать было бы нечего")


def validate(args: argparse.Namespace) -> Config:
    """Проверяет все параметры и собирает все ошибки сразу."""
    errors = []
    checks = (
        lambda: validate_package(args.package, args.test_mode),
        lambda: validate_repo(args.repo, args.test_mode),
        lambda: validate_version(args.version, args.test_mode),
        lambda: validate_output(args.output),
        lambda: validate_filter(args.filter, args.package),
    )
    for check in checks:
        try:
            check()
        except ConfigError as e:
            errors.append(str(e))
    if errors:
        raise ConfigError("\n".join(errors))
    return Config(args.package, args.repo, args.test_mode, args.version,
                  args.output, args.ascii_tree, args.filter,
                  args.load_order, args.plantuml_jar)


@dataclass
class Dependency:
    """Прямая зависимость пакета, объединённая по всем target framework."""
    id: str
    version_range: str
    frameworks: List[str] = field(default_factory=list)

    @property
    def min_version(self) -> str:
        return resolve_version(self.version_range)


def http_get(url: str) -> bytes:
    """GET-запрос средствами stdlib с обработкой сетевых ошибок."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except HTTPError as e:
        if e.code == HTTP_NOT_FOUND:
            raise RepoError(f"ресурс не найден (HTTP 404): {url}") from e
        raise RepoError(f"сервер вернул HTTP {e.code} для {url}") from e
    except (socket.timeout, TimeoutError) as e:
        raise RepoError(
            f"превышено время ожидания ({HTTP_TIMEOUT} c): {url}") from e
    except URLError as e:
        raise RepoError(f"не удалось подключиться к {url}: {e.reason}") from e


def _load_resources(index_url: str) -> list:
    """Скачивает service index и возвращает список 'resources'."""
    raw = http_get(index_url)
    try:
        index = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RepoError(
            f"{index_url} не является корректным JSON "
            f"(service index NuGet v3)") from e

    resources = index.get("resources") if isinstance(index, dict) else None
    if not isinstance(resources, list):
        raise RepoError(
            f"в {index_url} нет списка 'resources': "
            f"это не NuGet v3 service index")
    return resources


def _package_base_of(res) -> Optional[str]:
    """URL (с завершающим '/'), если ресурс — PackageBaseAddress."""
    types = res.get("@type", []) if isinstance(res, dict) else []
    if isinstance(types, str):
        types = [types]
    is_base = any(t.startswith("PackageBaseAddress") for t in types)
    if is_base and res.get("@id"):
        base = res["@id"]
        return base if base.endswith("/") else base + "/"
    return None


def find_package_base_address(index_url: str) -> str:
    """Читает service index (v3/index.json), ищет PackageBaseAddress."""
    for res in _load_resources(index_url):
        base = _package_base_of(res)
        if base is not None:
            return base
    raise RepoError("в service index не найден ресурс PackageBaseAddress")


def local_name(tag: str) -> str:
    """Имя тега без пространства имён (xmlns в .nuspec бывает разным)."""
    return tag.rsplit("}", 1)[-1]


def _find_child(parent, name: str):
    return next((c for c in parent if local_name(c.tag) == name), None)


def _dependencies_element(nuspec: bytes):
    """Разбирает XML и возвращает <dependencies> (или None, если нет)."""
    try:
        root = etree.fromstring(nuspec)
    except etree.ParseError as e:
        raise RepoError(f"некорректный XML в .nuspec: {e}") from e

    metadata = _find_child(root, "metadata")
    if metadata is None:
        raise RepoError("в .nuspec отсутствует элемент <metadata>")
    return _find_child(metadata, "dependencies")


def _add_dependency(found: Dict[str, Dependency], dep_el,
                    framework: str) -> None:
    dep_id = dep_el.get("id")
    if not dep_id:
        return
    key = dep_id.lower()
    new_dep = Dependency(dep_id, dep_el.get("version", ""))
    dep = found.setdefault(key, new_dep)
    if framework not in dep.frameworks:
        dep.frameworks.append(framework)


def _collect_dependencies(deps_el) -> List[Dependency]:
    found: Dict[str, Dependency] = {}
    for child in deps_el:
        name = local_name(child.tag)
        if name == "group":
            fw = child.get("targetFramework") or "any"
            for d in child:
                if local_name(d.tag) == "dependency":
                    _add_dependency(found, d, fw)
        elif name == "dependency":
            _add_dependency(found, child, "any")
    return list(found.values())


def parse_dependencies(nuspec: bytes) -> List[Dependency]:
    """Извлекает <dependency> из всех <group> (и без групп)."""
    deps_el = _dependencies_element(nuspec)
    if deps_el is None:
        return []
    return _collect_dependencies(deps_el)


def resolve_version(version_range: str) -> str:
    """Выбирает конкретную версию из диапазона NuGet: нижнюю границу.

    "1.0" -> 1.0; "[1.0, )" -> 1.0; "[1.0]" -> 1.0; "(, 2.0]" -> 2.0.
    Исключающие границы упрощённо трактуются как включающие.
    """
    rng = version_range.strip()
    if not rng:
        return ""
    if rng.startswith(("[", "(")):
        parts = [p.strip() for p in rng[1:-1].split(",")]
        # нижняя граница, а если её нет — верхняя
        return parts[0] or "".join(parts[1:2])
    return rng


def get_direct_dependencies(repo_url: str, package: str, version: str,
                            base: str = "") -> List[Dependency]:
    """Прямые зависимости пакета заданной версии (репозиторий NuGet v3).

    base — уже найденный PackageBaseAddress
    (чтобы не читать service index повторно).
    """
    base = base or find_package_base_address(repo_url)
    pid = package.lower()
    ver = version.split("+", 1)[0].lower()
    url = f"{base}{pid}/{ver}/{pid}.nuspec"
    try:
        nuspec = http_get(url)
    except RepoError as e:
        if "HTTP 404" in str(e):
            raise RepoError(
                f"пакет {package} версии {version} "
                f"не найден в репозитории") from e
        raise
    return parse_dependencies(nuspec)


ChildrenFn = Callable[[str, str], List[Tuple[str, str]]]


@dataclass
class GraphResult:
    root: str
    graph: Dict[str, List[str]]
    versions: Dict[str, str]
    cycles: List[List[str]]
    skipped: List[str]


class _GraphBuilder:
    """Состояние рекурсивного DFS при построении графа зависимостей."""

    def __init__(self, get_children: ChildrenFn, substring: str):
        self.get_children = get_children
        self.sub = substring.lower()
        self.graph: Dict[str, List[str]] = {}
        self.versions: Dict[str, str] = {}
        self.cycles: List[List[str]] = []
        self.skipped: List[str] = []
        self.canon: Dict[str, str] = {}
        self.stack: List[str] = []
        self.on_stack = set()

    def is_filtered(self, name: str) -> bool:
        """True, если пакет исключён фильтром (и запоминает его)."""
        if self.sub and self.sub in name.lower():
            if name not in self.skipped:
                self.skipped.append(name)
            return True
        return False

    def add_edge(self, name: str, dep_name: str, dep_version: str) -> None:
        dep = self.canon.setdefault(dep_name.lower(), dep_name)
        if dep not in self.graph[name]:
            self.graph[name].append(dep)
        if dep in self.on_stack:
            start = self.stack.index(dep)
            self.cycles.append(self.stack[start:] + [dep])
        elif dep not in self.graph:
            self.dfs(dep, dep_version)

    def dfs(self, name: str, version: str) -> None:
        self.graph[name] = []
        self.versions[name] = version
        self.stack.append(name)
        self.on_stack.add(name)
        for dep_name, dep_version in self.get_children(name, version):
            if not self.is_filtered(dep_name):
                self.add_edge(name, dep_name, dep_version)
        self.stack.pop()
        self.on_stack.discard(name)


def build_graph(root: str, root_version: str, get_children: ChildrenFn,
                substring: str = "") -> GraphResult:
    """Строит граф зависимостей с учётом транзитивности рекурсивным DFS.

    * Пакеты, чьё имя содержит substring (без учёта регистра), не
      анализируются и в граф не попадают.
    * Цикл обнаруживается как ребро в пакет, который сейчас находится в
      стеке рекурсии: ребро сохраняется, но рекурсия в него не идёт.
    * Уже обработанный пакет повторно не раскрывается (ромбовидные
      зависимости).
    """
    builder = _GraphBuilder(get_children, substring)
    root = builder.canon.setdefault(root.lower(), root)
    builder.dfs(root, root_version)
    return GraphResult(root, builder.graph, builder.versions,
                       builder.cycles, builder.skipped)


def parse_test_repo(path: str) -> Dict[str, List[str]]:
    """Читает файл описания графа. Формат строки:  A: B C.

    Зависимости перечисляются через пробел.
    Пустые строки и строки, начинающиеся с '#', игнорируются.
    Пакет без зависимостей:  C:
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise RepoError(f"не удалось прочитать файл '{path}': {e}") from e

    repo: Dict[str, List[str]] = {}
    for n, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, deps = _parse_test_line(line, raw, f"{path}:{n}")
        if name in repo:
            raise RepoError(f"{path}:{n}: пакет '{name}' описан повторно")
        repo[name] = deps
    if not repo:
        raise RepoError(
            f"файл '{path}' не содержит описания ни одного пакета")
    return repo


def _parse_test_line(line: str, raw: str,
                     where: str) -> Tuple[str, List[str]]:
    """Разбирает одну строку 'A: B C' тестового репозитория."""
    if ":" not in line:
        raise RepoError(
            f"{where}: ожидался формат 'A: B C', получено '{raw.strip()}'")
    left, right = line.split(":", 1)
    name = left.strip()
    if not TEST_ID_RE.fullmatch(name):
        raise RepoError(
            f"{where}: имя пакета '{name}' должно состоять "
            f"из заглавных латинских букв")
    deps = right.split()
    for d in deps:
        if not TEST_ID_RE.fullmatch(d):
            raise RepoError(
                f"{where}: имя зависимости '{d}' должно состоять "
                f"из заглавных латинских букв")
    return name, deps


def make_test_children(repo: Dict[str, List[str]]) -> ChildrenFn:
    return lambda name, version: [(d, "") for d in repo.get(name, [])]


def make_nuget_children(repo_url: str) -> ChildrenFn:
    """Провайдер прямых зависимостей из NuGet с кэшем.

    Версия транзитивной зависимости — нижняя граница диапазона из .nuspec.
    """
    base = find_package_base_address(repo_url)
    cache: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}

    def children(name: str, version: str) -> List[Tuple[str, str]]:
        key = (name.lower(), version)
        if key not in cache:
            result = []
            for d in get_direct_dependencies(repo_url, name, version, base):
                if not d.min_version:
                    raise RepoError(
                        f"у зависимости {d.id} пакета {name} {version} "
                        f"не указана версия")
                if not VERSION_RE.fullmatch(d.min_version):
                    raise RepoError(
                        f"не удалось определить версию зависимости {d.id} "
                        f"из диапазона '{d.version_range}' "
                        f"(пакет {name} {version})")
                result.append((d.id, d.min_version))
            cache[key] = result
        return cache[key]

    return children


def _label(res: GraphResult, name: str) -> str:
    v = res.versions.get(name, "")
    return f"{name} {v}" if v else name


def print_graph(res: GraphResult, substring: str) -> None:
    edges = sum(len(d) for d in res.graph.values())
    print(f"Граф зависимостей пакета {_label(res, res.root)}: "
          f"пакетов {len(res.graph)}, рёбер {edges}")
    for name, deps in res.graph.items():
        right = ", ".join(_label(res, d) for d in deps)
        print(f"  {_label(res, name)} -> {right or '(нет зависимостей)'}")
    if res.cycles:
        print(f"Циклические зависимости ({len(res.cycles)}):")
        for c in res.cycles:
            print("  " + " -> ".join(_label(res, x) for x in c))
    else:
        print("Циклических зависимостей нет")
    if res.skipped:
        print(f"Пропущено по фильтру '{substring}': "
              f"{', '.join(res.skipped)}")


def load_order(res: GraphResult) -> List[str]:
    """Порядок загрузки: пост-порядок рекурсивного DFS.

    Пакет попадает в список после всех своих зависимостей, поэтому при
    установке в этом порядке каждый пакет получает уже загруженные
    зависимости. Корневой пакет всегда последний. Обратные рёбра циклов
    игнорируются (цикл «разрывается» там, где DFS в него вернулся), а при
    ромбовидных зависимостях общий пакет загружается один раз.
    """
    order: List[str] = []
    visited = set()

    def visit(name: str) -> None:
        visited.add(name)
        for dep in res.graph.get(name, []):
            if dep not in visited:
                visit(dep)
        order.append(name)

    visit(res.root)
    return order


def print_load_order(res: GraphResult, substring: str) -> None:
    """(только для этапа 4) вывод порядка загрузки."""
    order = load_order(res)
    print(f"Порядок загрузки зависимостей пакета {_label(res, res.root)} "
          f"({len(order)}):")
    for i, name in enumerate(order, 1):
        print(f"  {i}. {_label(res, name)}")
    if res.cycles:
        broken = ", ".join(
            f"{_label(res, c[-2])} -> {_label(res, c[-1])}"
            for c in res.cycles)
        print(f"Циклы разорваны по рёбрам: {broken}")
    if res.skipped:
        print(f"Пропущено по фильтру '{substring}': "
              f"{', '.join(res.skipped)}")


class RenderError(Exception):
    """Ошибка построения изображения."""


def to_plantuml(res: GraphResult) -> str:
    """Текстовое представление графа на языке PlantUML.

    Корень выделен цветом, рёбра, замыкающие цикл, — красным пунктиром.
    Все узлы объявляются явно, поэтому пакет без зависимостей тоже виден.
    """
    ids = {name: f"n{i}" for i, name in enumerate(res.graph)}
    back = {(c[-2], c[-1]) for c in res.cycles}
    lines = ["@startuml",
             f"title Зависимости {_label(res, res.root)}",
             "skinparam shadowing false"]
    for name, nid in ids.items():
        color = " #LightSkyBlue" if name == res.root else ""
        lines.append(f'rectangle "{_label(res, name)}" as {nid}{color}')
    for name, deps in res.graph.items():
        for dep in deps:
            if (name, dep) in back:
                lines.append(
                    f"{ids[name]} -[#red,dashed]-> {ids[dep]} : цикл")
            else:
                lines.append(f"{ids[name]} --> {ids[dep]}")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def ascii_tree(res: GraphResult) -> str:
    """Зависимости в виде ASCII-дерева (рекурсивный обход).

    (*) — поддерево этого пакета уже показано выше;
    (цикл) — возврат к предку.
    """
    lines = [_label(res, res.root)]
    shown = {res.root}
    path = {res.root}
    marks = False

    def walk(name: str, prefix: str) -> None:
        nonlocal marks
        deps = res.graph[name]
        for i, dep in enumerate(deps, 1):
            last = i == len(deps)
            branch = "└── " if last else "├── "
            text = prefix + branch + _label(res, dep)
            if dep in path:
                lines.append(text + " (цикл)")
                marks = True
            elif dep in shown and res.graph[dep]:
                lines.append(text + " (*)")
                marks = True
            else:
                lines.append(text)
                shown.add(dep)
                path.add(dep)
                walk(dep, prefix + ("    " if last else "│   "))
                path.discard(dep)

    walk(res.root, "")
    if marks:
        lines.append("(*) — поддерево показано выше; "
                     "(цикл) — возврат к пакету-предку")
    return "\n".join(lines)


_PUML_ALPHABET = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
)


def plantuml_encode(text: str) -> str:
    """Кодирование диаграммы для URL PlantUML (deflate + свой base64)."""
    data = zlib.compress(text.encode("utf-8"), 9)[2:-4]
    out = []
    for i in range(0, len(data), 3):
        b1, b2, b3 = data[i:i + 3].ljust(3, b"\0")
        out.append(_PUML_ALPHABET[b1 >> 2])
        out.append(_PUML_ALPHABET[((b1 & 0x3) << 4) | (b2 >> 4)])
        out.append(_PUML_ALPHABET[((b2 & 0xF) << 2) | (b3 >> 6)])
        out.append(_PUML_ALPHABET[b3 & 0x3F])
    return "".join(out)


def render_png_server(puml: str, output: str,
                      server: str = PLANTUML_SERVER) -> None:
    url = f"{server.rstrip('/')}/png/{plantuml_encode(puml)}"
    if len(url) > MAX_URL_LENGTH:
        raise RenderError(
            "диаграмма слишком велика для запроса к серверу PlantUML; "
            "используйте --plantuml-jar")
    try:
        png = http_get(url)
    except RepoError as e:
        raise RenderError(
            f"сервер PlantUML недоступен или отклонил запрос: {e}") from e
    if not png.startswith(b"\x89PNG"):
        raise RenderError("сервер PlantUML вернул не PNG-изображение")
    with open(output, "wb") as f:
        f.write(png)


def render_png_jar(puml_path: str, jar: str, output: str) -> None:
    java = shutil.which("java")
    if not java:
        raise RenderError("Java не найдена в PATH (нужна для plantuml.jar)")
    cmd = [java, "-jar", jar, "-tpng", "-charset", "UTF-8", puml_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=JAR_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RenderError(
            f"plantuml.jar не завершился за {JAR_TIMEOUT} секунд") from e
    if proc.returncode:
        details = (proc.stderr or proc.stdout).strip()
        raise RenderError(f"plantuml.jar завершился с ошибкой: {details}")
    if not os.path.isfile(output):
        raise RenderError(f"plantuml.jar не создал файл '{output}'")


def save_visualization(res: GraphResult, output: str, jar: str = "") -> str:
    """Сохраняет .puml рядом с PNG и строит PNG. Возвращает путь к .puml."""
    puml = to_plantuml(res)
    puml_path = os.path.splitext(output)[0] + ".puml"
    try:
        with open(puml_path, "w", encoding="utf-8") as f:
            f.write(puml)
    except OSError as e:
        raise RenderError(f"не удалось записать '{puml_path}': {e}") from e
    print(f"Текст PlantUML сохранён: {puml_path}")
    try:
        if jar:
            render_png_jar(puml_path, jar, output)
        else:
            render_png_server(puml, output)
    except OSError as e:
        raise RenderError(f"не удалось записать '{output}': {e}") from e
    return puml_path


def _build_result(cfg: Config) -> GraphResult:
    """Читает репозиторий (тестовый или NuGet) и строит граф."""
    if cfg.test_mode:
        repo = parse_test_repo(cfg.repo)
        if cfg.package not in repo:
            raise RepoError(
                f"пакет '{cfg.package}' не описан "
                f"в тестовом репозитории '{cfg.repo}'")
        children, version = make_test_children(repo), ""
    else:
        children, version = make_nuget_children(cfg.repo), cfg.version
    return build_graph(cfg.package, version, children, cfg.filter)


def _print_config_errors(error: ConfigError) -> None:
    print("Ошибка конфигурации:", file=sys.stderr)
    for line in str(error).splitlines():
        print(f"  {line}", file=sys.stderr)


def _output_result(cfg: Config, result: GraphResult) -> int:
    """Выводит граф в консоль и сохраняет изображение."""
    print_graph(result, cfg.filter)
    if cfg.ascii_tree:
        print("\nASCII-дерево зависимостей:")
        print(ascii_tree(result))
    try:
        save_visualization(result, cfg.output, cfg.plantuml_jar)
    except RenderError as e:
        print(f"Ошибка визуализации: {e}", file=sys.stderr)
        return EXIT_RENDER_ERROR
    print(f"Изображение графа сохранено: {cfg.output}")
    return EXIT_OK


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = validate(args)
    except ConfigError as e:
        _print_config_errors(e)
        return EXIT_CONFIG_ERROR

    sys.setrecursionlimit(RECURSION_LIMIT)
    try:
        result = _build_result(cfg)
    except RepoError as e:
        print(f"Ошибка репозитория: {e}", file=sys.stderr)
        return EXIT_REPO_ERROR
    except RecursionError:
        print("Ошибка: слишком глубокая вложенность зависимостей",
              file=sys.stderr)
        return EXIT_REPO_ERROR

    if cfg.load_order:
        print_load_order(result, cfg.filter)
        return EXIT_OK
    return _output_result(cfg, result)


if __name__ == "__main__":
    sys.exit(main())
