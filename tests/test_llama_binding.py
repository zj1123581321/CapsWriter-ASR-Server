import ctypes
import importlib
import logging
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LLAMA_PACKAGE = "core.server.engines.llama"
LLAMA_MODULE_PATH = REPO_ROOT / "core/server/engines/llama/llama.py"
BIN_DIR = LLAMA_MODULE_PATH.parent / "bin"
BUILD_DIR = BIN_DIR / "b10621"
INFERENCE_PACKAGES = (
    "core.server.engines.qwen_asr_gguf.inference",
    "core.server.engines.force_aligner_gguf.inference",
    "core.server.engines.fun_asr_gguf.inference",
)
ENGINE_PACKAGES = tuple(name.rsplit(".inference", 1)[0] for name in INFERENCE_PACKAGES)


def _required_library_names():
    if sys.platform == "win32":
        return ("ggml.dll", "ggml-base.dll", "llama.dll")
    if sys.platform == "darwin":
        return ("libggml.dylib", "libggml-base.dylib", "libllama.dylib")
    return ("libggml.so", "libggml-base.so", "libllama.so")


def _clear_binding_imports():
    prefixes = (LLAMA_PACKAGE, *ENGINE_PACKAGES)
    for name in tuple(sys.modules):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            del sys.modules[name]


def _stub_inference_dependencies(monkeypatch):
    exported_names = {
        "asr": ("QwenASREngine",),
        "aligner": ("QwenForcedAligner",),
        "schema": (
            "ForcedAlignItem", "ForcedAlignResult", "DecodeResult",
            "AlignerConfig", "ASREngineConfig", "TranscribeResult",
        ),
        "chinese_itn": ("chinese_to_num",),
        "audio": ("load_audio",),
        "exporters": (),
    }
    for package_name in ENGINE_PACKAGES:
        package = types.ModuleType(package_name)
        package.__path__ = [str(REPO_ROOT / Path(*package_name.split(".")))]
        package.logger = logging.getLogger("binding-import-test")
        package.console = object()
        monkeypatch.setitem(sys.modules, package_name, package)
        if package_name.endswith("fun_asr_gguf"):
            continue
        for module_name, names in exported_names.items():
            dependency = types.ModuleType(f"{package_name}.inference.{module_name}")
            for exported_name in names:
                setattr(dependency, exported_name, type(exported_name, (), {}))
            monkeypatch.setitem(
                sys.modules, f"{package_name}.inference.{module_name}", dependency,
            )


def _mock_gguf_import(monkeypatch):
    gguf = types.ModuleType("gguf")
    constants = types.ModuleType("gguf.constants")
    constants.GGML_QUANT_SIZES = {}
    constants.GGMLQuantizationType = type("GGMLQuantizationType", (), {})
    monkeypatch.setitem(sys.modules, "gguf", gguf)
    monkeypatch.setitem(sys.modules, "gguf.constants", constants)
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))


def _mock_library_directory(monkeypatch, *, exists, present_files):
    original_is_dir = Path.is_dir
    original_is_file = Path.is_file

    def is_dir(path):
        if path == BUILD_DIR:
            return exists
        return original_is_dir(path)

    def is_file(path):
        if path.parent == BUILD_DIR and path.name in _required_library_names():
            return path.name in present_files
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(Path, "is_file", is_file)


class _FakeFunction:
    argtypes = None
    restype = None

    def __call__(self, *args):
        return None


class _FakeLibrary:
    def __init__(self, path):
        self.__file__ = path
        self._functions = {}

    def __getattr__(self, name):
        if name not in self._functions:
            self._functions[name] = _FakeFunction()
        return self._functions[name]


def test_missing_version_directory_fails_with_version_and_expected_path(monkeypatch):
    _clear_binding_imports()
    _mock_gguf_import(monkeypatch)
    _mock_library_directory(monkeypatch, exists=False, present_files=set())

    with pytest.raises(RuntimeError) as error:
        importlib.import_module(LLAMA_PACKAGE)

    assert "b10621" in str(error.value)
    assert str(BUILD_DIR) in str(error.value)


def test_missing_llama_library_is_named_in_startup_error(monkeypatch):
    _clear_binding_imports()
    _mock_gguf_import(monkeypatch)
    required = set(_required_library_names())
    missing_library = next(name for name in required if name.startswith(("llama", "libllama")))
    _mock_library_directory(
        monkeypatch, exists=True, present_files=required - {missing_library},
    )
    monkeypatch.setattr(ctypes, "CDLL", lambda path: _FakeLibrary(path))

    with pytest.raises(RuntimeError) as error:
        importlib.import_module(LLAMA_PACKAGE)

    assert missing_library in str(error.value)


def test_gguf_inference_packages_use_shared_llama_module(monkeypatch):
    _clear_binding_imports()
    _mock_gguf_import(monkeypatch)
    _mock_library_directory(
        monkeypatch, exists=True, present_files=set(_required_library_names()),
    )
    monkeypatch.setattr(ctypes, "CDLL", lambda path: _FakeLibrary(path))
    monkeypatch.setattr("os.chdir", lambda path: None)
    _stub_inference_dependencies(monkeypatch)

    shared_llama = importlib.import_module(f"{LLAMA_PACKAGE}.llama")
    shared_dir = LLAMA_MODULE_PATH.parent.resolve()
    assert Path(shared_llama.__file__).resolve().is_relative_to(shared_dir)

    for package_name in INFERENCE_PACKAGES:
        inference = importlib.import_module(package_name)
        assert Path(inference.llama.__file__).resolve().is_relative_to(shared_dir)
