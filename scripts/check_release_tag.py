"""Validate a SemVer release tag and derive a Docker-safe image tag."""

import argparse
import re
from pathlib import Path

NUMERIC = r"(?:0|[1-9][0-9]*)"
IDENTIFIER = rf"(?:{NUMERIC}|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
TAG = re.compile(
    rf"v{NUMERIC}\.{NUMERIC}\.{NUMERIC}"
    rf"(?P<pre>-(?:{IDENTIFIER})(?:\.{IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def classify(tag: str) -> tuple[str, str, bool]:
    match = TAG.fullmatch(tag)
    if match is None:
        raise ValueError(
            "Expected vMAJOR.MINOR.PATCH with optional SemVer prerelease/build metadata"
        )
    version = tag.removeprefix("v")
    image_tag = version.replace("+", "_")
    if len(image_tag) > 128:
        raise ValueError("Derived Docker tag exceeds 128 characters")
    return version, image_tag, match.group("pre") is None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        version, image_tag, stable = classify(args.tag)
    except ValueError as error:
        parser.error(str(error))
    output = f"version={version}\nimage_tag={image_tag}\nstable={str(stable).lower()}\n"
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
