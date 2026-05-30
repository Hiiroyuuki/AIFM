from __future__ import annotations

import argparse
import hashlib
import json
import re
import warnings
from pathlib import Path

import torch
from torch import nn


class PathStructureTokenizer:
    PAD_ID = 0
    CLS_ID = 1
    DIR_START_ID = 2
    DIR_END_ID = 3
    FILE_ID = 4
    EXT_ID = 5
    HASH_OFFSET = 16

    PAD_TYPE = 0
    CLS_TYPE = 1
    STRUCTURE_TYPE = 2
    DIR_NAME_TYPE = 3
    FILE_NAME_TYPE = 4
    EXTENSION_TYPE = 5
    TOKEN_TYPE_COUNT = 6

    SPECIAL_TOKENS = {
        PAD_ID: "[PAD]",
        CLS_ID: "[CLS]",
        DIR_START_ID: "[DIR_START]",
        DIR_END_ID: "[DIR_END]",
        FILE_ID: "[FILE]",
        EXT_ID: "[EXT]",
    }

    def __init__(
        self,
        hash_bucket_count: int = 65536,
        lowercase: bool = True,
        max_component_tokens: int = 16,
    ):
        self.hash_bucket_count = hash_bucket_count
        self.lowercase = lowercase
        self.max_component_tokens = max_component_tokens
        self.vocab_size = self.HASH_OFFSET + hash_bucket_count

    def tokenize_folder(self, folder_path: str | Path) -> dict:
        root = Path(folder_path).expanduser().resolve(strict=False)
        if not root.is_dir():
            raise NotADirectoryError(str(root))

        token_ids = [self.CLS_ID]
        depth_ids = [0]
        type_ids = [self.CLS_TYPE]
        self._append_directory(root, 0, token_ids, depth_ids, type_ids)
        self.validate_sequence(token_ids, depth_ids, type_ids)

        return {
            "root_path": str(root),
            "token_ids": token_ids,
            "depth_ids": depth_ids,
            "type_ids": type_ids,
        }

    def _append_directory(
        self,
        path: Path,
        depth: int,
        token_ids: list[int],
        depth_ids: list[int],
        type_ids: list[int],
    ) -> None:
        self.append_structure_token(
            token_ids,
            depth_ids,
            type_ids,
            self.DIR_START_ID,
            depth,
        )
        self.append_text_tokens(
            token_ids,
            depth_ids,
            type_ids,
            self.directory_display_name(path),
            depth,
            self.DIR_NAME_TYPE,
        )

        for child in self.sorted_children(path):
            if child.is_dir():
                self._append_directory(
                    child.resolve(strict=False),
                    depth + 1,
                    token_ids,
                    depth_ids,
                    type_ids,
                )
            elif child.is_file():
                self._append_file(child.resolve(strict=False), depth + 1, token_ids, depth_ids, type_ids)

        self.append_structure_token(
            token_ids,
            depth_ids,
            type_ids,
            self.DIR_END_ID,
            depth,
        )

    def _append_file(
        self,
        path: Path,
        depth: int,
        token_ids: list[int],
        depth_ids: list[int],
        type_ids: list[int],
    ) -> None:
        stem, extension = self.split_file_name(path.name)
        self.append_structure_token(token_ids, depth_ids, type_ids, self.FILE_ID, depth)
        self.append_text_tokens(
            token_ids,
            depth_ids,
            type_ids,
            stem,
            depth,
            self.FILE_NAME_TYPE,
        )
        if extension:
            self.append_structure_token(token_ids, depth_ids, type_ids, self.EXT_ID, depth)
            self.append_text_tokens(
                token_ids,
                depth_ids,
                type_ids,
                extension,
                depth,
                self.EXTENSION_TYPE,
            )

    def sorted_children(self, path: Path) -> list[Path]:
        try:
            children = list(path.iterdir())
        except OSError:
            return []

        return sorted(
            children,
            key=lambda child: (0 if child.is_dir() else 1, child.name.casefold()),
        )

    def append_structure_token(
        self,
        token_ids: list[int],
        depth_ids: list[int],
        type_ids: list[int],
        token_id: int,
        depth: int,
    ) -> None:
        token_ids.append(token_id)
        depth_ids.append(depth)
        type_ids.append(self.STRUCTURE_TYPE)

    def append_text_tokens(
        self,
        token_ids: list[int],
        depth_ids: list[int],
        type_ids: list[int],
        text: str,
        depth: int,
        token_type: int,
    ) -> None:
        tokens = self.split_text(text)[: self.max_component_tokens]
        if not tokens:
            tokens = ["<empty>"]

        for token in tokens:
            token_ids.append(self.hash_token(token))
            depth_ids.append(depth)
            type_ids.append(token_type)

    def split_file_name(self, file_name: str) -> tuple[str, str]:
        path = Path(file_name)
        extension = path.suffix[1:] if path.suffix else ""
        stem = path.stem if extension else file_name
        return stem, extension

    def split_text(self, text: str) -> list[str]:
        normalized = text.lower() if self.lowercase else text
        return re.findall(
            r"[\u4e00-\u9fff]+|[A-Za-z]+|\d+|[^A-Za-z0-9\s]",
            normalized,
        )

    def hash_token(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, byteorder="big", signed=False)
        return self.HASH_OFFSET + (value % self.hash_bucket_count)

    def directory_display_name(self, path: Path) -> str:
        return path.name or str(path)

    @staticmethod
    def validate_sequence(
        token_ids: list[int],
        depth_ids: list[int],
        type_ids: list[int],
    ) -> None:
        if not (len(token_ids) == len(depth_ids) == len(type_ids)):
            raise ValueError("token_ids, depth_ids, and type_ids must have the same length.")

    def tokenizer_info(self) -> dict:
        return {
            "type": "stable_hash_folder_tree_tokenizer",
            "hash_bucket_count": self.hash_bucket_count,
            "vocab_size": self.vocab_size,
            "lowercase": self.lowercase,
            "max_component_tokens": self.max_component_tokens,
            "special_tokens": self.SPECIAL_TOKENS,
        }


