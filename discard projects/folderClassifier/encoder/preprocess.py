from __future__ import annotations

import argparse
import json
from pathlib import Path


class FilePathEncoder:
    def __init__(self, folder_path: str | Path):
        self.root = Path(folder_path).expanduser().resolve(strict=False)
        self.root_path = str(self.root)
        self.original_paths: list[str] = []
        self.encoded_paths: list[str] = []
        self.sizes: dict[str, str] = {}
        self.encoded_sizes: dict[str, str] = {}
        self.id_to_name: dict[int, str] = {}
        self._name_to_id: dict[str, int] = {}

    def collect_file_paths(self) -> list[Path]:
        if not self.root.is_dir():
            raise NotADirectoryError(self.root_path)

        return sorted(
            (
                path.resolve(strict=False)
                for path in self.root.rglob("*")
                if path.is_file()
            ),
            key=lambda path: str(path).casefold(),
        )

    def collect_folder_paths(self) -> list[Path]:
        if not self.root.is_dir():
            raise NotADirectoryError(self.root_path)

        folders = [self.root]
        folders.extend(
            path.resolve(strict=False)
            for path in self.root.rglob("*")
            if path.is_dir()
        )
        return sorted(folders, key=lambda path: str(path).casefold())

    def collect_sizes(self, file_paths: list[Path]) -> dict[str, int]:
        sizes = {str(folder): 0 for folder in self.collect_folder_paths()}

        for file_path in file_paths:
            try:
                file_size = file_path.stat().st_size
            except OSError:
                continue

            sizes[str(file_path)] = file_size
            folder = file_path.parent.resolve(strict=False)
            while True:
                sizes[str(folder)] = sizes.get(str(folder), 0) + file_size
                if folder == self.root:
                    break
                if self.root not in folder.parents:
                    break
                folder = folder.parent

        return sizes

    def encode_name(self, name: str) -> int:
        if name not in self._name_to_id:
            next_id = len(self._name_to_id) + 1
            self._name_to_id[name] = next_id
            self.id_to_name[next_id] = name

        return self._name_to_id[name]

    def encode_path(self, path: str | Path) -> str:
        target = Path(path).resolve(strict=False)
        if target == self.root:
            return "."

        relative_parts = target.relative_to(self.root).parts
        encoded_parts = [str(self.encode_name(part)) for part in relative_parts]
        return "/".join(encoded_parts)

    @staticmethod
    def format_size(size_bytes: int) -> str:
        size = float(max(size_bytes, 0))
        units = ("B", "KB", "MB", "GB", "TB", "PB")

        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(size)}B"

                text = f"{size:.2f}".rstrip("0").rstrip(".")
                return f"{text}{unit}"

            size /= 1024

        return f"{int(size_bytes)}B"

    def encode(self) -> FilePathEncoder:
        self.original_paths = []
        self.encoded_paths = []
        self.sizes = {}
        self.encoded_sizes = {}
        self.id_to_name = {}
        self._name_to_id = {}

        file_paths = self.collect_file_paths()
        self.original_paths = [str(path) for path in file_paths]
        raw_sizes = self.collect_sizes(file_paths)
        self.sizes = {
            path: self.format_size(size)
            for path, size in raw_sizes.items()
        }

        for file_path in file_paths:
            self.encoded_paths.append(self.encode_path(file_path))

        self.encoded_sizes = {
            self.encode_path(path): self.format_size(size)
            for path, size in raw_sizes.items()
        }

        return self

    def to_dict(self) -> dict:
        return {
            "root_path": self.root_path,
            "original_paths": self.original_paths,
            "encoded_paths": self.encoded_paths,
            "sizes": self.sizes,
            "encoded_sizes": self.encoded_sizes,
            "id_to_name": self.id_to_name,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=4)

    def save(self, output_path: str | Path) -> None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as file:
            file.write(self.to_json())
            file.write("\n")

    @classmethod
    def main(cls) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("selected_folder_path")
        parser.add_argument("-o", "--output")
        args = parser.parse_args()

        preprocessor = cls(args.selected_folder_path).encode()
        if args.output:
            preprocessor.save(args.output)
            return

        print(preprocessor.to_json())

if __name__ == "__main__":
    FilePathEncoder.main()
