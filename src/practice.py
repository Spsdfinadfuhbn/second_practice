import argparse
import json
import os
import re
import socket
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

NUGET_ID_RE = re.compile(r"^[A-Za-z0-9_]+([.-][A-Za-z0-9_]+)*$")
TEST_ID_RE = re.compile(r"^[A-Z]+$")  
VERSION_RE = re.compile(
    r"^\d+(\.\d+){1,3}(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?(\+[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$"
)
FORBIDDEN_FILENAME_CHARS = set('<>:"|?*\0')
HTTP_TIMEOUT = 15  
USER_AGENT = "depgraph/0.3 (educational project)"


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="depgraph",
        description="Визуализация графа зависимостей пакета (формат NuGet).",
    )
    p.add_argument("-p", "--package", required=True,
                   help="имя анализируемого пакета")
    p.add_argument("-r", "--repo", required=True,
                   help="URL репозитория (https://api.nuget.org/v3/index.json) "
                        "или путь к файлу тестового репозитория (с --test-mode)")
    p.add_argument("-t", "--test-mode", action="store_true",
                   help="режим работы с тестовым репозиторием (--repo — путь к файлу)")
    p.add_argument("-v", "--version", default="",
                   help="версия пакета, например 13.0.3 (обязательна вне тестового режима)")
    p.add_argument("-o", "--output", default="graph.png",
                   help="имя файла с изображением графа (.png), по умолчанию graph.png")
    p.add_argument("-a", "--ascii-tree", action="store_true",
                   help="вывести зависимости в виде ASCII-дерева")
    p.add_argument("-f", "--filter", default="",
                   help="подстрока: пакеты, в имени которых она есть, не анализируются")
    return p


def validate_package(name: str, test_mode: bool) -> None:
    if not name or not name.strip():
        raise ConfigError("--package: имя пакета не может быть пустым")
    if test_mode:
        if not TEST_ID_RE.fullmatch(name):
            raise ConfigError(
                f"--package: в тестовом режиме имя должно состоять из заглавных "
                f"латинских букв, получено '{name}'")
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
                f"--repo: '{repo}' не является корректным URL (нужен http:// или https://). "
                f"Для пути к файлу используйте --test-mode")


def validate_version(version: str, test_mode: bool) -> None:
    if not version:
        if not test_mode:
            raise ConfigError("--version: версия обязательна вне тестового режима")
        return
    if not VERSION_RE.fullmatch(version):
        raise ConfigError(
            f"--version: '{version}' не соответствует формату версии (пример: 1.2.3, 1.0.0-beta.1)")


def validate_output(output: str) -> None:
    if not output or not output.strip():
        raise ConfigError("--output: имя файла не может быть пустым")
    if any(c in FORBIDDEN_FILENAME_CHARS for c in output):
        raise ConfigError(f"--output: имя '{output}' содержит недопустимые символы")
    if not output.lower().endswith(".png"):
        raise ConfigError(f"--output: файл должен иметь расширение .png, получено '{output}'")
    if os.path.basename(output) in ("", ".png"):
        raise ConfigError("--output: у файла отсутствует имя")
    directory = os.path.dirname(output) or "."
    if not os.path.isdir(directory):
        raise ConfigError(f"--output: каталог '{directory}' не существует")
    if os.path.isdir(output):
        raise ConfigError(f"--output: '{output}' — это каталог")
    if not os.access(directory, os.W_OK):
        raise ConfigError(f"--output: нет прав на запись в каталог '{directory}'")


