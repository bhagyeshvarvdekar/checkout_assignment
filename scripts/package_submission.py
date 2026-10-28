import argparse
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "checkout-payment-gateway"
REQUIRED_FILES = (
    ".dockerignore",
    ".editorconfig",
    ".env.example",
    ".gitignore",
    ".python-version",
    "Dockerfile",
    "Makefile",
    "README.md",
    "docker-compose.yml",
    "main.py",
    "pyproject.toml",
    "poetry.lock",
    "imposters/bank_simulator.ejs",
    "scripts/smoke.py",
    "scripts/package_submission.py",
)
SOURCE_PATTERNS = (
    "payment_gateway_api/**/*.py",
    "tests/**/*.py",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)


def submission_files() -> list[Path]:
    files = {ROOT / name for name in REQUIRED_FILES}
    for pattern in SOURCE_PATTERNS:
        files.update(ROOT.glob(pattern))
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(
                f"Required regular file missing or symlinked: {path.relative_to(ROOT)}"
            )
        if not path.resolve().is_relative_to(ROOT):
            raise ValueError("Submission source resolves outside the project")
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a source ZIP for submission.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"{PREFIX}.zip")
    parser.add_argument(
        "--list", action="store_true", help="List included paths without creating a ZIP"
    )
    args = parser.parse_args()
    try:
        files = submission_files()
        if args.list:
            for path in files:
                print(path.relative_to(ROOT).as_posix())
            return 0
        output = args.output.resolve()
        if output in {path.resolve() for path in files}:
            raise ValueError("Output cannot replace a submission source file")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=output.parent, suffix=".zip", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in files:
                    archive.write(path, f"{PREFIX}/{path.relative_to(ROOT).as_posix()}")
            temporary_path.replace(output)
        finally:
            temporary_path.unlink(missing_ok=True)
        print(f"Created {output} with {len(files)} files under {PREFIX}/")
    except (OSError, ValueError) as exc:
        print(f"Packaging failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
