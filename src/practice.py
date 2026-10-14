import argparse
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

NUGET_ID_RE = re.compile(r"^[A-Za-z0-9_]+([.-][A-Za-z0-9_]+)*$")
TEST_ID_RE = re.compile(r"^[A-Z]+$")
VERSION_RE = re.compile(
    r"^\d+(\.\d+){1,3}(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?(\+[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$"
)
FORBIDDEN_FILENAME_CHARS = set('<>:"|?*\0')


class ConfigError(Exception):
    """Ошибка пользовательской конфигурации."""


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
    p.add_argument(
        "-r",
        "--repo",
        required=True,
        help="URL репозитория (https://api.nuget.org/v3/index.json) "
        "или путь к файлу тестового репозитория (с --test-mode)")
    p.add_argument(
        "-t",
        "--test-mode",
        action="store_true",
        help="режим работы с тестовым репозиторием (--repo — путь к файлу)")
    p.add_argument(
        "-v",
        "--version",
        default="",
        help="версия пакета, например 13.0.3 (обязательна вне тестового режима)")
    p.add_argument(
        "-o",
        "--output",
        default="graph.png",
        help="имя файла с изображением графа (.png), по умолчанию graph.png")
    p.add_argument("-a", "--ascii-tree", action="store_true",
                   help="вывести зависимости в виде ASCII-дерева")
    p.add_argument(
        "-f",
        "--filter",
        default="",
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
            raise ConfigError(
                "--version: версия обязательна вне тестового режима")
        return
    if not VERSION_RE.fullmatch(version):
        raise ConfigError(
            f"--version: '{version}' не соответствует формату версии (пример: 1.2.3, 1.0.0-beta.1)")


def validate_output(output: str) -> None:
    if not output or not output.strip():
        raise ConfigError("--output: имя файла не может быть пустым")
    if any(c in FORBIDDEN_FILENAME_CHARS for c in output):
        raise ConfigError(
            f"--output: имя '{output}' содержит недопустимые символы")
    if not output.lower().endswith(".png"):
        raise ConfigError(
            f"--output: файл должен иметь расширение .png, получено '{output}'")
    if os.path.basename(output) in ("", ".png"):
        raise ConfigError("--output: у файла отсутствует имя")
    directory = os.path.dirname(output) or "."
    if not os.path.isdir(directory):
        raise ConfigError(f"--output: каталог '{directory}' не существует")
    if os.path.isdir(output):
        raise ConfigError(f"--output: '{output}' — это каталог")
    if not os.access(directory, os.W_OK):
        raise ConfigError(
            f"--output: нет прав на запись в каталог '{directory}'")


def validate_filter(substr: str) -> None:
    if substr != "" and not substr.strip():
        raise ConfigError(
            "--filter: подстрока не может состоять только из пробелов")


def validate(args: argparse.Namespace) -> Config:
    """Проверяет все параметры и собирает все ошибки сразу."""
    errors = []
    checks = (
        lambda: validate_package(args.package, args.test_mode),
        lambda: validate_repo(args.repo, args.test_mode),
        lambda: validate_version(args.version, args.test_mode),
        lambda: validate_output(args.output),
        lambda: validate_filter(args.filter),
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


def print_config(cfg: Config) -> None:
    """(только для этапа 1) вывод параметров в формате ключ-значение."""
    items = {
        "package": cfg.package,
        "repo": cfg.repo,
        "test_mode": cfg.test_mode,
        "version": cfg.version or "(не задана)",
        "output": cfg.output,
        "ascii_tree": cfg.ascii_tree,
        "filter": cfg.filter or "(не задан)",
    }
    for key, value in items.items():
        print(f"{key}: {value}")


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
    print_config(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