def validate_filter(substr: str, package: str) -> None:
    if substr != "" and not substr.strip():
        raise ConfigError("--filter: подстрока не может состоять только из пробелов")
    if substr and package and substr.lower() in package.lower():
        raise ConfigError(
            f"--filter: подстрока '{substr}' входит в имя анализируемого пакета '{package}', "
            f"анализировать было бы нечего")


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
                  args.output, args.ascii_tree, args.filter)


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
    """GET-запрос средствами стандартной библиотеки с обработкой сетевых ошибок."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except HTTPError as e:
        if e.code == 404:
            raise RepoError(f"ресурс не найден (HTTP 404): {url}") from e
        raise RepoError(f"сервер вернул HTTP {e.code} для {url}") from e
    except (socket.timeout, TimeoutError) as e:
        raise RepoError(f"превышено время ожидания ({HTTP_TIMEOUT} c): {url}") from e
    except URLError as e:
        raise RepoError(f"не удалось подключиться к {url}: {e.reason}") from e


def find_package_base_address(index_url: str) -> str:
    """Читает service index (v3/index.json) и находит ресурс PackageBaseAddress."""
    raw = http_get(index_url)
    try:
        index = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RepoError(f"{index_url} не является корректным JSON (service index NuGet v3)") from e

    resources = index.get("resources") if isinstance(index, dict) else None
    if not isinstance(resources, list):
        raise RepoError(f"в {index_url} нет списка 'resources': это не NuGet v3 service index")

    for res in resources:
        types = res.get("@type", []) if isinstance(res, dict) else []
        if isinstance(types, str):
            types = [types]
        if any(t.startswith("PackageBaseAddress") for t in types) and res.get("@id"):
            base = res["@id"]
            return base if base.endswith("/") else base + "/"
    raise RepoError("в service index не найден ресурс PackageBaseAddress")


def local_name(tag: str) -> str:
    """Имя тега без пространства имён (xmlns в .nuspec бывает разным)."""
    return tag.rsplit("}", 1)[-1]


def parse_dependencies(nuspec: bytes) -> List[Dependency]:
    """Извлекает <dependency> из всех <group> (и из <dependencies> без групп)."""
    try:
        root = ET.fromstring(nuspec)
    except ET.ParseError as e:
        raise RepoError(f"некорректный XML в .nuspec: {e}") from e

    metadata = next((c for c in root if local_name(c.tag) == "metadata"), None)
    if metadata is None:
        raise RepoError("в .nuspec отсутствует элемент <metadata>")
    deps_el = next((c for c in metadata if local_name(c.tag) == "dependencies"), None)
    if deps_el is None:
        return []  

    found: Dict[str, Dependency] = {}

    def add(dep_el, framework: str) -> None:
        dep_id = dep_el.get("id")
        if not dep_id:
            return
        key = dep_id.lower() 
        dep = found.setdefault(key, Dependency(dep_id, dep_el.get("version", "")))
        if framework not in dep.frameworks:
            dep.frameworks.append(framework)

    for child in deps_el:
        name = local_name(child.tag)
        if name == "group":
            fw = child.get("targetFramework") or "any"
            for d in child:
                if local_name(d.tag) == "dependency":
                    add(d, fw)
        elif name == "dependency":
            add(child, "any")
    return list(found.values())


def resolve_version(version_range: str) -> str:
    """Выбирает конкретную версию из диапазона NuGet: нижнюю границу.

    "1.0" -> 1.0; "[1.0, )" -> 1.0; "[1.0]" -> 1.0; "(, 2.0]" -> 2.0.
    Исключающие границы упрощённо трактуются как включающие.
    """
    rng = version_range.strip()
    if not rng:
        return ""
    if rng[0] in "[(":
        parts = [p.strip() for p in rng[1:-1].split(",")]
        if parts[0]:
            return parts[0]
        return parts[1] if len(parts) > 1 else ""
    return rng


def get_direct_dependencies(repo_url: str, package: str, version: str,
                            base: str = "") -> List[Dependency]:
    """Прямые зависимости пакета заданной версии по URL репозитория NuGet v3.

    base — уже найденный PackageBaseAddress (чтобы не читать service index повторно).
    """
    base = base or find_package_base_address(repo_url)
    pid = package.lower()                     
    ver = version.split("+", 1)[0].lower()    
    url = f"{base}{pid}/{ver}/{pid}.nuspec"
    try:
        nuspec = http_get(url)
    except RepoError as e:
        if "HTTP 404" in str(e):
            raise RepoError(f"пакет {package} версии {version} не найден в репозитории") from e
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


def build_graph(root: str, root_version: str, get_children: ChildrenFn,
                substring: str = "") -> GraphResult:
    """Строит граф зависимостей с учётом транзитивности рекурсивным DFS.

    * Пакеты, чьё имя содержит substring (без учёта регистра), не анализируются
      и в граф не попадают.
    * Цикл обнаруживается как ребро в пакет, который сейчас находится в стеке
      рекурсии: ребро сохраняется, но рекурсия в него не идёт.
    * Уже обработанный пакет повторно не раскрывается (ромбовидные зависимости).
    """
    sub = substring.lower()
    graph: Dict[str, List[str]] = {}
    versions: Dict[str, str] = {}
    cycles: List[List[str]] = []
    skipped: List[str] = []
    canon: Dict[str, str] = {}      
    stack: List[str] = []            
    on_stack = set()

    def dfs(name: str, version: str) -> None:
        graph[name] = []
        versions[name] = version
        stack.append(name)
        on_stack.add(name)
        for dep_name, dep_version in get_children(name, version):
            if sub and sub in dep_name.lower():
                if dep_name not in skipped:
                    skipped.append(dep_name)
                continue
            dep = canon.setdefault(dep_name.lower(), dep_name)
            if dep not in graph[name]:
                graph[name].append(dep)
            if dep in on_stack:                       
                cycles.append(stack[stack.index(dep):] + [dep])
            elif dep not in graph:                    
                dfs(dep, dep_version)
        stack.pop()
        on_stack.discard(name)

    root = canon.setdefault(root.lower(), root)
    dfs(root, root_version)
    return GraphResult(root, graph, versions, cycles, skipped)


def parse_test_repo(path: str) -> Dict[str, List[str]]:
    """Читает файл описания графа. Формат строки:  A: B C   (зависимости через пробел).

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
        where = f"{path}:{n}"
        if ":" not in line:
            raise RepoError(f"{where}: ожидался формат 'A: B C', получено '{raw.strip()}'")
        left, right = line.split(":", 1)
        name = left.strip()
        if not TEST_ID_RE.fullmatch(name):
            raise RepoError(f"{where}: имя пакета '{name}' должно состоять из заглавных латинских букв")
        if name in repo:
            raise RepoError(f"{where}: пакет '{name}' описан повторно")
        deps = right.split()
        for d in deps:
            if not TEST_ID_RE.fullmatch(d):
                raise RepoError(f"{where}: имя зависимости '{d}' должно состоять из заглавных латинских букв")
        repo[name] = deps
    if not repo:
        raise RepoError(f"файл '{path}' не содержит описания ни одного пакета")
    return repo


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
                    raise RepoError(f"у зависимости {d.id} пакета {name} {version} не указана версия")
                if not VERSION_RE.fullmatch(d.min_version):
                    raise RepoError(
                        f"не удалось определить версию зависимости {d.id} "
                        f"из диапазона '{d.version_range}' (пакет {name} {version})")
                result.append((d.id, d.min_version))
            cache[key] = result
        return cache[key]

    return children