class FileStructureTransformer(nn.Module):
    def __init__(
        self,
        vocab_size: int,
        feature_dim: int = 128,
        num_heads: int = 8,
        num_layers: int = 4,
        feedforward_dim: int = 256,
        dropout: float = 0.1,
        max_tokens: int = 2048,
        max_depth: int = 128,
        token_type_count: int = PathStructureTokenizer.TOKEN_TYPE_COUNT,
        use_projection_head: bool = True,
    ):
        super().__init__()
        self.feature_dim = feature_dim
        self.max_tokens = max_tokens
        self.use_projection_head = use_projection_head
        self.token_embedding = nn.Embedding(vocab_size, feature_dim, padding_idx=0)
        self.position_embedding = nn.Embedding(max_tokens, feature_dim)
        self.depth_embedding = nn.Embedding(max_depth + 1, feature_dim)
        self.type_embedding = nn.Embedding(token_type_count, feature_dim)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=feature_dim,
            nhead=num_heads,
            dim_feedforward=feedforward_dim,
            dropout=dropout,
            batch_first=True,
        )
        self.encoder = nn.TransformerEncoder(
            encoder_layer,
            num_layers=num_layers,
            enable_nested_tensor=False,
        )
        self.output_norm = nn.LayerNorm(feature_dim)
        self.projection = (
            nn.Sequential(
                nn.Linear(feature_dim, feature_dim),
                nn.ReLU(),
                nn.LayerNorm(feature_dim),
            )
            if use_projection_head
            else nn.Identity()
        )

    def forward(
        self,
        token_ids: torch.Tensor,
        depth_ids: torch.Tensor,
        type_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        return self.forward_features(token_ids, depth_ids, type_ids, attention_mask)

    def forward_features(
        self,
        token_ids: torch.Tensor,
        depth_ids: torch.Tensor,
        type_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        batch_size, sequence_length = token_ids.shape
        positions = torch.arange(sequence_length, device=token_ids.device)
        positions = positions.unsqueeze(0).expand(batch_size, sequence_length)

        hidden = (
            self.token_embedding(token_ids)
            + self.position_embedding(positions)
            + self.depth_embedding(depth_ids)
            + self.type_embedding(type_ids)
        )
        padding_mask = ~attention_mask.bool()
        encoded = self.encoder(hidden, src_key_padding_mask=padding_mask)
        encoded = self.output_norm(encoded)
        cls_feature = self.projection(encoded[:, 0])
        return cls_feature, encoded


class FolderTransformerFeatureExtractor:
    def __init__(
        self,
        feature_dim: int = 128,
        num_heads: int = 8,
        num_layers: int = 4,
        feedforward_dim: int = 256,
        dropout: float = 0.1,
        max_tokens: int = 2048,
        max_depth: int = 128,
        batch_size: int = 8,
        hash_bucket_count: int = 65536,
        max_component_tokens: int = 16,
        seed: int = 42,
        device: str | torch.device | None = None,
        checkpoint_path: str | Path | None = None,
        use_projection_head: bool = True,
    ):
        if feature_dim % num_heads != 0:
            raise ValueError("feature_dim must be divisible by num_heads.")

        self.feature_dim = feature_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.feedforward_dim = feedforward_dim
        self.dropout = dropout
        self.max_tokens = max_tokens
        self.max_depth = max_depth
        self.batch_size = batch_size
        self.seed = seed
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.use_projection_head = use_projection_head
        self.tokenizer = PathStructureTokenizer(
            hash_bucket_count=hash_bucket_count,
            max_component_tokens=max_component_tokens,
        )
        self.model: FileStructureTransformer | None = None
        self.set_seed(seed)
        if checkpoint_path:
            self.load_checkpoint(checkpoint_path)
        else:
            warnings.warn(
                "Model is randomly initialized. Output features are not semantically meaningful "
                "until the model is trained or a checkpoint is loaded.",
                RuntimeWarning,
                stacklevel=2,
            )

    def encode_folder(self, folder_path: str | Path) -> dict:
        tokenized = self.tokenizer.tokenize_folder(folder_path)
        if self.model is None:
            self.prepare_model()
        self.model.eval()
        chunks = self.build_chunks(tokenized)
        feature = self.encode_chunks(chunks)

        return {
            "root_path": tokenized["root_path"],
            "feature_dim": self.feature_dim,
            "file_count": self.count_token(tokenized["token_ids"], PathStructureTokenizer.FILE_ID),
            "directory_count": self.count_token(
                tokenized["token_ids"],
                PathStructureTokenizer.DIR_START_ID,
            ),
            "chunk_count": len(chunks),
            "token_count": len(tokenized["token_ids"]),
            "feature": feature.detach().cpu().tolist(),
            "tokenizer": self.tokenizer.tokenizer_info(),
        }

    @staticmethod
    def set_seed(seed: int | None) -> None:
        if seed is None:
            return

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)

    def prepare_model(self) -> None:
        self.model = FileStructureTransformer(
            vocab_size=self.tokenizer.vocab_size,
            feature_dim=self.feature_dim,
            num_heads=self.num_heads,
            num_layers=self.num_layers,
            feedforward_dim=self.feedforward_dim,
            dropout=self.dropout,
            max_tokens=self.max_tokens,
            max_depth=self.max_depth,
            use_projection_head=self.use_projection_head,
        ).to(self.device)

    def build_chunks(self, tokenized: dict) -> list[dict[str, list[int]]]:
        self.tokenizer.validate_sequence(
            tokenized["token_ids"],
            tokenized["depth_ids"],
            tokenized["type_ids"],
        )
        if len(tokenized["token_ids"]) <= self.max_tokens:
            return [tokenized]

        chunks = []
        payload_size = max(self.max_tokens - 1, 1)
        token_payload = tokenized["token_ids"][1:]
        depth_payload = tokenized["depth_ids"][1:]
        type_payload = tokenized["type_ids"][1:]

        for start in range(0, len(token_payload), payload_size):
            # This simple chunking may split directory subtrees. For better structure preservation, implement subtree-aware chunking later.
            end = start + payload_size
            chunk = {
                "root_path": tokenized["root_path"],
                "token_ids": [
                    PathStructureTokenizer.CLS_ID,
                    *token_payload[start:end],
                ],
                "depth_ids": [0, *depth_payload[start:end]],
                "type_ids": [
                    PathStructureTokenizer.CLS_TYPE,
                    *type_payload[start:end],
                ],
            }
            self.tokenizer.validate_sequence(
                chunk["token_ids"],
                chunk["depth_ids"],
                chunk["type_ids"],
            )
            chunks.append(chunk)

        return chunks or [tokenized]

    def encode_token_batch(
        self,
        token_ids: torch.Tensor,
        depth_ids: torch.Tensor,
        type_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        if self.model is None:
            raise RuntimeError("Model has not been prepared.")

        return self.model.forward_features(token_ids, depth_ids, type_ids, attention_mask)

    def encode_chunks(self, chunks: list[dict[str, list[int]]]) -> torch.Tensor:
        if self.model is None:
            raise RuntimeError("Model has not been prepared.")

        features = []
        weights = []
        with torch.inference_mode():
            for start in range(0, len(chunks), self.batch_size):
                batch = chunks[start : start + self.batch_size]
                tensors = self.batch_to_tensors(batch)
                chunk_features, _encoded_tokens = self.encode_token_batch(**tensors)
                valid_token_counts = tensors["attention_mask"].sum(dim=1).clamp(min=1)
                features.append(chunk_features.detach().cpu())
                weights.append(valid_token_counts.detach().cpu().float())

        stacked = torch.cat(features, dim=0)
        stacked_weights = torch.cat(weights, dim=0).unsqueeze(1)
        return (stacked * stacked_weights).sum(dim=0) / stacked_weights.sum()

    def batch_to_tensors(
        self,
        chunks: list[dict[str, list[int]]],
    ) -> dict[str, torch.Tensor]:
        token_rows = []
        depth_rows = []
        type_rows = []
        mask_rows = []

        for chunk in chunks:
            token_ids = chunk["token_ids"][: self.max_tokens]
            depth_ids = [
                min(depth, self.max_depth)
                for depth in chunk["depth_ids"][: self.max_tokens]
            ]
            type_ids = chunk["type_ids"][: self.max_tokens]
            valid_length = len(token_ids)
            pad_length = self.max_tokens - valid_length

            token_rows.append(token_ids + [PathStructureTokenizer.PAD_ID] * pad_length)
            depth_rows.append(depth_ids + [0] * pad_length)
            type_rows.append(type_ids + [PathStructureTokenizer.PAD_TYPE] * pad_length)
            mask_rows.append([1] * valid_length + [0] * pad_length)

        return {
            "token_ids": torch.tensor(token_rows, dtype=torch.long, device=self.device),
            "depth_ids": torch.tensor(depth_rows, dtype=torch.long, device=self.device),
            "type_ids": torch.tensor(type_rows, dtype=torch.long, device=self.device),
            "attention_mask": torch.tensor(mask_rows, dtype=torch.bool, device=self.device),
        }

    @staticmethod
    def count_token(token_ids: list[int], token_id: int) -> int:
        return sum(1 for item in token_ids if item == token_id)

    @staticmethod
    def save_feature(feature_data: dict, output_path: str | Path) -> None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as file:
            json.dump(feature_data, file, ensure_ascii=False, indent=4)
            file.write("\n")

    def save_checkpoint(self, output_path: str | Path) -> None:
        if self.model is None:
            self.prepare_model()

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "model_state_dict": self.model.state_dict(),
                "feature_dim": self.feature_dim,
                "num_heads": self.num_heads,
                "num_layers": self.num_layers,
                "feedforward_dim": self.feedforward_dim,
                "dropout": self.dropout,
                "max_tokens": self.max_tokens,
                "max_depth": self.max_depth,
                "use_projection_head": self.use_projection_head,
                "tokenizer_info": self.tokenizer.tokenizer_info(),
            },
            output,
        )

    def load_checkpoint(self, checkpoint_path: str | Path) -> None:
        checkpoint = torch.load(Path(checkpoint_path), map_location=self.device)
        tokenizer_info = checkpoint.get("tokenizer_info", {})
        if tokenizer_info:
            self.tokenizer = PathStructureTokenizer(
                hash_bucket_count=int(
                    tokenizer_info.get("hash_bucket_count", self.tokenizer.hash_bucket_count)
                ),
                lowercase=bool(tokenizer_info.get("lowercase", self.tokenizer.lowercase)),
                max_component_tokens=int(
                    tokenizer_info.get("max_component_tokens", self.tokenizer.max_component_tokens)
                ),
            )

        self.feature_dim = int(checkpoint.get("feature_dim", self.feature_dim))
        self.num_heads = int(checkpoint.get("num_heads", self.num_heads))
        self.num_layers = int(checkpoint.get("num_layers", self.num_layers))
        self.feedforward_dim = int(checkpoint.get("feedforward_dim", self.feedforward_dim))
        self.dropout = float(checkpoint.get("dropout", self.dropout))
        self.max_tokens = int(checkpoint.get("max_tokens", self.max_tokens))
        self.max_depth = int(checkpoint.get("max_depth", self.max_depth))
        self.use_projection_head = bool(
            checkpoint.get("use_projection_head", self.use_projection_head)
        )
        if self.feature_dim % self.num_heads != 0:
            raise ValueError("feature_dim must be divisible by num_heads.")

        self.model = FileStructureTransformer(
            vocab_size=self.tokenizer.vocab_size,
            feature_dim=self.feature_dim,
            num_heads=self.num_heads,
            num_layers=self.num_layers,
            feedforward_dim=self.feedforward_dim,
            dropout=self.dropout,
            max_tokens=self.max_tokens,
            max_depth=self.max_depth,
            use_projection_head=self.use_projection_head,
        ).to(self.device)
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model.eval()

    @classmethod
    def main(cls) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("selected_folder_path")
        parser.add_argument("-o", "--output")
        parser.add_argument("--feature-dim", type=int, default=128)
        parser.add_argument("--num-heads", type=int, default=8)
        parser.add_argument("--num-layers", type=int, default=4)
        parser.add_argument("--max-tokens", type=int, default=2048)
        parser.add_argument("--batch-size", type=int, default=8)
        parser.add_argument("--hash-buckets", type=int, default=65536)
        parser.add_argument("--max-component-tokens", type=int, default=16)
        parser.add_argument("--device", default=None)
        parser.add_argument("--checkpoint")
        parser.add_argument("--save-checkpoint")
        args = parser.parse_args()

        extractor = cls(
            feature_dim=args.feature_dim,
            num_heads=args.num_heads,
            num_layers=args.num_layers,
            max_tokens=args.max_tokens,
            batch_size=args.batch_size,
            hash_bucket_count=args.hash_buckets,
            max_component_tokens=args.max_component_tokens,
            device=args.device,
            checkpoint_path=args.checkpoint,
        )
        feature_data = extractor.encode_folder(args.selected_folder_path)

        if args.save_checkpoint:
            extractor.save_checkpoint(args.save_checkpoint)

        if args.output:
            extractor.save_feature(feature_data, args.output)
            return

        print(json.dumps(feature_data, ensure_ascii=False, indent=4))


if __name__ == "__main__":
    FolderTransformerFeatureExtractor.main()
