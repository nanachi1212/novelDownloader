"""User adapter plugin utilities.

GUI-generated adapters live outside the bundled ``sites`` package so users can
drop in or generate a new rule without editing the application source.
"""
import importlib.util
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def user_adapter_dir() -> Path:
    return app_root() / "user_adapters"


def safe_module_name(name: str) -> str:
    value = re.sub(r"[^0-9a-zA-Z_]+", "_", name.strip().lower()).strip("_")
    if not value:
        value = "custom_site"
    if value[0].isdigit():
        value = f"site_{value}"
    return value


def class_name_from_module(module_name: str) -> str:
    parts = [p for p in module_name.split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) + "Adapter"


def domains_from_url(url: str) -> list[str]:
    host = urlparse(url).netloc.lower()
    host = host.split(":")[0]
    if not host:
        raise ValueError(f"無法從網址解析網域: {url}")
    domains = [host]
    if host.startswith("www."):
        domains.append(host[4:])
    else:
        domains.append(f"www.{host}")
    return domains


def generate_adapter_template(url: str, module_name: str = "", display_name: str = "") -> str:
    domains = domains_from_url(url)
    module_name = safe_module_name(module_name or domains[0])
    class_name = class_name_from_module(module_name)
    label = display_name.strip() or domains[0]
    domain_literal = ", ".join(repr(d) for d in domains)
    return f'''"""User adapter for {label}.

This auto-generated adapter pins the domain to GenericAdapter first. If the
site needs special parsing, edit this file and override parse_catalog() or
parse_chapter() using sites/base.py as the interface reference.
"""
from sites.generic import GenericAdapter


class {class_name}(GenericAdapter):
    domains = [{domain_literal}]
    encoding = None
    adapter_label = {label!r}
'''


def write_generated_adapter(url: str, module_name: str = "", display_name: str = "") -> Path:
    directory = user_adapter_dir()
    directory.mkdir(parents=True, exist_ok=True)
    module_name = safe_module_name(module_name or urlparse(url).netloc or "custom_site")
    path = directory / f"{module_name}.py"
    if path.exists():
        raise FileExistsError(f"Adapter 已存在: {path}")
    path.write_text(generate_adapter_template(url, module_name, display_name), encoding="utf-8")
    return path


def install_adapter_file(src: str | Path) -> Path:
    src = Path(src)
    if src.suffix.lower() != ".py":
        raise ValueError("Adapter 檔案必須是 .py")
    directory = user_adapter_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_module_name(src.stem)
    target = target.with_suffix(".py")
    if src.resolve() != target.resolve():
        shutil.copy2(src, target)
    return target


def load_user_adapters(base_class) -> tuple[list[type], list[str]]:
    directory = user_adapter_dir()
    if not directory.exists():
        return [], []

    adapters = []
    errors = []
    for path in sorted(directory.glob("*.py")):
        if path.with_suffix(path.suffix + ".disabled").exists():
            continue
        module_name = f"user_adapters.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"無法建立 import spec: {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            for value in module.__dict__.values():
                if (
                    isinstance(value, type)
                    and issubclass(value, base_class)
                    and value is not base_class
                    and getattr(value, "domains", None)
                ):
                    value.adapter_source = str(path)
                    adapters.append(value)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    return adapters, errors


def list_user_adapter_files() -> list[Path]:
    directory = user_adapter_dir()
    if not directory.exists():
        return []
    return sorted(directory.glob("*.py"))


def toggle_adapter_enabled(path: str | Path) -> bool:
    """切換 adapter 啟用狀態；回傳切換後是否啟用。"""
    path = Path(path)
    marker = path.with_suffix(path.suffix + ".disabled")
    if marker.exists():
        marker.unlink()
        return True
    marker.write_text("停用此 adapter 的標記檔。", encoding="utf-8")
    return False


def adapter_is_enabled(path: str | Path) -> bool:
    path = Path(path)
    return not path.with_suffix(path.suffix + ".disabled").exists()