def print_graph(res: GraphResult, substring: str) -> None:
    def label(name: str) -> str:
        v = res.versions.get(name, "")
        return f"{name} {v}" if v else name

    edges = sum(len(d) for d in res.graph.values())
    print(f"Граф зависимостей пакета {label(res.root)}: пакетов {len(res.graph)}, рёбер {edges}")
    for name, deps in res.graph.items():
        right = ", ".join(label(d) for d in deps) if deps else "(нет зависимостей)"
        print(f"  {label(name)} -> {right}")
    if res.cycles:
        print(f"Циклические зависимости ({len(res.cycles)}):")
        for c in res.cycles:
            print("  " + " -> ".join(label(x) for x in c))
    else:
        print("Циклических зависимостей нет")
    if res.skipped:
        print(f"Пропущено по фильтру '{substring}': {', '.join(res.skipped)}")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = validate(args)
    except ConfigError as e:
        print("Ошибка конфигурации:", file=sys.stderr)
        for line in str(e).splitlines():
            print(f"  {line}", file=sys.stderr)
        return 2

    sys.setrecursionlimit(5000)
    try:
        if cfg.test_mode:
            repo = parse_test_repo(cfg.repo)
            if cfg.package not in repo:
                raise RepoError(f"пакет '{cfg.package}' не описан в тестовом репозитории '{cfg.repo}'")
            children, version = make_test_children(repo), ""
        else:
            children, version = make_nuget_children(cfg.repo), cfg.version
        result = build_graph(cfg.package, version, children, cfg.filter)
    except RepoError as e:
        print(f"Ошибка репозитория: {e}", file=sys.stderr)
        return 3
    except RecursionError:
        print("Ошибка: слишком глубокая вложенность зависимостей", file=sys.stderr)
        return 3

    print_graph(result, cfg.filter)
    return 0


if __name__ == "__main__":
    sys.exit(main())


 
