import os
import re
import shlex
import subprocess
import sys
from functools import lru_cache

import sublime
from LSP.plugin import DottedDict
from LSP.plugin.core.protocol import WorkspaceFolder
from LSP.plugin.core.types import ClientConfig
from LSP.plugin.core.typing import Any, Callable, List, Optional, Tuple, cast
from lsp_utils import NpmClientHandler
from sublime_lib import ResourcePath

if int(sublime.version()) >= 4070:
    from LSP.plugin import MarkdownLangMap


# TODO: do we want all the "*requirements*.txt" files and are there
# any other roots that we should check for?
ROOT_FILES = {
    ".git",
    "poetry.lock",
    "setup.py",
    "dev-requirements.txt",
    "requirements-dev.txt",
    "requirements.txt",
    "test-requirements.txt",
}

USER_HOME = os.path.normpath(os.path.expanduser("~"))


@lru_cache
def is_project_root(root: str) -> bool:
    if root == USER_HOME:
        return True
    for name in ROOT_FILES:
        if os.path.exists(os.path.join(root, name)):
            return True
    return False


def plugin_loaded() -> None:
    LspPyrightPlugin.setup()


def plugin_unloaded() -> None:
    LspPyrightPlugin.cleanup()


class LspPyrightPlugin(NpmClientHandler):
    package_name = __package__.partition(".")[0]
    server_directory = "language-server"
    server_binary_path = os.path.join(server_directory, "node_modules", "pyright", "langserver.index.js")
    python_exe = "python" if sublime.platform() != "windows" else "python.exe"

    @classmethod
    def minimum_node_version(cls) -> Tuple[int, int, int]:
        return (14, 0, 0)

    def on_settings_changed(self, settings: DottedDict) -> None:
        super().on_settings_changed(settings)

        dev_environment = self.get_dev_environment(settings)

        if dev_environment in ("sublime_text", "sublime_text_33", "sublime_text_38"):
            if dev_environment == "sublime_text":
                # the Python version this plugin runs on
                py_ver = cast(Tuple[int, int], tuple(sys.version_info[:2]))
            else:
                py_ver = (3, 8) if dev_environment == "sublime_text_38" else (3, 3)

            # add package dependencies into "python.analysis.extraPaths"
            extraPaths = settings.get("python.analysis.extraPaths") or []  # type: List[str]
            extraPaths.extend(self.find_package_dependency_dirs(py_ver))
            settings.set("python.analysis.extraPaths", extraPaths)

    @classmethod
    def on_pre_start(
        cls,
        window: sublime.Window,
        initiating_view: sublime.View,
        workspace_folders: List[WorkspaceFolder],
        configuration: ClientConfig,
    ) -> Optional[str]:
        python_path = cls.resolve_python_path_from_venv(configuration.settings, workspace_folders) or "python"
        print('{}: Using python path "{}"'.format(cls.name(), python_path))
        configuration.settings.set("python.pythonPath", python_path)
        return None

    @classmethod
    def install_or_update(cls) -> None:
        super().install_or_update()
        # Copy resources
        src = "Packages/{}/resources/".format(cls.package_name)
        dest = os.path.join(cls.package_storage(), "resources")
        ResourcePath(src).copytree(dest, exist_ok=True)

    @classmethod
    def markdown_language_id_to_st_syntax_map(cls) -> Optional["MarkdownLangMap"]:
        return {"python": (("python", "py"), ("LSP-pyright/syntaxes/pyright",))}

    # -------------- #
    # custom methods #
    # -------------- #

    @classmethod
    def get_dev_environment(cls, settings: DottedDict) -> str:
        dev_environment = cls.get_plugin_setting("dev_environment")
        if dev_environment is None:
            dev_environment = settings.get("pyright.dev_environment")
        else:
            print(
                "[LSP-pyright] "
                + '"dev_environment" setting has been deprecated and will be removed in the future. '
                + 'Please use "pyright.dev_environment", which is under "settings" instead.'
            )

        return dev_environment

    @classmethod
    def get_plugin_setting(cls, key: str, default: Optional[Any] = None) -> Any:
        return sublime.load_settings(cls.package_name + ".sublime-settings").get(key, default)

    def find_package_dependency_dirs(self, py_ver: Tuple[int, int] = (3, 3)) -> List[str]:
        dep_dirs = sys.path.copy()

        # replace paths for target Python version
        # @see https://github.com/sublimelsp/LSP-pyright/issues/28
        re_pattern = r"(python3\.?)[38]"
        re_replacement = r"\g<1>8" if py_ver == (3, 8) else r"\g<1>3"
        dep_dirs = [re.sub(re_pattern, re_replacement, d, flags=re.IGNORECASE) for d in dep_dirs]

        # move the "Packages/" to the last
        # @see https://github.com/sublimelsp/LSP-pyright/pull/26#discussion_r520747708
        packages_path = sublime.packages_path()
        dep_dirs.remove(packages_path)
        dep_dirs.append(packages_path)

        # sublime stubs - add as first
        dep_dirs.insert(0, os.path.join(self.package_storage(), "resources", "typings", "sublime_text"))

        return [path for path in dep_dirs if os.path.isdir(path)]

    @classmethod
    def _venv_path(cls, root: str, venv_name: str = "venv") -> Optional[str]:
        venv = os.path.join(root, venv_name, "bin", cls.python_exe)
        return venv if os.path.exists(venv) else None

    @classmethod
    def _find_venv(cls, root: str) -> Optional[str]:
        if not os.path.lexists(root):
            return None
        while True:
            venv = cls._venv_path(root)
            if venv is not None:
                return venv
            elif is_project_root(root):
                return None
            else:
                dirname = os.path.dirname(root)
                if len(dirname) >= len(root) or dirname == USER_HOME:
                    return None
                root = dirname

    # TODO: use this
    # TODO: check sub-folders
    @classmethod
    def resolve_virtualenv(
        cls, settings: DottedDict, folders: List[WorkspaceFolder],
    ) -> Optional[str]:
        """ resolve_virtualenv returns the path to the first "venv" python
        """

        # WARN: do we want this?
        python_path = settings.get("python.pythonPath")
        if python_path:
            return python_path

        if folders:
            for folder in folders:
                venv = cls._find_venv(folder.path)
                if venv is not None:
                    return venv

        return None

    @classmethod
    def resolve_python_path_from_venv(
        cls, settings: DottedDict, workspace_folders: List[WorkspaceFolder]
    ) -> Optional[str]:
        """
        Resolves the python binary path depending on environment variables and files in the workspace.

        See https://github.com/fannheyward/coc-pyright/blob/d58a468b1d7479a1b56906e386f44b997181e307/src/configSettings.ts#L47.  # noqa: E501
        """

        def binary_from_python_path(path: str) -> Optional[str]:
            if sublime.platform() == "windows":
                binary_path = os.path.join(path, "Scripts", "python.exe")
            else:
                binary_path = os.path.join(path, "bin", "python")

            return binary_path if os.path.isfile(binary_path) else None

        python_path = settings.get("python.pythonPath")
        if python_path:
            return python_path

        if not workspace_folders:
            return None
        workspace_folder = workspace_folders[0].path

        # Config file, venv resolution command, post-processing
        venv_config_files = [
            ("Pipfile", ["pipenv", "--py"], None),
            ("poetry.lock", ["poetry", "env", "info", "-p"], binary_from_python_path),
            (".python-version", ["pyenv", "which", "python"], None),
        ]  # type: List[Tuple[str, List[str], Optional[Callable[[str], Optional[str]]]]]

        if sublime.platform() == "windows":
            # do not create a window for the process
            startupinfo = subprocess.STARTUPINFO()  # type: ignore
            startupinfo.wShowWindow = subprocess.SW_HIDE  # type: ignore
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore
        else:
            startupinfo = None  # type: ignore

        for config_file, command, post_processing in venv_config_files:
            full_config_file_path = os.path.join(workspace_folder, config_file)
            if os.path.isfile(full_config_file_path):
                try:
                    python_path = subprocess.check_output(
                        command, cwd=workspace_folder, startupinfo=startupinfo, universal_newlines=True
                    ).strip()
                    return post_processing(python_path) if post_processing else python_path
                except FileNotFoundError:
                    print("{}: WARN: {} detected but {} not found".format(cls.name(), config_file, command[0]))
                except subprocess.CalledProcessError:
                    print(
                        "{}: WARN: {} detected but {} exited with non-zero exit status".format(
                            cls.name(), config_file, " ".join(map(shlex.quote, command))
                        )
                    )

        def binary_from_venv(root: str, child: str) -> Optional[str]:
            if os.path.isfile(os.path.join(root, child, "pyvenv.cfg")):
                return binary_from_python_path(os.path.join(root, child))

        # virtual environment as subfolder in project
        for file in ["venv", ".venv"]:
            binary = binary_from_venv(workspace_folder, file)
            if binary is not None:
                return binary

        for file in os.listdir(workspace_folder):
            if file in {"venv", ".venv"}:
                continue
            binary = binary_from_venv(workspace_folder, file)
            if binary is not None:
                return binary

        return None
