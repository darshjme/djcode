"""Software installation helper for DJcode.

Detects the system package manager, checks if tools are installed,
and provides install commands or performs installation with user confirmation.
No auto-install without consent. Cross-platform: macOS, Debian/Ubuntu, Fedora, Arch.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------

@dataclass
class PackageInfo:
    """Installation metadata for a known package across managers."""

    brew: str | None = None
    apt: str | None = None
    dnf: str | None = None
    pacman: str | None = None
    pip: str | None = None
    cargo: str | None = None
    url: str | None = None
    binary_name: str | None = None  # Override the command name to check

    def get_install_name(self, manager: str) -> str | None:
        """Get the package name for a given manager."""
        return getattr(self, manager, None)


# Known packages DJcode might need
KNOWN_PACKAGES: dict[str, PackageInfo] = {
    "ollama": PackageInfo(
        brew="ollama",
        url="https://ollama.com",
    ),
    "docker": PackageInfo(
        brew="docker",
        apt="docker.io",
        dnf="docker",
        pacman="docker",
        url="https://docs.docker.com/get-docker/",
    ),
    "git": PackageInfo(
        brew="git",
        apt="git",
        dnf="git",
        pacman="git",
    ),
    "rg": PackageInfo(
        brew="ripgrep",
        apt="ripgrep",
        dnf="ripgrep",
        pacman="ripgrep",
        cargo="ripgrep",
    ),
    "fd": PackageInfo(
        brew="fd",
        apt="fd-find",
        dnf="fd-find",
        pacman="fd",
        cargo="fd-find",
    ),
    "jq": PackageInfo(
        brew="jq",
        apt="jq",
        dnf="jq",
        pacman="jq",
    ),
    "uv": PackageInfo(
        pip="uv",
        url="https://docs.astral.sh/uv/",
    ),
    "node": PackageInfo(
        brew="node",
        apt="nodejs",
        dnf="nodejs",
        pacman="nodejs",
        url="https://nodejs.org/",
    ),
    "python3": PackageInfo(
        brew="python3",
        apt="python3",
        dnf="python3",
        pacman="python",
    ),
    "ffmpeg": PackageInfo(
        brew="ffmpeg",
        apt="ffmpeg",
        dnf="ffmpeg",
        pacman="ffmpeg",
    ),
    "whisper-cpp": PackageInfo(
        brew="whisper-cpp",
        url="https://github.com/ggerganov/whisper.cpp",
        binary_name="whisper-cpp",
    ),
    "cmake": PackageInfo(
        brew="cmake",
        apt="cmake",
        dnf="cmake",
        pacman="cmake",
    ),
    "rust": PackageInfo(
        brew="rustup",
        url="https://rustup.rs/",
        binary_name="rustc",
    ),
    "go": PackageInfo(
        brew="go",
        apt="golang",
        dnf="golang",
        pacman="go",
        url="https://go.dev/dl/",
    ),
    "redis": PackageInfo(
        brew="redis",
        apt="redis-server",
        dnf="redis",
        pacman="redis",
        binary_name="redis-server",
    ),
    "tree": PackageInfo(
        brew="tree",
        apt="tree",
        dnf="tree",
        pacman="tree",
    ),
    "wget": PackageInfo(
        brew="wget",
        apt="wget",
        dnf="wget",
        pacman="wget",
    ),
    "curl": PackageInfo(
        brew="curl",
        apt="curl",
        dnf="curl",
        pacman="curl",
    ),
}


# ---------------------------------------------------------------------------
# Package manager detection
# ---------------------------------------------------------------------------

class SoftwareInstaller:
    """Detects and installs missing software dependencies."""

    # Manager detection order (most specific first)
    _MANAGERS = [
        ("brew", "brew"),
        ("apt", "apt-get"),
        ("dnf", "dnf"),
        ("pacman", "pacman"),
        ("pip", "pip3"),
    ]

    def __init__(self) -> None:
        self._detected_manager: str | None = None
        self._detected_manager_cmd: str | None = None

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect_package_manager(self) -> str:
        """Detect the primary system package manager.

        Returns the manager key (brew, apt, dnf, pacman, pip) or 'unknown'.
        """
        if self._detected_manager is not None:
            return self._detected_manager

        for key, binary in self._MANAGERS:
            if shutil.which(binary):
                self._detected_manager = key
                self._detected_manager_cmd = binary
                return key

        self._detected_manager = "unknown"
        return "unknown"

    def is_installed(self, package: str) -> bool:
        """Check if a package/command is available on PATH."""
        # Use binary_name override if this is a known package
        binary = package
        if package in KNOWN_PACKAGES:
            info = KNOWN_PACKAGES[package]
            if info.binary_name:
                binary = info.binary_name

        return shutil.which(binary) is not None

    # ------------------------------------------------------------------
    # Install commands
    # ------------------------------------------------------------------

    def suggest_install(self, package: str) -> str:
        """Return the install command string for the detected package manager.

        For unknown packages, returns a generic message.
        """
        manager = self.detect_package_manager()
        info = KNOWN_PACKAGES.get(package)

        if info is None:
            # Unknown package — try generic install
            return self._generic_install_cmd(manager, package)

        # Try the detected manager first
        pkg_name = info.get_install_name(manager)
        if pkg_name:
            return self._build_install_cmd(manager, pkg_name)

        # Try pip fallback
        if info.pip:
            return f"pip install {info.pip}"

        # Try cargo fallback
        if info.cargo:
            return f"cargo install {info.cargo}"

        # URL fallback
        if info.url:
            return f"Visit: {info.url}"

        return f"No install method found for '{package}' on {manager}"

    def suggest_all_methods(self, package: str) -> list[str]:
        """Return all known install methods for a package."""
        info = KNOWN_PACKAGES.get(package)
        if info is None:
            return [f"Package '{package}' not in known registry."]

        methods: list[str] = []
        for mgr_key, _ in self._MANAGERS:
            pkg_name = info.get_install_name(mgr_key)
            if pkg_name:
                methods.append(self._build_install_cmd(mgr_key, pkg_name))
        if info.cargo:
            methods.append(f"cargo install {info.cargo}")
        if info.url:
            methods.append(f"Manual: {info.url}")
        return methods

    def install(self, package: str, confirm: bool = True) -> bool:
        """Install a package. Shows command and asks for confirmation.

        Args:
            package: The package to install (key from KNOWN_PACKAGES or raw name).
            confirm: If True, ask for user confirmation before running.

        Returns:
            True if installation succeeded, False otherwise.
        """
        cmd = self.suggest_install(package)

        if cmd.startswith("Visit:") or cmd.startswith("No install"):
            print(f"Cannot auto-install '{package}'. {cmd}")
            return False

        if confirm:
            print(f"Install command: {cmd}")
            try:
                answer = input("Run this command? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.")
                return False
            if answer not in ("y", "yes"):
                print("Cancelled.")
                return False

        try:
            # Split into parts for subprocess
            parts = cmd.split()

            # Use sudo for apt/dnf/pacman if not root
            needs_sudo = parts[0] in ("apt-get", "dnf", "pacman") and os.geteuid() != 0
            if needs_sudo:
                parts = ["sudo"] + parts

            result = subprocess.run(
                parts,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                print(f"Successfully installed '{package}'.")
                return True
            else:
                print(f"Installation failed (exit code {result.returncode}).")
                if result.stderr:
                    # Show last 5 lines of stderr
                    err_lines = result.stderr.strip().splitlines()[-5:]
                    for line in err_lines:
                        print(f"  {line}")
                return False

        except subprocess.TimeoutExpired:
            print("Installation timed out after 5 minutes.")
            return False
        except FileNotFoundError:
            print(f"Package manager command not found: {parts[0]}")
            return False
        except Exception as exc:
            print(f"Installation error: {exc}")
            return False

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    def check_multiple(self, packages: list[str]) -> dict[str, bool]:
        """Check installation status of multiple packages at once."""
        return {pkg: self.is_installed(pkg) for pkg in packages}

    def suggest_missing(self, packages: list[str]) -> list[tuple[str, str]]:
        """For each missing package, return (package, install_command) tuples."""
        results: list[tuple[str, str]] = []
        for pkg in packages:
            if not self.is_installed(pkg):
                results.append((pkg, self.suggest_install(pkg)))
        return results

    # ------------------------------------------------------------------
    # Status / diagnostics
    # ------------------------------------------------------------------

    def system_info(self) -> dict[str, str]:
        """Return basic system info for diagnostics."""
        return {
            "os": platform.system(),
            "os_version": platform.version(),
            "arch": platform.machine(),
            "python": sys.version.split()[0],
            "package_manager": self.detect_package_manager(),
            "shell": os.environ.get("SHELL", "unknown"),
        }

    def health_check(self, packages: list[str] | None = None) -> str:
        """Run a health check on essential or specified packages.

        Returns a formatted string report.
        """
        if packages is None:
            packages = ["git", "python3", "node", "docker", "rg", "fd", "jq", "uv", "ollama"]

        lines = [f"System: {platform.system()} {platform.machine()}"]
        lines.append(f"Package manager: {self.detect_package_manager()}")
        lines.append("")

        installed_count = 0
        for pkg in packages:
            status = self.is_installed(pkg)
            icon = "+" if status else "-"
            if status:
                installed_count += 1
                # Try to get version
                version = self._get_version(pkg)
                version_str = f" ({version})" if version else ""
                lines.append(f"  [{icon}] {pkg}{version_str}")
            else:
                cmd = self.suggest_install(pkg)
                lines.append(f"  [{icon}] {pkg} — install: {cmd}")

        lines.append(f"\n{installed_count}/{len(packages)} packages available")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_install_cmd(self, manager: str, package_name: str) -> str:
        """Build the full install command for a manager + package."""
        cmds = {
            "brew": f"brew install {package_name}",
            "apt": f"apt-get install -y {package_name}",
            "dnf": f"dnf install -y {package_name}",
            "pacman": f"pacman -S --noconfirm {package_name}",
            "pip": f"pip install {package_name}",
        }
        return cmds.get(manager, f"{manager} install {package_name}")

    def _generic_install_cmd(self, manager: str, package: str) -> str:
        """Build a generic install command for an unknown package."""
        if manager == "brew":
            return f"brew install {package}"
        elif manager == "apt":
            return f"apt-get install -y {package}"
        elif manager == "dnf":
            return f"dnf install -y {package}"
        elif manager == "pacman":
            return f"pacman -S --noconfirm {package}"
        elif manager == "pip":
            return f"pip install {package}"
        return f"Search for '{package}' in your system package manager"

    def _get_version(self, package: str) -> str:
        """Try to get the version of an installed package."""
        binary = package
        if package in KNOWN_PACKAGES:
            info = KNOWN_PACKAGES[package]
            if info.binary_name:
                binary = info.binary_name

        for flag in ("--version", "-V", "version"):
            try:
                result = subprocess.run(
                    [binary, flag],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Return first line, trimmed
                    first_line = result.stdout.strip().splitlines()[0]
                    # Extract just the version number if possible
                    import re

                    match = re.search(r"(\d+\.\d+[\.\d]*)", first_line)
                    if match:
                        return match.group(1)
                    return first_line[:60]
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
        return ""


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_installer: SoftwareInstaller | None = None


def get_installer() -> SoftwareInstaller:
    """Get or create the global SoftwareInstaller instance."""
    global _installer
    if _installer is None:
        _installer = SoftwareInstaller()
    return _installer


def is_installed(package: str) -> bool:
    """Quick check if a package is installed."""
    return get_installer().is_installed(package)


def suggest_install(package: str) -> str:
    """Quick install suggestion for a package."""
    return get_installer().suggest_install(package)


def ensure_installed(package: str, auto_confirm: bool = False) -> bool:
    """Check if installed, prompt to install if not.

    Returns True if the package is available (already or newly installed).
    """
    inst = get_installer()
    if inst.is_installed(package):
        return True
    return inst.install(package, confirm=not auto_confirm)
